# SPDX-License-Identifier: Apache-2.0
"""`advance`の受入test（Phase 8。AC-C08-07 / ADR-0014 決定22 / ADR-0015）。

1回のadvanceで1 actionだけを返し、未完了actionがある間は**新しいactionを生成しない**
ことを固定する。`HOST_ACTION`へ検証済みrecordのcomment IDと対象head SHAを含めること
（AC-C08-07）も、ここで検証する。
"""

from __future__ import annotations

import json

from c08_support.helpers import (
    HEAD,
    ISSUED_AT,
    NUMBER,
    REPOSITORY,
    RUN,
    FakeEvidencePort,
    FakeIds,
    FakePayloadPort,
    machine_state,
    review_records,
    seed,
)

from claude_code_codex_review_loop.domain.values import (
    Awaiting,
    MachineState,
    OpaqueBinding,
    PendingRecord,
    RecordKind,
    State,
)
from claude_code_codex_review_loop.schema import REGISTRY, SchemaKind, validate_object
from claude_code_codex_review_loop.schema.projection import canonical_payload_hash
from claude_code_codex_review_loop.state import CheckpointLoaded, load_checkpoint, save_checkpoint
from claude_code_codex_review_loop.workflow import (
    AwaitUser,
    EngineStopped,
    HostActionIssued,
    PersistRequired,
    Terminal,
    advance,
    read_pending_action,
)


def _break_attempt(env) -> None:
    """checkpointのattemptを0にする（schemaは通るが1始まりの制約に反する値）。"""
    loaded = load_checkpoint(env.checkpoint)
    assert isinstance(loaded, CheckpointLoaded)
    payload = dict(loaded.payload)
    section = dict(payload["host_action"])
    section["pending"] = {**section["pending"], "attempt": 0}
    payload["host_action"] = section
    save_checkpoint(env.checkpoint, payload)


def _advance(env, *, ids=None, evidence=None, payload=None, head=HEAD, run_id=RUN, number=NUMBER):
    return advance(
        paths=env.paths,
        run_id=run_id,
        repository=REPOSITORY,
        number=number,
        head_sha=head,
        payload_port=payload if payload is not None else FakePayloadPort(),
        evidence_port=FakeEvidencePort(review_records() if evidence is None else evidence),
        id_source=ids if ids is not None else FakeIds(),
        issued_at=ISSUED_AT,
    )


class TestIssue:
    def test_returns_the_action_for_the_awaiting(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        outcome = _advance(env)
        assert isinstance(outcome, HostActionIssued)
        assert outcome.envelope["action_kind"] == "APPLY_FINDINGS"
        assert outcome.reissued is False

    def test_envelope_passes_the_schema(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        outcome = _advance(env)
        assert isinstance(outcome, HostActionIssued)
        assert validate_object(REGISTRY[SchemaKind.HOST_ACTION], dict(outcome.envelope)).ok

    def test_envelope_binds_run_target_and_head(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        outcome = _advance(env)
        assert isinstance(outcome, HostActionIssued)
        envelope = outcome.envelope
        assert (envelope["run_id"], envelope["repository"], envelope["number"]) == (RUN, REPOSITORY, NUMBER)
        assert envelope["expected_head_sha"] == HEAD
        assert envelope["payload_hash"] == canonical_payload_hash(dict(envelope["payload"]))

    def test_verified_records_carry_comment_id_and_head(self, tmp_path) -> None:
        """AC-C08-07: 検証済みrecordのcomment IDと対象head SHAを渡す。"""
        env = seed(tmp_path, state=machine_state())
        records = review_records()
        outcome = _advance(env, evidence=records)
        assert isinstance(outcome, HostActionIssued)
        assert outcome.envelope["verified_records"] == [
            {"comment_id": records[0].comment_id, "head_sha": records[0].head_sha}
        ]

    def test_result_path_is_issued_inside_the_run_directory(self, tmp_path) -> None:
        """result pathはControllerが払い出す（呼び出し側の任意pathを受理しない）。"""
        env = seed(tmp_path, state=machine_state())
        outcome = _advance(env)
        assert isinstance(outcome, HostActionIssued)
        assert outcome.result_path.is_relative_to(env.run_dir)
        assert outcome.envelope["result_path"] == outcome.action.result_path

    def test_envelope_is_written_before_returning(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        outcome = _advance(env)
        assert isinstance(outcome, HostActionIssued)
        stored = json.loads(outcome.envelope_path.read_text(encoding="utf-8"))
        assert stored == outcome.envelope
        assert canonical_payload_hash(stored) == outcome.action.envelope_hash

    def test_checkpoint_holds_the_pending_action(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        outcome = _advance(env)
        assert isinstance(outcome, HostActionIssued)
        loaded = load_checkpoint(env.checkpoint)
        assert isinstance(loaded, CheckpointLoaded)
        assert read_pending_action(loaded.payload) == outcome.action

    def test_first_attempt_has_its_own_correlation_id(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        ids = FakeIds()
        outcome = _advance(env, ids=ids)
        assert isinstance(outcome, HostActionIssued)
        assert (outcome.action.attempt, outcome.action.correlation_id) == (1, "id-3")
        assert outcome.action.action_id != outcome.action.nonce

    def test_payload_port_receives_the_action_context(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        port = FakePayloadPort()
        _advance(env, payload=port)
        context = port.calls[0]
        assert (context.run_id, context.repository, context.number) == (RUN, REPOSITORY, NUMBER)
        assert context.head_sha == HEAD and context.action.value == "APPLY_FINDINGS"


class TestReissue:
    def test_pending_action_is_presented_unchanged(self, tmp_path) -> None:
        """ADR-0014 決定22: 中断後は同じaction ID / nonce / result pathを返す。"""
        env = seed(tmp_path, state=machine_state())
        first = _advance(env)
        assert isinstance(first, HostActionIssued)
        again = _advance(env, ids=FakeIds("other"))
        assert isinstance(again, HostActionIssued)
        assert again.action == first.action
        assert again.envelope == first.envelope
        assert again.reissued is True

    def test_missing_envelope_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        first = _advance(env)
        assert isinstance(first, HostActionIssued)
        first.envelope_path.unlink()
        assert _advance(env) == EngineStopped(
            "envelope_missing", f"action envelopeが無い: {first.action.envelope_path}"
        )

    def test_tampered_envelope_stops(self, tmp_path) -> None:
        """hashが一致しないenvelopeは再提示しない（内容の入れ替えを受理しない）。"""
        env = seed(tmp_path, state=machine_state())
        first = _advance(env)
        assert isinstance(first, HostActionIssued)
        envelope = dict(first.envelope)
        envelope["payload"] = {"round": 99, "finding_ids": ["F-9"]}
        first.envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
        outcome = _advance(env)
        assert isinstance(outcome, EngineStopped) and outcome.code == "envelope_mismatch"

    def test_unreadable_envelope_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        first = _advance(env)
        assert isinstance(first, HostActionIssued)
        first.envelope_path.write_text('{"schema_version": 2}', encoding="utf-8")
        outcome = _advance(env)
        assert isinstance(outcome, EngineStopped) and outcome.code == "envelope_invalid"


class TestRetryGate:
    """receiptがある未完了actionは、**retryできる失敗の場合だけ**次のattemptを発行する。"""

    def _with_receipt(self, env, action, *, outcome: str, category=None, kind=None):
        from claude_code_codex_review_loop.workflow import SubmitReceipt, with_receipt

        loaded = load_checkpoint(env.checkpoint)
        assert isinstance(loaded, CheckpointLoaded)
        receipt = SubmitReceipt(
            action_id=action.action_id,
            nonce=action.nonce,
            outcome=outcome,
            submit_hash="s" * 64,
            result_hash="r" * 64,
            result_kind=kind,
            error_category=category,
        )
        save_checkpoint(env.checkpoint, with_receipt(loaded.payload, receipt))

    def test_completed_receipt_does_not_retry(self, tmp_path) -> None:
        """v1 -> v2 migrationはCOMPLETED receiptを持つ未完了actionを作り得る。"""
        env = seed(tmp_path, state=machine_state())
        issued = _advance(env)
        assert isinstance(issued, HostActionIssued)
        self._with_receipt(env, issued.action, outcome="COMPLETED", kind=RecordKind.FIX_RESULT)
        outcome = _advance(env)
        assert isinstance(outcome, EngineStopped) and outcome.code == "attempt_not_retryable"

    def test_permanent_failure_receipt_does_not_retry(self, tmp_path) -> None:
        from claude_code_codex_review_loop.errors import ErrorCategory

        env = seed(tmp_path, state=machine_state())
        issued = _advance(env)
        assert isinstance(issued, HostActionIssued)
        self._with_receipt(env, issued.action, outcome="FAILED", category=ErrorCategory.PERMANENT)
        outcome = _advance(env)
        assert isinstance(outcome, EngineStopped) and outcome.code == "attempt_not_retryable"

    def test_transient_failure_receipt_retries(self, tmp_path) -> None:
        from claude_code_codex_review_loop.errors import ErrorCategory

        env = seed(tmp_path, state=machine_state())
        issued = _advance(env)
        assert isinstance(issued, HostActionIssued)
        self._with_receipt(env, issued.action, outcome="FAILED", category=ErrorCategory.TRANSIENT)
        outcome = _advance(env, ids=FakeIds("retry"))
        assert isinstance(outcome, HostActionIssued) and outcome.action.attempt == 2


class TestMigratedCheckpoint:
    def test_v1_completed_action_is_not_re_executed(self, tmp_path) -> None:
        """v1 -> v2 migrationはCOMPLETED receiptつきのpendingを作る。再実行しない。"""
        from claude_code_codex_review_loop.workflow import with_machine_state

        env = seed(tmp_path, state=machine_state())
        v1 = with_machine_state(
            {
                "schema_version": 1,
                "run_id": RUN,
                "repository": REPOSITORY,
                "number": NUMBER,
                "host_action": {
                    "action_id": "act-v1",
                    "action_kind": "APPLY_FINDINGS",
                    "nonce": "nonce-v1",
                    "expected_head_sha": HEAD,
                    "result_path": "actions/act-v1/result.json",
                    "envelope_path": "actions/act-v1/action.json",
                    "envelope_hash": "e" * 64,
                    "submit": {
                        "outcome": "COMPLETED",
                        "submit_hash": "s" * 64,
                        "result_hash": "r" * 64,
                        "result_kind": "FIX_RESULT",
                    },
                },
            },
            machine_state(),
        )
        save_checkpoint(env.checkpoint, v1)
        outcome = _advance(env)
        assert isinstance(outcome, EngineStopped) and outcome.code == "attempt_not_retryable"


class TestLedgerScope:
    def test_new_logical_action_drops_the_previous_ledger(self, tmp_path) -> None:
        """logical actionを跨いでreceiptが累積しない（ADR-0015 決定21）。"""
        from claude_code_codex_review_loop.workflow import read_receipts

        env = seed(tmp_path, state=machine_state())
        first = _advance(env)
        assert isinstance(first, HostActionIssued)
        TestRetryGate()._with_receipt(
            env, first.action, outcome="COMPLETED", kind=RecordKind.FIX_RESULT
        )
        # actionを完了させた状態（pendingなし・receiptあり）から次のlogical actionを発行する
        loaded = load_checkpoint(env.checkpoint)
        assert isinstance(loaded, CheckpointLoaded)
        from claude_code_codex_review_loop.workflow import without_pending_action

        save_checkpoint(env.checkpoint, without_pending_action(loaded.payload))
        second = _advance(env, ids=FakeIds("next"))
        assert isinstance(second, HostActionIssued)
        after = load_checkpoint(env.checkpoint)
        assert isinstance(after, CheckpointLoaded)
        assert read_receipts(after.payload) == ()

    def test_retry_keeps_the_ledger(self, tmp_path) -> None:
        from claude_code_codex_review_loop.errors import ErrorCategory
        from claude_code_codex_review_loop.workflow import read_receipts

        env = seed(tmp_path, state=machine_state())
        first = _advance(env)
        assert isinstance(first, HostActionIssued)
        TestRetryGate()._with_receipt(
            env, first.action, outcome="FAILED", category=ErrorCategory.TRANSIENT
        )
        _advance(env, ids=FakeIds("retry"))
        after = load_checkpoint(env.checkpoint)
        assert isinstance(after, CheckpointLoaded)
        receipts = read_receipts(after.payload)
        assert isinstance(receipts, tuple) and len(receipts) == 1


class TestNonActionOutcomes:
    def test_terminal_state_issues_nothing(self, tmp_path) -> None:
        env = seed(tmp_path, state=MachineState(state=State.MERGED))
        assert _advance(env) == Terminal(state=State.MERGED)

    def test_pending_record_requires_persistence(self, tmp_path) -> None:
        """`RecordProduced`後は永続化が先（実行は後続PRのPersistRecord）。"""
        record = PendingRecord(
            kind=RecordKind.FIX_RESULT,
            binding=OpaqueBinding("cr:run-1:00000002:x"),
            source_state=State.APPLYING_FIXES,
        )
        env = seed(tmp_path, state=MachineState(state=State.APPLYING_FIXES, pending_record=record))
        assert _advance(env) == PersistRequired(record=record)

    def test_user_input_awaiting_returns_await_user(self, tmp_path) -> None:
        env = seed(
            tmp_path,
            state=MachineState(state=State.READY_FOR_HUMAN_MERGE, awaiting=Awaiting.USER_INPUT_GATE),
        )
        assert _advance(env) == AwaitUser(awaiting=Awaiting.USER_INPUT_GATE)

    def test_non_host_awaiting_stops(self, tmp_path) -> None:
        env = seed(
            tmp_path,
            state=MachineState(state=State.RUNNING_REVIEW, awaiting=Awaiting.CODEX_CODE_REVIEW),
        )
        outcome = _advance(env)
        assert isinstance(outcome, EngineStopped) and outcome.code == "not_host_action"

    def test_state_without_awaiting_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=MachineState(state=State.WAITING_CI))
        outcome = _advance(env)
        assert isinstance(outcome, EngineStopped) and outcome.code == "no_awaiting"


class TestRefusals:
    def test_missing_checkpoint_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        env.checkpoint.unlink()
        outcome = _advance(env)
        assert isinstance(outcome, EngineStopped) and outcome.code == "checkpoint_unavailable"

    def test_checkpoint_for_another_run_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        loaded = load_checkpoint(env.checkpoint)
        assert isinstance(loaded, CheckpointLoaded)
        payload = dict(loaded.payload)
        payload["run_id"] = "run-other"
        save_checkpoint(env.checkpoint, payload)
        outcome = _advance(env)
        assert isinstance(outcome, EngineStopped) and outcome.code == "run_mismatch"

    def test_checkpoint_for_another_target_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        outcome = _advance(env, number=NUMBER + 1)
        assert isinstance(outcome, EngineStopped) and outcome.code == "target_mismatch"

    def test_checkpoint_without_state_stops(self, tmp_path) -> None:
        env = seed(tmp_path)
        outcome = _advance(env)
        assert isinstance(outcome, EngineStopped) and outcome.code == "state_unavailable"

    def test_invalid_action_payload_stops(self, tmp_path) -> None:
        """portが供給したpayloadがschemaを通らなければ、actionを発行しない。"""
        env = seed(tmp_path, state=machine_state())
        outcome = _advance(env, payload=FakePayloadPort(payload={"round": "first"}))
        assert isinstance(outcome, EngineStopped) and outcome.code == "envelope_invalid"

    def test_evidence_of_a_disallowed_kind_stops(self, tmp_path) -> None:
        """根拠に使えないrecord種別は同梱しない（actionごとの選択表を守る）。"""
        from c07_support.helpers import verified_chain

        env = seed(tmp_path, state=machine_state())
        outcome = _advance(env, evidence=verified_chain([RecordKind.GATE_QUESTION]).records)
        assert isinstance(outcome, EngineStopped) and outcome.code == "evidence_kind"

    def test_unwritable_action_directory_stops(self, tmp_path) -> None:
        """envelopeを保存できなければactionを返さない（保存 -> checkpoint -> 返却の順）。"""
        env = seed(tmp_path, state=machine_state())
        (env.run_dir / "actions").write_text("not a directory", encoding="utf-8")
        outcome = _advance(env)
        assert isinstance(outcome, EngineStopped) and outcome.code == "envelope_write"

    def test_reused_action_id_stops(self, tmp_path) -> None:
        """注入されたid_sourceがIDを再生成しても、既存envelopeを上書きしない（fail closed）。

        `id_source`は呼び出し側から注入するため、engineはその一意性を前提にできない。
        既存のaction directoryへ書き込めないことで、古いresult fileを新しいactionの
        結果として受理する経路を塞ぐ。
        """
        from claude_code_codex_review_loop.workflow import without_pending_action

        env = seed(tmp_path, state=machine_state())
        first = _advance(env, ids=FakeIds())
        assert isinstance(first, HostActionIssued)
        loaded = load_checkpoint(env.checkpoint)
        assert isinstance(loaded, CheckpointLoaded)
        save_checkpoint(env.checkpoint, without_pending_action(loaded.payload))
        outcome = _advance(env, ids=FakeIds())
        assert isinstance(outcome, EngineStopped) and outcome.code == "envelope_write"

    def test_unreadable_pending_action_stops(self, tmp_path) -> None:
        """schemaは通るが解釈できないpending actionを「無い」へ丸めない。"""
        env = seed(tmp_path, state=machine_state())
        issued = _advance(env)
        assert isinstance(issued, HostActionIssued)
        _break_attempt(env)
        outcome = _advance(env)
        assert isinstance(outcome, EngineStopped) and outcome.code == "host_action_unavailable"

    def test_evidence_for_another_head_stops(self, tmp_path) -> None:
        """AC-C08-07: 根拠recordは、そのactionが束ねられたheadを対象にしていなければならない。"""
        from c07_support.helpers import verified_chain
        from c08_support.helpers import NEW_HEAD

        env = seed(tmp_path, state=machine_state())
        other = verified_chain([RecordKind.REVIEW_RESULT], head=NEW_HEAD).records
        outcome = _advance(env, evidence=other)
        assert isinstance(outcome, EngineStopped) and outcome.code == "evidence_head"

    def test_evidence_out_of_order_stops(self, tmp_path) -> None:
        from c07_support.helpers import verified_chain

        env = seed(tmp_path, state=machine_state())
        records = verified_chain([RecordKind.REVIEW_RESULT, RecordKind.CLARIFICATION_ANSWER]).records
        outcome = _advance(env, evidence=tuple(reversed(records)))
        assert isinstance(outcome, EngineStopped) and outcome.code == "evidence_order"
