# SPDX-License-Identifier: Apache-2.0
"""`AWAIT_USER`搬送路の受入test（Phase 8 PR-2c。ADR-0018）。

`advance`がuser requestを払い出し、`submit`がそれへbindした応答を一度だけconsumeする
往復を固定する。`HOST_ACTION`と同じ規則（未応答の再提示・one-time nonce・同一内容の
冪等replay・内容差異の停止）が、ユーザー入力側でも成立することを確かめる。
"""

from __future__ import annotations

import pytest
from c07_support.helpers import verified_chain
from c08_support.helpers import (
    HEAD,
    MAX_RESULT_BYTES,
    NEW_HEAD,
    RUN,
    FakeEvidencePort,
    FakeIds,
    FakeRecordSource,
    permission_resume_payload,
    raw,
    user_env,
    user_machine_state,
    user_record_payload,
    user_submit_payload,
    write_result,
)

from claude_code_codex_review_loop.domain.values import (
    Awaiting,
    IntegrityEvidenceRef,
    MachineState,
    OpaqueBinding,
    OpaqueRef,
    PendingRecord,
    RecordKind,
    State,
)
from claude_code_codex_review_loop.state import (
    CheckpointLoaded,
    checkpoint_path,
    load_checkpoint,
    save_checkpoint,
)
from claude_code_codex_review_loop.workflow import (
    AwaitUser,
    ConsumedIntent,
    EngineStopped,
    UserInputAccepted,
    UserInputReplayed,
    advance,
    intent_key,
    read_consumed_intent,
    read_user_receipt,
    read_user_request,
    submit,
    with_consumed_intent,
)

GATE = Awaiting.USER_INPUT_GATE
DECISION = Awaiting.USER_INPUT_DECISION
PERMISSION = Awaiting.USER_INPUT_PERMISSION

# awaitingごとの代表的なresult kind（registryが許可する組み合わせ）
REPRESENTATIVE_KIND = {
    GATE: RecordKind.GATE_QUESTION,
    DECISION: RecordKind.USER_DECISION,
    PERMISSION: RecordKind.USER_CANCEL,
}


def _payload(env) -> dict[str, object]:
    loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
    assert isinstance(loaded, CheckpointLoaded)
    return loaded.payload


def _issue(env, **overrides) -> AwaitUser:
    outcome = advance(**env.advance_kwargs(**overrides))
    assert isinstance(outcome, AwaitUser), outcome
    return outcome


def _respond(
    env,
    issued: AwaitUser,
    *,
    kind: RecordKind | None = None,
    payload: dict[str, object] | None = None,
    permission: bool = False,
    **envelope_overrides,
):
    """result fileを書き、それを指すsubmit envelopeを組み立てる。"""
    if permission:
        body = permission_resume_payload() if payload is None else payload
        kind_value = None
    else:
        assert kind is not None
        body = user_record_payload(kind) if payload is None else payload
        kind_value = kind.value
    digest = write_result(env.run_dir, issued.request.result_path, body)
    envelope = user_submit_payload(
        request_id=issued.request.request_id,
        nonce=issued.request.nonce,
        result_hash=digest,
        awaiting=env.awaiting,
        result_kind=kind_value,
    )
    envelope.update(envelope_overrides)
    return envelope


def _submit(env, envelope, **overrides):
    return submit(raw(envelope), **env.submit_kwargs(**overrides))


def _save(env, payload: dict[str, object]) -> None:
    save_checkpoint(checkpoint_path(env.paths, RUN), payload)


def user_env_records(env, kinds) -> FakeRecordSource:
    """chainが進んだ状態のrecords port（seq=len(kinds)まで伸びている）。"""
    return FakeRecordSource(records=verified_chain(list(kinds)).records)


def _restate(env, machine_state: MachineState) -> None:
    """checkpointのstateだけを差し替える（user requestは保持する）。"""
    from claude_code_codex_review_loop.workflow import with_machine_state

    save_checkpoint(checkpoint_path(env.paths, RUN), with_machine_state(_payload(env), machine_state))


class TestIssue:
    @pytest.mark.parametrize("awaiting", [GATE, DECISION, PERMISSION], ids=lambda a: a.value)
    def test_each_user_awaiting_gets_a_request(self, tmp_path, awaiting: Awaiting) -> None:
        env = user_env(tmp_path, awaiting=awaiting)
        issued = _issue(env, evidence_port=FakeEvidencePort(()))
        assert issued.awaiting is awaiting
        assert issued.envelope["awaiting"] == awaiting.value
        assert issued.envelope["expected_head_sha"] == HEAD
        assert issued.reissued is False
        assert issued.envelope_path.is_file()

    def test_accepted_kinds_come_from_the_registry(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        assert issued.envelope["accepted_result_kinds"] == [
            "GATE_QUESTION",
            "GATE_CHANGES",
            "MERGE_APPROVAL",
            "USER_CANCEL",
        ]

    def test_since_seq_is_the_current_chain_high_water(self, tmp_path) -> None:
        """awaiting instanceの識別子はrequest発行時点のchain最大seqである。"""
        env = user_env(tmp_path, seeded=(RecordKind.REVIEW_RESULT, RecordKind.FINAL_REPORT))
        issued = _issue(env)
        assert issued.envelope["since_seq"] == 2
        assert issued.request.since_seq == 2

    def test_evidence_is_included_with_head(self, tmp_path) -> None:
        """判断の根拠recordをcomment IDと対象headとともに渡す（AC-C08-07と同型）。"""
        env = user_env(tmp_path)
        issued = _issue(env)
        assert issued.envelope["verified_records"] == [
            {"comment_id": env.records[0].comment_id, "head_sha": HEAD}
        ]

    def test_evidence_of_another_kind_stops(self, tmp_path) -> None:
        env = user_env(tmp_path)
        outcome = advance(
            **env.advance_kwargs(
                evidence_port=FakeEvidencePort(verified_chain([RecordKind.REVIEW_RESULT]).records)
            )
        )
        assert isinstance(outcome, EngineStopped) and outcome.code == "evidence_kind"

    def test_a_broken_chain_stops_before_asking_the_user(self, tmp_path) -> None:
        """壊れたchainの上でユーザーへ判断を求めない（承認をbindできない）。"""
        env = user_env(tmp_path)
        violation = IntegrityEvidenceRef(
            binding=OpaqueBinding("iv:gap:run-1:2"),
            descriptor=OpaqueRef("gap"),
            head=OpaqueRef(HEAD),
        )
        outcome = advance(
            **env.advance_kwargs(
                records_port=FakeRecordSource(records=env.records, violations=(violation,))
            )
        )
        assert isinstance(outcome, EngineStopped) and outcome.code == "chain_violation"


class TestReissue:
    def test_resume_presents_the_same_request(self, tmp_path) -> None:
        env = user_env(tmp_path)
        first = _issue(env)
        again = _issue(env, id_source=FakeIds("other"))
        assert again.reissued is True
        assert again.request == first.request
        assert again.envelope == first.envelope

    def test_missing_envelope_stops(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        issued.envelope_path.unlink()
        outcome = advance(**env.advance_kwargs())
        assert isinstance(outcome, EngineStopped) and outcome.code == "request_missing"

    def test_tampered_envelope_stops(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        text = issued.envelope_path.read_text(encoding="utf-8")
        issued.envelope_path.write_text(text.replace('"since_seq":1', '"since_seq":9'), encoding="utf-8")
        outcome = advance(**env.advance_kwargs())
        assert isinstance(outcome, EngineStopped) and outcome.code in {
            "request_mismatch",
            "request_invalid",
        }
    def test_a_broken_chain_stops_before_reissuing(self, tmp_path) -> None:
        """request発行**後**にchainが壊れた場合も、再提示せず停止する（決定13）。

        pendingがあるからと素通しすると、壊れたchainの上で判断を求めることになる。
        """
        env = user_env(tmp_path)
        _issue(env)
        violation = IntegrityEvidenceRef(
            binding=OpaqueBinding("iv:gap:run-1:2"),
            descriptor=OpaqueRef("gap"),
            head=OpaqueRef(HEAD),
        )
        outcome = advance(
            **env.advance_kwargs(
                records_port=FakeRecordSource(records=env.records, violations=(violation,))
            )
        )
        assert isinstance(outcome, EngineStopped) and outcome.code == "chain_violation"

    def test_a_request_for_another_awaiting_is_replaced(self, tmp_path) -> None:
        """C-01が別の入力を待っていれば、古いrequestを再提示せず新規発行する。"""
        env = user_env(tmp_path, awaiting=GATE)
        first = _issue(env)
        _restate(env, user_machine_state(DECISION))
        outcome = _issue(env, id_source=FakeIds("req2"), evidence_port=FakeEvidencePort(()))
        assert outcome.reissued is False
        assert outcome.awaiting is DECISION
        assert outcome.request.request_id != first.request.request_id

    def test_a_request_for_another_head_is_replaced(self, tmp_path) -> None:
        """headが動けばrecordのbind先が変わるため、古いrequestは再提示しない。"""
        env = user_env(tmp_path)
        first = _issue(env)
        outcome = _issue(
            env, head_sha=NEW_HEAD, id_source=FakeIds("req2"), evidence_port=FakeEvidencePort(())
        )
        assert outcome.reissued is False
        assert outcome.request.expected_head_sha == NEW_HEAD
        assert outcome.request.request_id != first.request.request_id

    def test_a_request_from_an_earlier_instance_is_replaced(self, tmp_path) -> None:
        """chainが進んだ後の同種awaitingは別instanceであり、古いrequestを引き継がない。

        経路2の受理は未応答requestを残す契約（決定10）のため、その後にchainとstateが進んで
        同じawaitingへ再到達すると、instance照合が無ければ**前instanceの消費済みintentが
        次の入力を重複と判定して飲み込む**。
        """
        env = user_env(tmp_path)
        first = _issue(env)
        consumed = ConsumedIntent(
            intent_key=intent_key(
                run_id=RUN,
                awaiting=GATE,
                since_seq=first.request.since_seq,
                head_sha=HEAD,
                kind=RecordKind.GATE_QUESTION,
            ),
            binding="ud:github",
            route="github_comment",
        )
        _save(env, with_consumed_intent(_payload(env), consumed))
        # 経路2の受理でstateが進み、hostの回答recordでchainが伸びてからgateへ戻る
        advanced = user_env_records(env, (RecordKind.FINAL_REPORT, RecordKind.GATE_ANSWER))
        outcome = _issue(
            env,
            records_port=advanced,
            id_source=FakeIds("req2"),
            evidence_port=FakeEvidencePort(()),
        )
        assert outcome.reissued is False
        assert outcome.request.since_seq == 2
        assert read_consumed_intent(_payload(env)) is None

class TestBindingEcho:
    def test_accepts_a_matching_submit(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        outcome = _submit(env, _respond(env, issued, kind=RecordKind.GATE_QUESTION))
        assert isinstance(outcome, UserInputAccepted)
        assert outcome.machine_state.pending_record is not None
        assert outcome.transaction is not None

    @pytest.mark.parametrize(
        ("field", "value", "code"),
        [
            ("request_id", "other", "stale_request"),
            ("nonce", "other", "stale_request"),
            ("awaiting", "USER_INPUT_DECISION", "awaiting_mismatch"),
            ("expected_head_sha", NEW_HEAD, "head_mismatch"),
        ],
    )
    def test_mismatched_binding_stops(self, tmp_path, field: str, value: str, code: str) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        envelope = _respond(env, issued, kind=RecordKind.GATE_QUESTION, **{field: value})
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == code

    def test_submit_without_a_pending_request_stops(self, tmp_path) -> None:
        env = user_env(tmp_path)
        envelope = user_submit_payload(request_id="req-1", nonce="req-2", result_hash="rh")
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "stale_request"

    def test_submit_for_another_run_stops(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        envelope = _respond(env, issued, kind=RecordKind.GATE_QUESTION, run_id="run-9")
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "run_mismatch"


class TestEnvelopeClassification:
    """submit envelopeは`action_id` / `request_id`の排他で判別する（ADR-0018 決定5）。"""

    def test_an_envelope_with_neither_key_stops(self, tmp_path) -> None:
        env = user_env(tmp_path)
        outcome = _submit(env, {"schema_version": 1, "run_id": RUN})
        assert isinstance(outcome, EngineStopped) and outcome.code == "submit_unclassified"

    def test_an_envelope_with_both_keys_stops(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        envelope = _respond(env, issued, kind=RecordKind.GATE_QUESTION)
        envelope["action_id"] = "act-1"
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "submit_unclassified"

    def test_unreadable_bytes_stop(self, tmp_path) -> None:
        env = user_env(tmp_path)
        outcome = submit(b"{not json", **env.submit_kwargs())
        assert isinstance(outcome, EngineStopped) and outcome.code == "submit_invalid"

    def test_a_user_envelope_that_fails_schema_stops(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        envelope = _respond(env, issued, kind=RecordKind.GATE_QUESTION)
        del envelope["result_kind"]  # GATE以外でresult_kindを省略できない
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "submit_invalid"


class TestResultRules:
    def test_kind_outside_the_awaiting_stops(self, tmp_path) -> None:
        env = user_env(tmp_path, awaiting=DECISION)
        issued = _issue(env)
        envelope = _respond(env, issued, kind=RecordKind.MERGE_APPROVAL)
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "result_kind_not_allowed"

    def test_result_hash_mismatch_stops(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        envelope = _respond(env, issued, kind=RecordKind.GATE_QUESTION, result_hash="0" * 64)
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "result_hash_mismatch"

    def test_missing_result_stops(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        envelope = _respond(env, issued, kind=RecordKind.GATE_QUESTION)
        (env.run_dir / issued.request.result_path).unlink()
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "missing"

    def test_result_over_the_size_limit_stops(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        envelope = _respond(env, issued, kind=RecordKind.GATE_QUESTION)
        outcome = _submit(env, envelope, max_result_bytes=8)
        assert isinstance(outcome, EngineStopped) and outcome.code == "too_large"

    @pytest.mark.parametrize(
        "kind", [RecordKind.GATE_QUESTION, RecordKind.MERGE_APPROVAL], ids=lambda k: k.value
    )
    def test_record_targeting_another_head_stops(self, tmp_path, kind: RecordKind) -> None:
        """`target_head_sha`も`approved_head_sha`もrequestのheadでなければならない。"""
        env = user_env(tmp_path)
        issued = _issue(env)
        envelope = _respond(
            env, issued, kind=kind, payload=user_record_payload(kind, head=NEW_HEAD)
        )
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "record_head_mismatch"

    def test_a_transcribed_record_claiming_the_other_route_stops(self, tmp_path) -> None:
        """転記recordがGitHub直接comment由来を名乗ると受理主体の照合が狂う。"""
        env = user_env(tmp_path)
        issued = _issue(env)
        payload = user_record_payload(RecordKind.GATE_QUESTION, route="github_comment")
        envelope = _respond(env, issued, kind=RecordKind.GATE_QUESTION, payload=payload)
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "input_route_mismatch"

    def test_a_broken_chain_stops_before_issuing_a_transaction(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        envelope = _respond(env, issued, kind=RecordKind.GATE_QUESTION)
        violation = IntegrityEvidenceRef(
            binding=OpaqueBinding("iv:gap:run-1:2"),
            descriptor=OpaqueRef("gap"),
            head=OpaqueRef(HEAD),
        )
        outcome = _submit(
            env, envelope, records_port=FakeRecordSource(records=env.records, violations=(violation,))
        )
        assert isinstance(outcome, EngineStopped) and outcome.code == "chain_violation"


class TestAcceptedRecordPath:
    @pytest.mark.parametrize(
        ("awaiting", "kind"),
        [
            (GATE, RecordKind.GATE_QUESTION),
            (GATE, RecordKind.GATE_CHANGES),
            (GATE, RecordKind.MERGE_APPROVAL),
            (DECISION, RecordKind.USER_DECISION),
        ],
        ids=lambda value: getattr(value, "value", value),
    )
    def test_every_registered_kind_produces_a_record(
        self, tmp_path, awaiting: Awaiting, kind: RecordKind
    ) -> None:
        env = user_env(tmp_path, awaiting=awaiting)
        issued = _issue(env, evidence_port=FakeEvidencePort(()))
        outcome = _submit(env, _respond(env, issued, kind=kind))
        assert isinstance(outcome, UserInputAccepted)
        assert outcome.machine_state.pending_record == PendingRecord(
            kind=kind,
            binding=OpaqueBinding(outcome.transaction.binding),  # type: ignore[union-attr]
            source_state=outcome.machine_state.state,
        )

    @pytest.mark.parametrize("awaiting", [GATE, DECISION, PERMISSION], ids=lambda a: a.value)
    def test_cancel_is_accepted_from_every_wait(self, tmp_path, awaiting: Awaiting) -> None:
        """`USER_CANCEL`はawaiting不問（C-01のP-21）。"""
        env = user_env(tmp_path, awaiting=awaiting)
        issued = _issue(env, evidence_port=FakeEvidencePort(()))
        outcome = _submit(env, _respond(env, issued, kind=RecordKind.USER_CANCEL))
        assert isinstance(outcome, UserInputAccepted)
        assert outcome.machine_state.pending_record is not None

    def test_the_transcribed_body_names_the_input_route(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        outcome = _submit(env, _respond(env, issued, kind=RecordKind.GATE_QUESTION))
        assert isinstance(outcome, UserInputAccepted) and outcome.transaction is not None
        assert outcome.transaction.body.startswith("**User**（入力経路: host_transcript）")

    def test_the_request_is_consumed_and_the_intent_is_recorded(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        outcome = _submit(env, _respond(env, issued, kind=RecordKind.GATE_QUESTION))
        assert isinstance(outcome, UserInputAccepted)
        payload = _payload(env)
        assert read_user_request(payload) is None
        receipt = read_user_receipt(payload)
        assert receipt is not None and receipt.intent_key == outcome.receipt.intent_key
        section = payload["user_request"]
        assert isinstance(section, dict)
        assert section["consumed"] == {
            "intent_key": outcome.receipt.intent_key,
            "binding": outcome.transaction.binding,  # type: ignore[union-attr]
            "route": "host_transcript",
        }
        assert "transaction" in payload


class TestIdempotency:
    def test_the_same_submit_is_replayed(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        envelope = _respond(env, issued, kind=RecordKind.GATE_QUESTION)
        first = _submit(env, envelope)
        assert isinstance(first, UserInputAccepted)
        again = _submit(env, envelope)
        assert isinstance(again, UserInputReplayed) and again.receipt == first.receipt

    def test_a_different_submit_for_the_same_request_stops(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        assert isinstance(_submit(env, _respond(env, issued, kind=RecordKind.GATE_QUESTION)), UserInputAccepted)
        changed = _respond(env, issued, kind=RecordKind.GATE_CHANGES)
        outcome = _submit(env, changed)
        assert isinstance(outcome, EngineStopped) and outcome.code == "duplicate_mismatch"

    def test_a_submit_while_a_record_is_pending_stops(self, tmp_path) -> None:
        """永続化を待つrecordがある間は新しい入力を受理しない。"""
        env = user_env(tmp_path)
        issued = _issue(env)
        envelope = _respond(env, issued, kind=RecordKind.GATE_QUESTION)
        _restate(
            env,
            MachineState(
                state=State.READY_FOR_HUMAN_MERGE,
                awaiting=GATE,
                pending_record=PendingRecord(
                    kind=RecordKind.USER_CANCEL,
                    binding=OpaqueBinding("cr:run-1:00000002:x"),
                    source_state=State.READY_FOR_HUMAN_MERGE,
                ),
            ),
        )
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "persist_required"

    def test_a_submit_after_another_route_consumed_the_wait_stops(self, tmp_path) -> None:
        """C-01が既にこの待機を消費していれば、転記submitを受理しない。"""
        env = user_env(tmp_path)
        issued = _issue(env)
        envelope = _respond(env, issued, kind=RecordKind.GATE_QUESTION)
        _restate(env, MachineState(state=State.READY_FOR_HUMAN_MERGE))
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "request_superseded"


class TestPermissionResume:
    def test_a_matching_resume_advances_without_a_record(self, tmp_path) -> None:
        env = user_env(tmp_path, awaiting=PERMISSION)
        issued = _issue(env, evidence_port=FakeEvidencePort(()))
        outcome = _submit(env, _respond(env, issued, permission=True))
        assert isinstance(outcome, UserInputAccepted)
        assert outcome.transaction is None
        assert outcome.machine_state.state is State.RUNNING_REVIEW
        assert outcome.receipt.intent_key is None
        assert "transaction" not in _payload(env)

    @pytest.mark.parametrize(
        "override",
        [
            {"permission_id": "other"},
            {"tool": "Bash(rm -rf)"},
            {"scope": "push twice"},
            {"current_head_sha": NEW_HEAD},
        ],
        ids=["permission_id", "tool", "scope", "head"],
    )
    def test_a_resume_that_does_not_match_the_stop_point_stops(self, tmp_path, override) -> None:
        env = user_env(tmp_path, awaiting=PERMISSION)
        issued = _issue(env, evidence_port=FakeEvidencePort(()))
        envelope = _respond(env, issued, permission=True, payload=permission_resume_payload(**override))
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "permission_resume_rejected"

    def test_a_missing_permission_section_stops(self, tmp_path) -> None:
        env = user_env(tmp_path, awaiting=PERMISSION)
        issued = _issue(env, evidence_port=FakeEvidencePort(()))
        envelope = _respond(env, issued, permission=True)
        from claude_code_codex_review_loop.state import checkpoint_path

        payload = _payload(env)
        del payload["permission"]
        save_checkpoint(checkpoint_path(env.paths, RUN), payload)
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "permission_unavailable"

    def test_a_resume_after_the_wait_was_consumed_stops(self, tmp_path) -> None:
        """別経路でstateが進んでいれば、record無しの応答も受理しない。"""
        env = user_env(tmp_path, awaiting=PERMISSION)
        issued = _issue(env, evidence_port=FakeEvidencePort(()))
        envelope = _respond(env, issued, permission=True)
        _restate(env, MachineState(state=State.RUNNING_REVIEW, awaiting=Awaiting.CODEX_CODE_REVIEW))
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "request_superseded"

    def test_a_resume_result_hash_mismatch_stops(self, tmp_path) -> None:
        env = user_env(tmp_path, awaiting=PERMISSION)
        issued = _issue(env, evidence_port=FakeEvidencePort(()))
        envelope = _respond(env, issued, permission=True, result_hash="0" * 64)
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "result_hash_mismatch"

    def test_an_incomplete_permission_section_stops(self, tmp_path) -> None:
        """停止点の値が欠けていれば、何にでも一致するresumeを作らない（fail closed）。"""
        env = user_env(tmp_path, awaiting=PERMISSION)
        issued = _issue(env, evidence_port=FakeEvidencePort(()))
        envelope = _respond(env, issued, permission=True)
        payload = _payload(env)
        section = payload["permission"]
        assert isinstance(section, dict)
        del section["blocked_tool"]
        save_checkpoint(checkpoint_path(env.paths, RUN), payload)
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "permission_unavailable"

    def test_a_resume_result_that_fails_schema_stops(self, tmp_path) -> None:
        env = user_env(tmp_path, awaiting=PERMISSION)
        issued = _issue(env, evidence_port=FakeEvidencePort(()))
        broken = permission_resume_payload()
        del broken["tool"]
        envelope = _respond(env, issued, permission=True, payload=broken)
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "schema_invalid"


class TestUnreadableSection:
    """`user_request`を解釈できない場合は「無い」へ丸めず停止する。"""

    def _break(self, env) -> None:
        """schemaは通るがreaderが受理しない値へ差し替える（負のsince_seq）。"""
        payload = _payload(env)
        section = payload["user_request"]
        assert isinstance(section, dict)
        section["pending"]["since_seq"] = -1
        save_checkpoint(checkpoint_path(env.paths, RUN), payload)

    def test_advance_stops(self, tmp_path) -> None:
        env = user_env(tmp_path)
        _issue(env)
        self._break(env)
        outcome = advance(**env.advance_kwargs())
        assert isinstance(outcome, EngineStopped) and outcome.code == "user_request_unavailable"

    def test_submit_stops(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        envelope = _respond(env, issued, kind=RecordKind.GATE_QUESTION)
        self._break(env)
        outcome = _submit(env, envelope)
        assert isinstance(outcome, EngineStopped) and outcome.code == "user_request_unavailable"


class TestRequestWrite:
    def test_an_invalid_envelope_is_not_written(self, tmp_path) -> None:
        """組み立てたenvelopeがschemaを通らなければ、保存もcheckpoint更新もしない。"""
        env = user_env(tmp_path)
        outcome = advance(
            **env.advance_kwargs(head_sha="", evidence_port=FakeEvidencePort(()))
        )
        assert isinstance(outcome, EngineStopped) and outcome.code == "request_invalid"
        assert read_user_request(_payload(env)) is None

    def test_a_colliding_request_id_stops(self, tmp_path) -> None:
        """同じrequest IDのenvelopeが既にあれば上書きせず停止する。"""
        env = user_env(tmp_path)
        issued = _issue(env)
        _restate(env, user_machine_state(GATE))  # requestを消さずstateだけ戻す
        payload = _payload(env)
        del payload["user_request"]
        save_checkpoint(checkpoint_path(env.paths, RUN), payload)
        assert issued.envelope_path.is_file()
        outcome = advance(**env.advance_kwargs())
        assert isinstance(outcome, EngineStopped) and outcome.code == "request_write"

    def test_a_corrupted_saved_envelope_stops(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issue(env)
        issued.envelope_path.write_text("{}", encoding="utf-8")
        outcome = advance(**env.advance_kwargs())
        assert isinstance(outcome, EngineStopped) and outcome.code == "request_invalid"


def test_max_result_bytes_is_a_parameter() -> None:
    """既定値はengineが持たない（解決はC-12）。"""
    assert MAX_RESULT_BYTES > 0
