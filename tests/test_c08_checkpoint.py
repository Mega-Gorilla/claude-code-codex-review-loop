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
    MachineState,
    OpaqueBinding,
    PendingRecord,
    RecordKind,
    State,
)
from claude_code_codex_review_loop.errors import ErrorCategory
from claude_code_codex_review_loop.schema import REGISTRY, SchemaKind, validate_object
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
    with_pending_action,
    with_receipt,
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
        payload = with_pending_action(checkpoint_payload(), ACTION)
        assert _valid(payload)
        assert read_pending_action(payload) == ACTION

    def test_absent_section_is_none(self) -> None:
        assert read_pending_action(checkpoint_payload()) is None

    def test_section_without_pending_is_none(self) -> None:
        payload = without_pending_action(with_pending_action(checkpoint_payload(), ACTION))
        assert read_pending_action(payload) is None

    def test_issued_at_is_optional(self) -> None:
        import dataclasses

        action = dataclasses.replace(ACTION, issued_at=None)
        payload = with_pending_action(checkpoint_payload(), action)
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

    def test_absent_ledger_is_empty(self) -> None:
        assert read_receipts(checkpoint_payload()) == ()
        assert read_receipts(with_pending_action(checkpoint_payload(), ACTION)) == ()

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
