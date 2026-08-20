# SPDX-License-Identifier: Apache-2.0
"""R4系列（構築拒否側）: 不正な付随値の組合せを構築・受理できない（AC-C01-06）。

表現方式に依存せず、MachineState / value objectの構築が組合せ不変条件を強制することを、
違反codeごとに検証する。
"""

from __future__ import annotations

import pytest
from c01_support.helpers import HEAD, binding, violation

from claude_code_codex_review_loop.domain import State
from claude_code_codex_review_loop.domain import events as ev
from claude_code_codex_review_loop.domain.commands import HostAction, RequestHostAction
from claude_code_codex_review_loop.domain.events import IllegalEventError
from claude_code_codex_review_loop.domain.values import (
    Awaiting,
    BlockedContinuation,
    Budget,
    CancellingProcedure,
    HaltingForBlockProcedure,
    IllegalMachineStateError,
    IncidentTarget,
    MachineState,
    OpaqueFingerprint,
    OpaqueRef,
    OpaqueSnapshot,
    PendingRecord,
    Progress,
    ProgressBlock,
    RecordEvidence,
    RecordingIncidentProcedure,
    RecordIntegrityBlock,
    RecordKind,
    canonicalize_integrity,
)

_S = State
_K = RecordKind


def _progress_block(reason: Progress = Progress.LIMIT_REACHED) -> ProgressBlock:
    return ProgressBlock(
        binding=binding("b-1"),
        head=HEAD,
        continuation=BlockedContinuation(
            resume_state=_S.CHANGES_REQUESTED,
            commands=(RequestHostAction(HostAction.APPLY_FINDINGS),),
            awaiting=Awaiting.HOST_APPLY_FINDINGS,
        ),
        reason=reason,
        budget=Budget.REVIEW_ROUND,
        counter_snapshot=OpaqueSnapshot("s-1"),
        fingerprint=OpaqueFingerprint("f-1"),
    )


def _pending(kind: RecordKind = _K.REVIEW_RESULT, state: State = _S.RUNNING_REVIEW) -> PendingRecord:
    return PendingRecord(kind=kind, binding=binding("p-1"), source_state=state)


def _expect(code: str, **kwargs: object) -> None:
    with pytest.raises(IllegalMachineStateError) as exc_info:
        MachineState(**kwargs)  # type: ignore[arg-type]
    assert exc_info.value.code == code


class TestMachineStateInvariants:
    def test_terminal_states_cannot_carry_payload(self) -> None:
        _expect("TERMINAL_BARE", state=_S.MERGED, awaiting=Awaiting.CI_RESULT)
        _expect("TERMINAL_BARE", state=_S.CANCELLED, deferred_integrity=(violation(),))

    def test_deferred_must_be_canonical(self) -> None:
        refs = (violation("v-2"), violation("v-1"))
        _expect(
            "DEFERRED_CANONICAL",
            state=_S.MERGING,
            awaiting=Awaiting.MERGE_OUTCOME_EXECUTE,
            deferred_integrity=refs,
        )
        assert [r.binding.value for r in canonicalize_integrity(refs)] == ["v-1", "v-2"]

    def test_block_only_in_blocked_and_blocked_requires_block(self) -> None:
        _expect("BLOCK_SCOPE", state=_S.RUNNING_REVIEW, block=_progress_block())
        _expect("BLOCK_SCOPE", state=_S.BLOCKED)

    def test_blocked_shape(self) -> None:
        _expect("BLOCKED_NO_AWAITING", state=_S.BLOCKED, block=_progress_block(), awaiting=Awaiting.CI_RESULT)
        _expect(
            "BLOCKED_NO_HALT_GATE",
            state=_S.BLOCKED,
            block=_progress_block(),
            procedure=HaltingForBlockProcedure(block=RecordIntegrityBlock((violation(),))),
        )
        _expect(
            "BLOCKED_PENDING_KIND",
            state=_S.BLOCKED,
            block=_progress_block(),
            pending_record=_pending(_K.REVIEW_RESULT, _S.BLOCKED),
        )

    def test_recovery_scope_and_target(self) -> None:
        _expect("RECOVERY_SCOPE", state=_S.RUNNING_REVIEW, recovery_to=_S.APPLYING_FIXES)
        _expect("RECOVERY_TARGET", state=_S.FAILED, recovery_to=_S.MERGING)
        _expect("RECOVERY_TARGET", state=_S.FAILED, recovery_to=_S.BLOCKED)

    def test_return_scope_and_target(self) -> None:
        _expect("RETURN_SCOPE", state=_S.RUNNING_REVIEW, return_to=_S.RUNNING_REVIEW)
        _expect("RETURN_SCOPE", state=_S.AWAITING_TOOL_PERMISSION, awaiting=Awaiting.USER_INPUT_PERMISSION)
        _expect(
            "RETURN_TARGET",
            state=_S.AWAITING_TOOL_PERMISSION,
            awaiting=Awaiting.USER_INPUT_PERMISSION,
            return_to=_S.MERGING,
        )

    def test_procedure_shape(self) -> None:
        _expect(
            "PROCEDURE_NO_AWAITING",
            state=_S.RUNNING_REVIEW,
            procedure=CancellingProcedure(binding("c-1")),
            awaiting=Awaiting.CODEX_CODE_REVIEW,
        )
        _expect("MERGING_NO_CANCELLING", state=_S.MERGING, procedure=CancellingProcedure(binding("c-1")))
        halt = HaltingForBlockProcedure(block=RecordIntegrityBlock((violation(),)))
        _expect("HALT_GATE_STATE", state=_S.WAITING_CI, procedure=halt)
        _expect("HALT_GATE_STATE", state=_S.MERGING, procedure=halt)
        _expect(
            "HALT_GATE_NO_PENDING",
            state=_S.RUNNING_REVIEW,
            procedure=halt,
            pending_record=_pending(),
        )
        _expect(
            "HALT_GATE_NO_DEFERRED",
            state=_S.RUNNING_REVIEW,
            procedure=halt,
            deferred_integrity=(violation(),),
        )

    def test_incident_procedure_shape(self) -> None:
        recording = RecordingIncidentProcedure(target=IncidentTarget.CANCELLED, audit=None)
        _expect("INCIDENT_NEEDS_DEFERRED", state=_S.RUNNING_REVIEW, procedure=recording)
        _expect(
            "INCIDENT_PENDING_KIND",
            state=_S.RUNNING_REVIEW,
            procedure=recording,
            deferred_integrity=(violation(),),
            pending_record=_pending(),
        )
        _expect(
            "INCIDENT_PENDING_SCOPE",
            state=_S.RUNNING_REVIEW,
            awaiting=Awaiting.CODEX_CODE_REVIEW,
            pending_record=_pending(_K.INTEGRITY_INCIDENT),
        )

    def test_deferred_scope(self) -> None:
        _expect("DEFERRED_SCOPE", state=_S.RUNNING_REVIEW, deferred_integrity=(violation(),))
        _expect(
            "DEFERRED_SCOPE",
            state=_S.MERGING,
            awaiting=Awaiting.MERGE_PRECONDITIONS,
            deferred_integrity=(violation(),),
        )

    def test_pending_home_and_awaiting_home(self) -> None:
        _expect("PENDING_HOME", state=_S.WAITING_CI, pending_record=_pending(_K.REVIEW_RESULT, _S.RUNNING_REVIEW))
        _expect("AWAITING_HOME", state=_S.RUNNING_REVIEW, awaiting=Awaiting.CI_RESULT)

    def test_valid_states_construct(self) -> None:
        assert MachineState(state=_S.MERGED).state is _S.MERGED
        assert MachineState(state=_S.BLOCKED, block=_progress_block()).block is not None
        failed = MachineState(state=_S.FAILED, recovery_to=_S.RUNNING_REVIEW, awaiting=Awaiting.CODEX_CODE_REVIEW)
        assert failed.recovery_to is _S.RUNNING_REVIEW


class TestBlockValueObjects:
    def test_progress_block_rejects_continue_reason(self) -> None:
        with pytest.raises(IllegalMachineStateError) as exc_info:
            _progress_block(Progress.CONTINUE)
        assert exc_info.value.code == "PROGRESS_BLOCK_REASON"

    def test_integrity_block_requires_canonical_nonempty_violations(self) -> None:
        with pytest.raises(IllegalMachineStateError) as empty:
            RecordIntegrityBlock(violations=())
        assert empty.value.code == "INTEGRITY_BLOCK_EMPTY"
        with pytest.raises(IllegalMachineStateError) as unordered:
            RecordIntegrityBlock(violations=(violation("v-2"), violation("v-1")))
        assert unordered.value.code == "INTEGRITY_BLOCK_ORDER"
        block = RecordIntegrityBlock(violations=(violation("v-1"), violation("v-2")))
        assert block.representative_binding == binding("v-1")
        assert block.head == HEAD


class TestEventShape:
    def test_verified_event_rejects_wrong_evidence_kind(self) -> None:
        wrong = RecordEvidence(kind=_K.FIX_RESULT, binding=binding("x"), ref=OpaqueRef("r"))
        with pytest.raises(IllegalEventError) as exc_info:
            ev.ReviewApprovedVerified(wrong)
        assert exc_info.value.code == "EVIDENCE_KIND"

    def test_cancellation_completed_requires_exactly_one_origin(self) -> None:
        with pytest.raises(IllegalEventError):
            ev.CancellationCompleted()
        with pytest.raises(IllegalEventError):
            ev.CancellationCompleted(attempt_binding=binding("a"), emergency_evidence=OpaqueRef("e"))
