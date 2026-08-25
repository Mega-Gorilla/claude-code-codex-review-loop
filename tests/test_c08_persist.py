# SPDX-License-Identifier: Apache-2.0
"""`PersistRecord`実行の受入test（Phase 8。ADR-0017）。

C-01が`pending_record`へ置いたrecordを投稿し、**C-06の検証を通ってから**`*Verified`
eventでstateを進めることを固定する。投稿と検証は製品経路（`ensure_comment_posted` ->
`verify_record_chain`）を通し、fake gh越しに実行する（実GitHubへは接続しない）。
"""

from __future__ import annotations

from c05_support.helpers import make_policy
from c06_support.helpers import HEAD
from c07_support.helpers import RUN, verified_chain
from c08_support.helpers import (
    FakeRecordEvents,
    FakeRecordSource,
    persist_env,
)

from claude_code_codex_review_loop.domain import events as ev
from claude_code_codex_review_loop.domain.values import (
    HaltingForBlockProcedure,
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
    EngineStopped,
    IntegrityDetected,
    PersistFailed,
    RecordPersisted,
    persist,
    read_machine_state,
    with_machine_state,
)


def _payload(env) -> dict[str, object]:
    loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
    assert isinstance(loaded, CheckpointLoaded)
    return loaded.payload


def _violation(name: str = "iv:tamper:run-1:c1") -> IntegrityEvidenceRef:
    return IntegrityEvidenceRef(
        binding=OpaqueBinding(name), descriptor=OpaqueRef("desc"), head=OpaqueRef(HEAD)
    )


class TestPersist:
    def test_posts_verifies_and_advances(self, tmp_path) -> None:
        env = persist_env(tmp_path)
        outcome = persist(**env.kwargs())
        assert isinstance(outcome, RecordPersisted)
        assert outcome.posted is True
        assert outcome.record.key == env.issued.binding
        assert outcome.machine_state.pending_record is None

    def test_record_reaches_github_once(self, tmp_path) -> None:
        env = persist_env(tmp_path)
        assert env.comment_count() == 1  # 先行するREVIEW_RESULTのみ
        persist(**env.kwargs())
        assert env.comment_count() == 2

    def test_transaction_is_consumed(self, tmp_path) -> None:
        """投稿と検証が終わるまでtransactionを消さない（消した時点で再発行できなくなる）。"""
        env = persist_env(tmp_path)
        assert "transaction" in _payload(env)
        persist(**env.kwargs())
        assert "transaction" not in _payload(env)

    def test_verified_body_matches_the_transaction(self, tmp_path) -> None:
        env = persist_env(tmp_path)
        outcome = persist(**env.kwargs())
        assert isinstance(outcome, RecordPersisted)
        assert outcome.record.body_hash == env.issued.body_hash

    def test_event_comes_from_the_port(self, tmp_path) -> None:
        """record -> eventの写像はport（C-10 / C-11）が持つ。"""
        env = persist_env(tmp_path)
        outcome = persist(**env.kwargs())
        assert isinstance(outcome, RecordPersisted)
        assert outcome.machine_state.state is State.READY_FOR_HUMAN_MERGE

    def test_already_posted_record_is_not_reposted(self, tmp_path) -> None:
        """確認後・checkpoint前で中断した場合、再実行しても投稿しない。"""
        env = persist_env(tmp_path)
        persist(**env.kwargs())
        before = env.comment_count()
        # transactionとpending recordを復元して再実行する（消費直前の中断を模す）
        save_checkpoint(
            checkpoint_path(env.paths, RUN),
            _restored(env),
        )
        outcome = persist(**env.kwargs())
        assert isinstance(outcome, RecordPersisted)
        assert outcome.posted is False
        assert env.comment_count() == before


def _restore(env) -> None:
    """persist直前のcheckpointへ戻す。"""
    save_checkpoint(checkpoint_path(env.paths, RUN), _restored(env))


def _restored(env) -> dict[str, object]:
    """persist直前のcheckpoint（transactionとpending recordを持つ状態）を作り直す。"""
    from c08_support.helpers import checkpoint_payload

    from claude_code_codex_review_loop.workflow import transaction_section

    state = MachineState(
        state=State.READY_FOR_HUMAN_MERGE,
        pending_record=PendingRecord(
            kind=RecordKind.GATE_ANSWER,
            binding=OpaqueBinding(env.issued.binding),
            source_state=State.READY_FOR_HUMAN_MERGE,
        ),
    )
    payload = with_machine_state(checkpoint_payload(), state)
    payload["transaction"] = transaction_section(env.issued)
    return payload


class TestRefusals:
    def test_without_pending_record_stops(self, tmp_path) -> None:
        env = persist_env(tmp_path, state=MachineState(state=State.READY_FOR_HUMAN_MERGE))
        outcome = persist(**env.kwargs())
        assert isinstance(outcome, EngineStopped) and outcome.code == "no_pending_record"

    def test_without_transaction_stops(self, tmp_path) -> None:
        env = persist_env(tmp_path, with_transaction=False)
        outcome = persist(**env.kwargs())
        assert isinstance(outcome, EngineStopped) and outcome.code == "transaction_missing"

    def test_transaction_for_another_record_stops(self, tmp_path) -> None:
        env = persist_env(tmp_path)
        payload = _payload(env)
        state = MachineState(
            state=State.READY_FOR_HUMAN_MERGE,
            pending_record=PendingRecord(
                kind=RecordKind.GATE_ANSWER,
                binding=OpaqueBinding("cr:run-1:00000009:other"),
                source_state=State.READY_FOR_HUMAN_MERGE,
            ),
        )
        save_checkpoint(checkpoint_path(env.paths, RUN), with_machine_state(payload, state))
        outcome = persist(**env.kwargs())
        assert isinstance(outcome, EngineStopped) and outcome.code == "transaction_mismatch"

    def test_unreadable_transaction_stops(self, tmp_path) -> None:
        env = persist_env(tmp_path)
        payload = _payload(env)
        transaction = dict(payload["transaction"])  # type: ignore[arg-type]
        transaction["seq"] = 0
        payload["transaction"] = transaction
        save_checkpoint(checkpoint_path(env.paths, RUN), payload)
        outcome = persist(**env.kwargs())
        assert isinstance(outcome, EngineStopped) and outcome.code == "transaction_unavailable"

    def test_pending_that_cannot_be_decided_stops(self, tmp_path) -> None:
        """直前seqがchainに無い状態では、推測して投稿しない（C-07の判定に従う）。"""
        env = persist_env(tmp_path)
        outcome = persist(**env.kwargs(records_port=FakeRecordSource(records=())))
        assert isinstance(outcome, EngineStopped) and outcome.code == "pending_unavailable"

    def test_unverified_record_after_posting_stops(self, tmp_path) -> None:
        """投稿したのに検証済みchainへ現れない場合、推測で再投稿しない。"""
        env = persist_env(tmp_path)
        stale = FakeRecordSource(records=verified_chain([RecordKind.REVIEW_RESULT]).records)
        outcome = persist(**env.kwargs(records_port=stale))
        assert isinstance(outcome, EngineStopped) and outcome.code == "record_unverified"
        assert env.comment_count() == 2  # 投稿自体は行われている

    def test_event_that_the_state_rejects_stops(self, tmp_path) -> None:
        """portが返したeventをC-01が受理しない場合は停止する。"""
        env = persist_env(tmp_path)
        wrong = FakeRecordEvents(
            events={
                RecordKind.GATE_ANSWER: ev.CiSucceeded(),
            }
        )
        outcome = persist(**env.kwargs(event_port=wrong))
        assert isinstance(outcome, EngineStopped) and outcome.code == "illegal_event"

    def test_event_port_failure_stops(self, tmp_path) -> None:
        env = persist_env(tmp_path)

        class Broken:
            def event_for(self, evidence, record):  # type: ignore[no-untyped-def]
                from claude_code_codex_review_loop.workflow import ActionRegistryError

                raise ActionRegistryError("入力が宣言と一致しない")

        outcome = persist(**env.kwargs(event_port=Broken()))
        assert isinstance(outcome, EngineStopped) and outcome.code == "event_unavailable"


class TestBodyHashContract:
    def test_missing_body_hash_stops_before_posting(self, tmp_path) -> None:
        """完成本文hashが無いtransactionは、**投稿する前に**拒否する。

        schema上optionalなのは既存fieldの制約を強化しないためで（ADR-0013 決定9）、
        producerが省略してよい意味ではない（ADR-0014 決定21）。投稿してから検証で
        落とすと、外部commentだけが増えてtransactionが残る。
        """
        env = persist_env(tmp_path)
        payload = _payload(env)
        transaction = dict(payload["transaction"])  # type: ignore[arg-type]
        transaction.pop("body_hash")
        payload["transaction"] = transaction
        save_checkpoint(checkpoint_path(env.paths, RUN), payload)
        outcome = persist(**env.kwargs())
        assert isinstance(outcome, EngineStopped) and outcome.code == "body_hash_missing"
        assert env.comment_count() == 1  # 投稿していない
        assert "transaction" in _payload(env)


class _CancelEvents:
    """`UserCancelVerified`を返すport（cancel手続きへ入る）。"""

    def event_for(self, evidence, record):  # type: ignore[no-untyped-def]
        return ev.UserCancelVerified(evidence=evidence)


class TestStateThatCannotBePersisted:
    def test_event_leading_to_an_unrepresentable_state_stops(self, tmp_path) -> None:
        """C-01が返す状態をcheckpointが表現できない場合、保存せず停止する。

        `UserCancelVerified`はcancel手続きへ入るが、その付随値（`CancellingProcedure`）は
        まだcheckpointが表現しない。落として保存すると復元できないcheckpointになるため、
        表現できるようになるまでは停止する（ADR-0017）。
        """
        from c02_support.helpers import REPRESENTATIVE

        from claude_code_codex_review_loop.schema import SchemaKind

        payload = dict(REPRESENTATIVE[SchemaKind.USER_CANCEL])
        payload["target_head_sha"] = HEAD
        env = persist_env(tmp_path, kind=RecordKind.USER_CANCEL, payload=payload)
        state = MachineState(
            state=State.APPLYING_FIXES,
            pending_record=PendingRecord(
                kind=RecordKind.USER_CANCEL,
                binding=OpaqueBinding(env.issued.binding),
                source_state=State.APPLYING_FIXES,
            ),
        )
        save_checkpoint(checkpoint_path(env.paths, RUN), with_machine_state(_payload(env), state))
        outcome = persist(**env.kwargs(event_port=_CancelEvents()))
        assert isinstance(outcome, EngineStopped) and outcome.code == "state_not_persistable"


class TestUnreachableCheckpoint:
    def test_missing_checkpoint_stops(self, tmp_path) -> None:
        env = persist_env(tmp_path)
        checkpoint_path(env.paths, RUN).unlink()
        outcome = persist(**env.kwargs())
        assert isinstance(outcome, EngineStopped) and outcome.code == "checkpoint_unavailable"


class TestBodyMismatch:
    def test_verified_record_with_another_body_stops(self, tmp_path) -> None:
        """確認後に本文が改変された場合、eventを組み立てず停止する。

        投稿済み判定の時点ではC-07の`evaluate_pending`が本文一致を確認しているが、
        その後の再検証で本文が変わっていれば、そのrecordをevidenceにしない。
        """
        import dataclasses as dc

        env = persist_env(tmp_path)
        assert isinstance(persist(**env.kwargs()), RecordPersisted)
        _restore(env)

        class TamperedAfterCheck:
            """投稿済み判定の後に本文が改変されたchainを返す（確認と検証の間のrace）。"""

            def __init__(self, inner) -> None:  # type: ignore[no-untyped-def]
                self.inner = inner
                self.calls = 0

            def chain(self, run_id: str):  # type: ignore[no-untyped-def]
                self.calls += 1
                chain = self.inner.chain(run_id)
                if self.calls == 1:
                    return chain
                records = tuple(
                    dc.replace(record, body_hash="0" * 64)
                    if record.key == env.issued.binding
                    else record
                    for record in chain.records
                )
                return dc.replace(chain, records=records)

        base = env.kwargs()["records_port"]
        outcome = persist(**env.kwargs(records_port=TamperedAfterCheck(base)))
        assert isinstance(outcome, EngineStopped) and outcome.code == "record_body_mismatch"


class TestEventsTheStateRejects:
    """C-01が受理しない位置では、stateを動かさず停止する（fail closed）。"""

    def _merging(self, env) -> None:
        state = MachineState(
            state=State.MERGING,
            pending_record=PendingRecord(
                kind=RecordKind.GATE_ANSWER,
                binding=OpaqueBinding(env.issued.binding),
                source_state=State.MERGING,
            ),
        )
        save_checkpoint(
            checkpoint_path(env.paths, RUN), with_machine_state(_payload(env), state)
        )

    def test_violation_that_the_state_rejects_stops(self, tmp_path) -> None:
        env = persist_env(tmp_path)
        self._merging(env)
        broken = FakeRecordSource(
            records=verified_chain([RecordKind.REVIEW_RESULT]).records, violations=(_violation(),)
        )
        outcome = persist(**env.kwargs(records_port=broken))
        assert isinstance(outcome, EngineStopped) and outcome.code == "illegal_event"

    def test_run_failed_that_the_state_rejects_stops(self, tmp_path) -> None:
        env = persist_env(tmp_path, scenario="ok,s500,s500,s500,s500")
        self._merging(env)
        outcome = persist(**env.kwargs(policy=make_policy(max_attempts=1)))
        assert isinstance(outcome, EngineStopped) and outcome.code == "illegal_event"


class TestIntegrityRouting:
    def test_state_after_integrity_round_trips(self, tmp_path) -> None:
        """integrity遷移後のcheckpointを、そのまま読み戻せる（次のresumeが復元できる）。"""
        env = persist_env(tmp_path)
        broken = FakeRecordSource(
            records=verified_chain([RecordKind.REVIEW_RESULT]).records, violations=(_violation(),)
        )
        outcome = persist(**env.kwargs(records_port=broken))
        assert isinstance(outcome, IntegrityDetected)
        restored = read_machine_state(_payload(env))
        assert restored == outcome.machine_state

    def test_halt_gate_survives_the_checkpoint(self, tmp_path) -> None:
        """active stateではhalt gateが保存され、停止完了前に落ちても復元できる。"""
        env = persist_env(
            tmp_path, kind=RecordKind.DECISION_BRIEF, payload=_decision_brief_payload()
        )
        state = MachineState(
            state=State.REVIEWING_DECISION_REQUEST,
            pending_record=PendingRecord(
                kind=RecordKind.DECISION_BRIEF,
                binding=OpaqueBinding(env.issued.binding),
                source_state=State.REVIEWING_DECISION_REQUEST,
            ),
        )
        save_checkpoint(checkpoint_path(env.paths, RUN), with_machine_state(_payload(env), state))
        broken = FakeRecordSource(
            records=verified_chain([RecordKind.REVIEW_RESULT]).records, violations=(_violation(),)
        )
        outcome = persist(**env.kwargs(records_port=broken))
        assert isinstance(outcome, IntegrityDetected)
        restored = read_machine_state(_payload(env))
        assert isinstance(restored, MachineState)
        assert isinstance(restored.procedure, HaltingForBlockProcedure)
        assert restored == outcome.machine_state

    def test_multiple_violations_keep_the_halt_command(self, tmp_path) -> None:
        """複数violationでも、最初の検出が出した`HaltRun`を落とさない。"""
        env = persist_env(
            tmp_path, kind=RecordKind.DECISION_BRIEF, payload=_decision_brief_payload()
        )
        state = MachineState(
            state=State.REVIEWING_DECISION_REQUEST,
            pending_record=PendingRecord(
                kind=RecordKind.DECISION_BRIEF,
                binding=OpaqueBinding(env.issued.binding),
                source_state=State.REVIEWING_DECISION_REQUEST,
            ),
        )
        save_checkpoint(checkpoint_path(env.paths, RUN), with_machine_state(_payload(env), state))
        broken = FakeRecordSource(
            records=verified_chain([RecordKind.REVIEW_RESULT]).records,
            violations=(_violation("iv:marker:run-1:c1"), _violation("iv:actor:run-1:c2")),
        )
        outcome = persist(**env.kwargs(records_port=broken))
        assert isinstance(outcome, IntegrityDetected)
        names = [type(command).__name__ for command in outcome.commands]
        assert names.count("HaltRun") == 1
        assert names == ["InvalidateApprovals", "HaltRun", "InvalidateApprovals"]


class TestIntegrityAfterPosting:
    def test_violation_before_posting_skips_the_post(self, tmp_path) -> None:
        """壊れたchainの上では投稿しない。violationはC-01のintegrity経路へ渡す。"""
        env = persist_env(tmp_path)
        broken = FakeRecordSource(
            records=verified_chain([RecordKind.REVIEW_RESULT]).records, violations=(_violation(),)
        )
        outcome = persist(**env.kwargs(records_port=broken))
        assert isinstance(outcome, IntegrityDetected)
        assert outcome.violations == (_violation(),)
        assert env.comment_count() == 1  # 投稿していない

    def test_violation_after_posting_goes_to_c01(self, tmp_path) -> None:
        """投稿は成功したが検証でviolationが出た場合も、再投稿せずintegrity経路へ渡す。"""
        env = persist_env(tmp_path)
        healthy = verified_chain([RecordKind.REVIEW_RESULT]).records

        class LateViolation:
            def __init__(self) -> None:
                self.calls = 0

            def chain(self, run_id: str):  # type: ignore[no-untyped-def]
                self.calls += 1
                violations = () if self.calls == 1 else (_violation(),)
                return FakeRecordSource(records=healthy, violations=violations).chain(run_id)

        outcome = persist(**env.kwargs(records_port=LateViolation()))
        assert isinstance(outcome, IntegrityDetected)
        assert env.comment_count() == 2  # 投稿は行われた


class TestBoundedRetry:
    def test_exhausted_retry_fails_the_run(self, tmp_path) -> None:
        """bounded retryが尽きた投稿失敗は`RunFailed`としてC-01へ入力する。

        resumable stateでは同一stateに留まる（C-01のF-02）。stateをここで決めるのは
        C-01であり、C-08はeventを入力するだけである。
        """
        env = persist_env(tmp_path, scenario="ok,s500,s500,s500,s500")
        outcome = persist(**env.kwargs(policy=make_policy(max_attempts=1)))
        assert isinstance(outcome, PersistFailed)
        assert outcome.machine_state.state is State.READY_FOR_HUMAN_MERGE
        assert "bounded retry" in outcome.detail

    def test_exhausted_retry_in_an_active_state_goes_to_failed(self, tmp_path) -> None:
        """active stateではF-01で`FAILED`（`recovery_to`を保持）へ入る。"""
        env = persist_env(
            tmp_path, kind=RecordKind.DECISION_BRIEF, payload=_decision_brief_payload(),
            scenario="ok,s500,s500,s500,s500",
        )
        state = MachineState(
            state=State.REVIEWING_DECISION_REQUEST,
            pending_record=PendingRecord(
                kind=RecordKind.DECISION_BRIEF,
                binding=OpaqueBinding(env.issued.binding),
                source_state=State.REVIEWING_DECISION_REQUEST,
            ),
        )
        save_checkpoint(
            checkpoint_path(env.paths, RUN), with_machine_state(_payload(env), state)
        )
        outcome = persist(**env.kwargs(policy=make_policy(max_attempts=1)))
        assert isinstance(outcome, PersistFailed)
        assert outcome.machine_state.state is State.FAILED
        assert outcome.machine_state.recovery_to is State.REVIEWING_DECISION_REQUEST

    def test_failed_run_keeps_the_transaction(self, tmp_path) -> None:
        """投稿できていない以上、transactionは消さない（次のresumeが再発行する）。"""
        env = persist_env(tmp_path, scenario="ok,s500,s500,s500,s500")
        persist(**env.kwargs(policy=make_policy(max_attempts=1)))
        assert "transaction" in _payload(env)


class TestGenericBoundary:
    def test_any_pending_kind_uses_the_same_path(self, tmp_path) -> None:
        """host actionの結果に限らない。C-09以降のrecordも同じ経路を通る。"""
        env = persist_env(
            tmp_path,
            kind=RecordKind.DECISION_BRIEF,
            payload=_decision_brief_payload(),
            state=None,
        )
        state = MachineState(
            state=State.REVIEWING_DECISION_REQUEST,
            pending_record=PendingRecord(
                kind=RecordKind.DECISION_BRIEF,
                binding=OpaqueBinding(env.issued.binding),
                source_state=State.REVIEWING_DECISION_REQUEST,
            ),
        )
        payload = with_machine_state(_payload(env), state)
        save_checkpoint(checkpoint_path(env.paths, RUN), payload)
        outcome = persist(**env.kwargs())
        assert isinstance(outcome, RecordPersisted)
        assert outcome.record.kind is RecordKind.DECISION_BRIEF


def _decision_brief_payload() -> dict[str, object]:
    from c02_support.helpers import REPRESENTATIVE

    from claude_code_codex_review_loop.schema import SchemaKind

    payload = dict(REPRESENTATIVE[SchemaKind.DECISION_BRIEF])
    payload["target_head_sha"] = HEAD
    return payload
