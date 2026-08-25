# SPDX-License-Identifier: Apache-2.0
"""checkpoint sectionのreader / writerの受入test（Phase 8。ADR-0015）。

未完了actionとreceipt ledgerを、**解釈できない場合に「無い」へ丸めない**こと
（silent repair禁止）と、MachineStateを保存されている値だけから復元できることを固定する。
"""

from __future__ import annotations

import pytest
from c07_support.helpers import checkpoint_payload

from claude_code_codex_review_loop.domain.values import (
    Awaiting,
    HaltingForBlockProcedure,
    IntegrityEvidenceRef,
    MachineState,
    OpaqueBinding,
    OpaqueRef,
    PendingRecord,
    RecordIntegrityBlock,
    RecordKind,
    State,
)
from claude_code_codex_review_loop.errors import ErrorCategory
from claude_code_codex_review_loop.schema import REGISTRY, SchemaKind, validate_object
from claude_code_codex_review_loop.schema.envelope import MAX_SUBMIT_RECEIPTS
from claude_code_codex_review_loop.workflow import (
    PendingAction,
    SectionUnavailable,
    SubmitReceipt,
    find_receipt,
    next_attempt,
    read_machine_state,
    read_pending_action,
    read_receipts,
    with_machine_state,
    with_new_logical_action,
    with_receipt,
    with_retry_attempt,
    with_verified_machine_state,
    without_pending_action,
)

ACTION = PendingAction(
    action_id="act-1",
    action_kind="APPLY_FINDINGS",
    nonce="nonce-1",
    expected_head_sha="a" * 40,
    result_path="actions/act-1/result.json",
    envelope_path="actions/act-1/action.json",
    envelope_hash="e" * 64,
    correlation_id="corr-1",
    attempt=1,
    issued_at="2026-08-25T09:00:00Z",
)
RECEIPT = SubmitReceipt(
    action_id="act-1",
    nonce="nonce-1",
    outcome="FAILED",
    submit_hash="s" * 64,
    result_hash="r" * 64,
    error_category=ErrorCategory.TRANSIENT,
    accepted_at="2026-08-25T09:05:00Z",
)


def _valid(payload: dict[str, object]) -> bool:
    return validate_object(REGISTRY[SchemaKind.CHECKPOINT], dict(payload)).ok


class TestPendingAction:
    def test_round_trip(self) -> None:
        payload = with_new_logical_action(checkpoint_payload(), ACTION)
        assert _valid(payload)
        assert read_pending_action(payload) == ACTION

    def test_absent_section_is_none(self) -> None:
        assert read_pending_action(checkpoint_payload()) is None

    def test_section_without_pending_is_none(self) -> None:
        payload = without_pending_action(with_new_logical_action(checkpoint_payload(), ACTION))
        assert read_pending_action(payload) is None

    def test_retry_attempt_round_trips(self) -> None:
        import dataclasses

        following = dataclasses.replace(ACTION, action_id="act-2", nonce="nonce-2", attempt=2)
        payload = with_retry_attempt(with_new_logical_action(checkpoint_payload(), ACTION), following)
        assert _valid(payload) and read_pending_action(payload) == following

    def test_issued_at_is_optional(self) -> None:
        import dataclasses

        action = dataclasses.replace(ACTION, issued_at=None)
        payload = with_new_logical_action(checkpoint_payload(), action)
        assert _valid(payload) and read_pending_action(payload) == action

    def test_absent_correlation_defaults_to_the_action_id(self) -> None:
        """v1から移行したcheckpointは単一attemptのlogical actionを意味する。"""
        payload = checkpoint_payload(
            host_action={"pending": {k: v for k, v in _pending_dict().items() if k != "correlation_id"}}
        )
        action = read_pending_action(payload)
        assert isinstance(action, PendingAction)
        assert (action.correlation_id, action.attempt) == (ACTION.action_id, 1)

    @pytest.mark.parametrize(
        "section",
        ["text", {"pending": "text"}, {"pending": {}}, {"pending": {"action_id": ""}}],
        ids=["not_object", "pending_not_object", "empty", "blank_id"],
    )
    def test_unreadable_section_is_reported(self, section: object) -> None:
        outcome = read_pending_action(checkpoint_payload(host_action=section))
        assert isinstance(outcome, SectionUnavailable)

    def test_non_positive_attempt_is_reported(self) -> None:
        payload = checkpoint_payload(host_action={"pending": {**_pending_dict(), "attempt": 0}})
        outcome = read_pending_action(payload)
        assert isinstance(outcome, SectionUnavailable) and "attempt" in outcome.detail


def _pending_dict() -> dict[str, object]:
    return {
        "action_id": ACTION.action_id,
        "action_kind": ACTION.action_kind,
        "nonce": ACTION.nonce,
        "expected_head_sha": ACTION.expected_head_sha,
        "result_path": ACTION.result_path,
        "envelope_path": ACTION.envelope_path,
        "envelope_hash": ACTION.envelope_hash,
        "correlation_id": ACTION.correlation_id,
        "attempt": ACTION.attempt,
    }


class TestReceipts:
    def test_round_trip(self) -> None:
        payload = with_receipt(checkpoint_payload(), RECEIPT)
        assert _valid(payload)
        assert read_receipts(payload) == (RECEIPT,)

    def test_completed_receipt_keeps_the_result_kind(self) -> None:
        receipt = SubmitReceipt(
            action_id="act-2",
            nonce="nonce-2",
            outcome="COMPLETED",
            submit_hash="s" * 64,
            result_hash="r" * 64,
            result_kind=RecordKind.FIX_RESULT,
        )
        payload = with_receipt(checkpoint_payload(), receipt)
        assert _valid(payload) and read_receipts(payload) == (receipt,)

    def test_ledger_keeps_every_attempt(self) -> None:
        import dataclasses

        second = dataclasses.replace(RECEIPT, action_id="act-2", nonce="nonce-2")
        payload = with_receipt(with_receipt(checkpoint_payload(), RECEIPT), second)
        assert read_receipts(payload) == (RECEIPT, second)

    def test_retry_attempt_keeps_the_ledger(self) -> None:
        """同じlogical actionのattemptでは、過去attemptのreceiptを保つ。"""
        import dataclasses

        payload = with_receipt(with_new_logical_action(checkpoint_payload(), ACTION), RECEIPT)
        following = dataclasses.replace(ACTION, action_id="act-2", nonce="nonce-2", attempt=2)
        assert read_receipts(with_retry_attempt(payload, following)) == (RECEIPT,)

    def test_new_logical_action_replaces_the_ledger(self) -> None:
        """新しいlogical actionでは前のreceiptを持ち越さない（ADR-0015 決定22）。"""
        import dataclasses

        payload = with_receipt(with_new_logical_action(checkpoint_payload(), ACTION), RECEIPT)
        fresh = dataclasses.replace(ACTION, action_id="act-9", nonce="nonce-9", correlation_id="corr-9")
        assert read_receipts(with_new_logical_action(payload, fresh)) == ()

    def test_ledger_has_a_structural_upper_bound(self) -> None:
        """checkpointが常に書ける大きさへ構造的に固定する。"""
        import dataclasses

        payload: dict[str, object] = checkpoint_payload()
        for index in range(MAX_SUBMIT_RECEIPTS + 1):
            payload = with_receipt(
                payload, dataclasses.replace(RECEIPT, action_id=f"act-{index}", nonce=f"n-{index}")
            )
        assert not _valid(payload)

    def test_absent_ledger_is_empty(self) -> None:
        assert read_receipts(checkpoint_payload()) == ()
        assert read_receipts(with_new_logical_action(checkpoint_payload(), ACTION)) == ()

    def test_find_receipt_matches_the_attempt(self) -> None:
        receipts = read_receipts(with_receipt(checkpoint_payload(), RECEIPT))
        assert isinstance(receipts, tuple)
        assert find_receipt(receipts, action_id="act-1", nonce="nonce-1") == RECEIPT
        assert find_receipt(receipts, action_id="act-1", nonce="other") is None


def _receipt_dict() -> dict[str, object]:
    return {
        "action_id": RECEIPT.action_id,
        "nonce": RECEIPT.nonce,
        "outcome": RECEIPT.outcome,
        "submit_hash": RECEIPT.submit_hash,
        "result_hash": RECEIPT.result_hash,
    }


class TestUnreadableReceipts:
    def test_not_a_list(self) -> None:
        outcome = read_receipts(checkpoint_payload(host_action={"receipts": "text"}))
        assert isinstance(outcome, SectionUnavailable)

    def test_entry_not_an_object(self) -> None:
        outcome = read_receipts(checkpoint_payload(host_action={"receipts": ["text"]}))
        assert isinstance(outcome, SectionUnavailable)

    def test_entry_missing_a_field(self) -> None:
        outcome = read_receipts(checkpoint_payload(host_action={"receipts": [{}]}))
        assert isinstance(outcome, SectionUnavailable)

    def test_unknown_result_kind(self) -> None:
        entry = {**_receipt_dict(), "result_kind": "NOT_A_KIND"}
        outcome = read_receipts(checkpoint_payload(host_action={"receipts": [entry]}))
        assert isinstance(outcome, SectionUnavailable) and "未知" in outcome.detail

    def test_section_not_an_object(self) -> None:
        assert isinstance(read_receipts(checkpoint_payload(host_action="text")), SectionUnavailable)


class TestMachineState:
    def test_round_trip_with_awaiting(self) -> None:
        state = MachineState(state=State.APPLYING_FIXES, awaiting=Awaiting.HOST_APPLY_FINDINGS)
        payload = with_machine_state(checkpoint_payload(), state)
        assert _valid(payload) and read_machine_state(payload) == state

    def test_round_trip_with_pending_record(self) -> None:
        state = MachineState(
            state=State.APPLYING_FIXES,
            pending_record=PendingRecord(
                kind=RecordKind.FIX_RESULT,
                binding=OpaqueBinding("cr:run-1:00000002:x"),
                source_state=State.APPLYING_FIXES,
            ),
        )
        payload = with_machine_state(checkpoint_payload(), state)
        assert _valid(payload) and read_machine_state(payload) == state

    def test_round_trip_with_recovery(self) -> None:
        state = MachineState(state=State.FAILED, recovery_to=State.APPLYING_FIXES)
        payload = with_machine_state(checkpoint_payload(), state)
        assert _valid(payload) and read_machine_state(payload) == state

    def test_round_trip_with_return_to(self) -> None:
        state = MachineState(
            state=State.AWAITING_TOOL_PERMISSION,
            awaiting=Awaiting.USER_INPUT_PERMISSION,
            return_to=State.APPLYING_FIXES,
        )
        payload = with_machine_state(checkpoint_payload(), state)
        assert _valid(payload) and read_machine_state(payload) == state

    def test_other_state_fields_are_kept(self) -> None:
        payload = checkpoint_payload(state={"state": "WAITING_CI", "round": 2, "session_id": "s-1"})
        updated = with_machine_state(payload, MachineState(state=State.APPLYING_FIXES))
        assert updated["state"]["round"] == 2 and updated["state"]["session_id"] == "s-1"

    def test_stale_awaiting_is_dropped_when_overwritten(self) -> None:
        payload = with_machine_state(
            checkpoint_payload(),
            MachineState(state=State.APPLYING_FIXES, awaiting=Awaiting.HOST_APPLY_FINDINGS),
        )
        updated = with_machine_state(payload, MachineState(state=State.WAITING_CI))
        assert "awaiting" not in updated["state"]

    def test_missing_section_is_reported(self) -> None:
        assert isinstance(read_machine_state(checkpoint_payload()), SectionUnavailable)

    def test_section_not_an_object_is_reported(self) -> None:
        assert isinstance(read_machine_state(checkpoint_payload(state="text")), SectionUnavailable)

    def test_missing_state_value_is_reported(self) -> None:
        assert isinstance(read_machine_state(checkpoint_payload(state={"round": 1})), SectionUnavailable)

    def test_unknown_state_value_is_reported(self) -> None:
        outcome = read_machine_state(checkpoint_payload(state={"state": "NOT_A_STATE"}))
        assert isinstance(outcome, SectionUnavailable)

    def test_state_needing_unsaved_context_is_reported(self) -> None:
        """`BLOCKED`はblock contextを要する。既定値で埋めると不変条件が壊れる。"""
        outcome = read_machine_state(checkpoint_payload(state={"state": "BLOCKED"}))
        assert isinstance(outcome, SectionUnavailable) and "復元できない" in outcome.detail

    def test_pending_record_not_an_object_is_reported(self) -> None:
        payload = checkpoint_payload(state={"state": "APPLYING_FIXES", "pending_record": "text"})
        assert isinstance(read_machine_state(payload), SectionUnavailable)

    def test_incomplete_pending_record_is_reported(self) -> None:
        payload = checkpoint_payload(
            state={"state": "APPLYING_FIXES", "pending_record": {"kind": "FIX_RESULT"}}
        )
        outcome = read_machine_state(payload)
        assert isinstance(outcome, SectionUnavailable) and "pending_record" in outcome.detail


class TestVerifiedMachineState:
    """**読み戻せない状態を書かない**（ADR-0017）。"""

    def test_representable_state_round_trips(self) -> None:
        violation = IntegrityEvidenceRef(
            binding=OpaqueBinding("iv:marker:run-1:c1"),
            descriptor=OpaqueRef("desc"),
            head=OpaqueRef("head-1"),
        )
        state = MachineState(
            state=State.APPLYING_FIXES,
            procedure=HaltingForBlockProcedure(
                block=RecordIntegrityBlock((violation,)), attempt_binding=violation.binding
            ),
        )
        payload = with_verified_machine_state(checkpoint_payload(), state)
        assert not isinstance(payload, SectionUnavailable)
        assert _valid(payload) and read_machine_state(payload) == state

    def test_blocked_state_round_trips(self) -> None:
        violation = IntegrityEvidenceRef(
            binding=OpaqueBinding("iv:gap:run-1:00000002"),
            descriptor=OpaqueRef("desc"),
            head=OpaqueRef("head-1"),
        )
        state = MachineState(state=State.BLOCKED, block=RecordIntegrityBlock((violation,)))
        payload = with_verified_machine_state(checkpoint_payload(), state)
        assert not isinstance(payload, SectionUnavailable)
        assert read_machine_state(payload) == state

    def test_deferred_integrity_round_trips(self) -> None:
        violation = IntegrityEvidenceRef(
            binding=OpaqueBinding("iv:edited:run-1:c3"),
            descriptor=OpaqueRef("desc"),
            head=OpaqueRef("head-1"),
        )
        state = MachineState(
            state=State.MERGING,
            awaiting=Awaiting.MERGE_OUTCOME_EXECUTE,
            deferred_integrity=(violation,),
        )
        payload = with_verified_machine_state(checkpoint_payload(), state)
        assert not isinstance(payload, SectionUnavailable)
        assert read_machine_state(payload) == state

    def test_unrepresentable_procedure_is_refused(self) -> None:
        """checkpointがまだ表現しない付随値は、黙って落とさず保存を拒否する。"""
        from claude_code_codex_review_loop.domain.values import CancellingProcedure

        state = MachineState(
            state=State.APPLYING_FIXES,
            procedure=CancellingProcedure(attempt_binding=OpaqueBinding("cancel-1")),
        )
        outcome = with_verified_machine_state(checkpoint_payload(), state)
        assert isinstance(outcome, SectionUnavailable)

    def test_unknown_procedure_kind_is_reported(self) -> None:
        payload = checkpoint_payload(
            state={"state": "APPLYING_FIXES", "procedure": {"kind": "CANCELLING"}}
        )
        outcome = read_machine_state(payload)
        assert isinstance(outcome, SectionUnavailable) and "procedure" in outcome.detail

    def test_halt_gate_without_attempt_binding_is_reported(self) -> None:
        payload = checkpoint_payload(
            state={"state": "APPLYING_FIXES", "procedure": {"kind": "HALTING_FOR_BLOCK"}}
        )
        assert isinstance(read_machine_state(payload), SectionUnavailable)

    def test_unknown_block_kind_is_reported(self) -> None:
        payload = checkpoint_payload(state={"state": "BLOCKED", "block": {"kind": "PROGRESS"}})
        outcome = read_machine_state(payload)
        assert isinstance(outcome, SectionUnavailable) and "block" in outcome.detail

    def test_malformed_violation_is_reported(self) -> None:
        payload = checkpoint_payload(
            state={
                "state": "MERGING",
                "awaiting": "MERGE_OUTCOME_EXECUTE",
                "deferred_integrity": [{"binding": "iv:a:r:1"}],
            }
        )
        assert isinstance(read_machine_state(payload), SectionUnavailable)


class TestFieldsThatDisappear:
    """次の状態に無い付随値は残さない（残すと正当な遷移が保存できなくなる）。"""

    def _violation(self) -> IntegrityEvidenceRef:
        return IntegrityEvidenceRef(
            binding=OpaqueBinding("iv:marker:run-1:c1"),
            descriptor=OpaqueRef("desc"),
            head=OpaqueRef("head-1"),
        )

    def _halt_gate(self) -> MachineState:
        violation = self._violation()
        return MachineState(
            state=State.APPLYING_FIXES,
            procedure=HaltingForBlockProcedure(
                block=RecordIntegrityBlock((violation,)), attempt_binding=violation.binding
            ),
        )

    def test_halt_completion_drops_the_procedure(self) -> None:
        """停止完了でhalt gateを抜け、`BLOCKED`へ入る（procedureが消える）。"""
        saved = with_machine_state(checkpoint_payload(), self._halt_gate())
        blocked = MachineState(
            state=State.BLOCKED, block=RecordIntegrityBlock((self._violation(),))
        )
        payload = with_verified_machine_state(saved, blocked)
        assert not isinstance(payload, SectionUnavailable)
        assert "procedure" not in payload["state"]
        assert read_machine_state(payload) == blocked

    def test_returning_to_normal_drops_the_procedure(self) -> None:
        saved = with_machine_state(checkpoint_payload(), self._halt_gate())
        normal = MachineState(state=State.APPLYING_FIXES)
        payload = with_verified_machine_state(saved, normal)
        assert not isinstance(payload, SectionUnavailable)
        assert "procedure" not in payload["state"]
        assert read_machine_state(payload) == normal

    def test_block_resolution_drops_the_block(self) -> None:
        """block解消でRUNNING_REVIEWへ戻る（blockが消える）。"""
        blocked = MachineState(
            state=State.BLOCKED, block=RecordIntegrityBlock((self._violation(),))
        )
        saved = with_machine_state(checkpoint_payload(), blocked)
        resumed = MachineState(state=State.RUNNING_REVIEW, awaiting=Awaiting.CODEX_CODE_REVIEW)
        payload = with_verified_machine_state(saved, resumed)
        assert not isinstance(payload, SectionUnavailable)
        assert "block" not in payload["state"]
        assert read_machine_state(payload) == resumed

    def test_consuming_deferred_integrity_drops_the_set(self) -> None:
        deferred = MachineState(
            state=State.MERGING,
            awaiting=Awaiting.MERGE_OUTCOME_EXECUTE,
            deferred_integrity=(self._violation(),),
        )
        saved = with_machine_state(checkpoint_payload(), deferred)
        consumed = MachineState(state=State.MERGING, awaiting=Awaiting.MERGE_OUTCOME_EXECUTE)
        payload = with_verified_machine_state(saved, consumed)
        assert not isinstance(payload, SectionUnavailable)
        assert "deferred_integrity" not in payload["state"]
        assert read_machine_state(payload) == consumed


class TestUnreadableStateSections:
    """解釈できないstate sectionを「無い」へ丸めない（silent repair禁止）。"""

    @pytest.mark.parametrize(
        "section",
        [
            {"state": "APPLYING_FIXES", "procedure": "text"},
            {"state": "BLOCKED", "block": "text"},
            {"state": "MERGING", "awaiting": "MERGE_OUTCOME_EXECUTE", "deferred_integrity": "text"},
            {
                "state": "MERGING",
                "awaiting": "MERGE_OUTCOME_EXECUTE",
                "deferred_integrity": ["text"],
            },
        ],
        ids=["procedure", "block", "deferred_not_list", "violation_not_object"],
    )
    def test_malformed_section_is_reported(self, section: dict[str, object]) -> None:
        assert isinstance(read_machine_state(checkpoint_payload(state=section)), SectionUnavailable)

    def test_normal_procedure_is_accepted(self) -> None:
        """明示的な`NORMAL`も読める（省略と同じ意味）。"""
        payload = checkpoint_payload(
            state={"state": "APPLYING_FIXES", "procedure": {"kind": "NORMAL"}}
        )
        assert read_machine_state(payload) == MachineState(state=State.APPLYING_FIXES)

    def test_state_needing_an_unsupported_block_is_refused(self) -> None:
        """まだ表現しないblock種別を持つ状態は、保存すると読めなくなるので拒否する。"""
        from claude_code_codex_review_loop.domain.values import (
            BlockedContinuation,
            Budget,
            Progress,
            ProgressBlock,
        )

        block = ProgressBlock(
            binding=OpaqueBinding("b-1"),
            head=OpaqueRef("head-1"),
            reason=Progress.NO_PROGRESS,
            budget=Budget.REVIEW_ROUND,
            counter_snapshot=None,
            fingerprint=None,
            continuation=BlockedContinuation(
                resume_state=State.RUNNING_REVIEW, awaiting=Awaiting.CODEX_CODE_REVIEW, commands=()
            ),
        )
        state = MachineState(state=State.BLOCKED, block=block)
        assert isinstance(with_verified_machine_state(checkpoint_payload(), state), SectionUnavailable)


class TestNextAttempt:
    def test_keeps_the_logical_action_and_increments(self) -> None:
        following = next_attempt(
            ACTION,
            action_id="act-2",
            nonce="nonce-2",
            result_path="actions/act-2/result.json",
            envelope_path="actions/act-2/action.json",
            envelope_hash="f" * 64,
            issued_at="2026-08-25T09:10:00Z",
        )
        assert following.correlation_id == ACTION.correlation_id
        assert following.attempt == ACTION.attempt + 1
        assert following.action_kind == ACTION.action_kind
        assert following.expected_head_sha == ACTION.expected_head_sha
