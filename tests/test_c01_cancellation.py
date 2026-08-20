# SPDX-License-Identifier: Apache-2.0
"""X系列: cancellationとprocess停止（AC-C01-10）。"""

from __future__ import annotations

import pytest
from c01_support.helpers import (
    binding,
    evidence,
    names,
    produced_verified,
    start,
    to_applying_fixes,
    to_gate,
    to_merging,
    to_progress_blocked,
    to_waiting_ci,
)

from claude_code_codex_review_loop.domain import State, TransitionRejected, transition
from claude_code_codex_review_loop.domain import events as ev
from claude_code_codex_review_loop.domain.values import (
    Awaiting,
    CancellingProcedure,
    MachineState,
    OpaqueBinding,
    OpaqueRef,
    RecordKind,
)

_K = RecordKind


def _cancel(ms: MachineState, bind: str = "cx-1") -> MachineState:
    """経路2（GitHub直接comment）でcancel intentを検証済みにする。"""
    ms, commands = transition(ms, ev.UserCancelVerified(evidence(_K.USER_CANCEL, bind)))
    assert names(commands) == ("HaltRun",)
    assert isinstance(ms.procedure, CancellingProcedure)
    return ms


def _resumable_states() -> list[MachineState]:
    """全8 resumable stateの代表MachineStateを系列から構築する。"""
    failed, _ = transition(start(), ev.RunFailed())
    assert failed.state is State.FAILED
    permission = start()
    permission, _ = produced_verified(
        permission, _K.PERMISSION_BLOCK, "pb-1", ev.ToolPermissionBlocked(evidence(_K.PERMISSION_BLOCK, "pb-1"))
    )
    decision = to_applying_fixes()
    for kind, bind, event in (
        (_K.DECISION_REQUEST, "dr-1", ev.DecisionRequestVerified(evidence(_K.DECISION_REQUEST, "dr-1"))),
        (_K.DECISION_VERDICT, "dv-1", ev.VerdictAskUserVerified(evidence(_K.DECISION_VERDICT, "dv-1"))),
        (_K.DECISION_BRIEF, "db-1", ev.DecisionBriefVerified(evidence(_K.DECISION_BRIEF, "db-1"))),
    ):
        decision, _ = produced_verified(decision, kind, bind, event)
    report_failed, _ = transition(
        transition(to_waiting_ci(), ev.CiSucceeded())[0], ev.ReportFailed()
    )
    merge_failed, _ = transition(to_merging(), ev.MergePreconditionMismatch())
    states = [
        to_waiting_ci(),  # WAITING_CI
        decision,  # AWAITING_USER_DECISION
        permission,  # AWAITING_TOOL_PERMISSION
        to_gate(),  # READY_FOR_HUMAN_MERGE
        to_progress_blocked(),  # BLOCKED
        failed,  # FAILED
        report_failed,  # REPORT_FAILED
        merge_failed,  # MERGE_FAILED
    ]
    assert sorted(ms.state.value for ms in states) == sorted(
        s.value
        for s in (
            State.WAITING_CI,
            State.AWAITING_USER_DECISION,
            State.AWAITING_TOOL_PERMISSION,
            State.READY_FOR_HUMAN_MERGE,
            State.BLOCKED,
            State.FAILED,
            State.REPORT_FAILED,
            State.MERGE_FAILED,
        )
    )
    return states


class TestX1CompletionGate:
    """X1: intent検証 -> 停止command -> 完了event -> CANCELLED。完了前はterminalにならない。"""

    def test_cancel_flow_reaches_cancelled_only_after_completion(self) -> None:
        ms = _cancel(start())
        assert ms.state is State.RUNNING_REVIEW  # 完了eventまでterminalにならない
        procedure = ms.procedure
        assert isinstance(procedure, CancellingProcedure)
        done, commands = transition(ms, ev.CancellationCompleted(attempt_binding=procedure.attempt_binding))
        assert done.state is State.CANCELLED and commands == ()

    def test_transcribed_cancel_record_route(self) -> None:
        """経路1: USER_CANCELのPRODUCED（awaiting維持）-> persist -> VERIFIED。"""
        ms = start()
        ms, commands = transition(ms, ev.RecordProduced(_K.USER_CANCEL, binding("cx-1")))
        assert names(commands) == ("PersistRecord",)
        assert ms.awaiting is Awaiting.CODEX_CODE_REVIEW  # awaitingは消費せず維持
        ms, commands = transition(ms, ev.UserCancelVerified(evidence(_K.USER_CANCEL, "cx-1")))
        assert names(commands) == ("HaltRun",)
        assert ms.pending_record is None and isinstance(ms.procedure, CancellingProcedure)

    def test_no_new_agent_command_during_cancelling(self) -> None:
        ms = _cancel(to_applying_fixes())
        with pytest.raises(TransitionRejected):
            transition(ms, ev.RecordProduced(_K.FIX_RESULT, binding("fx-1")))


class TestX2ResumableHaltReissue:
    """X2: 全8 resumable stateでのcancel -> 停止失敗 -> resumeで、停止commandの再発行だけが返る。"""

    def test_halt_reissue_on_failure_and_resume_for_all_resumable_states(self) -> None:
        for base in _resumable_states():
            ms = _cancel(base, bind=f"cx-{base.state.value}")
            failed, commands = transition(ms, ev.RunFailed())
            assert failed == ms, base.state
            assert names(commands) == ("HaltRun",), base.state
            resumed, commands = transition(ms, ev.ResumeValidated())
            assert resumed == ms and names(commands) == ("HaltRun",), base.state


class TestX3BindingAndEmergency:
    """X3: attempt binding不一致の拒否と、緊急停止経路のrun / checkpoint bind検証。"""

    def test_mismatched_completion_event_is_rejected(self) -> None:
        ms = _cancel(start())
        with pytest.raises(TransitionRejected):
            transition(ms, ev.CancellationCompleted(attempt_binding=OpaqueBinding("stale-attempt")))
        # cancelling中の緊急停止evidenceも（attempt binding不一致として）拒否される
        with pytest.raises(TransitionRejected):
            transition(ms, ev.CancellationCompleted(emergency_evidence=OpaqueRef("ckpt-1")))

    def test_emergency_stop_with_verified_bind(self) -> None:
        ms = start()
        done, commands = transition(ms, ev.CancellationCompleted(emergency_evidence=OpaqueRef("ckpt-1")))
        assert done.state is State.CANCELLED and commands == ()

    def test_stale_pending_is_kept_for_audit_and_not_consumed(self) -> None:
        """cancel中のstale pendingはsemantic継続に使えず、監査のため保持される。"""
        ms = start()
        ms, _ = transition(ms, ev.RecordProduced(_K.REVIEW_RESULT, binding("r-1")))
        ms = _cancel(ms)
        assert ms.pending_record is not None  # 監査保持
        with pytest.raises(TransitionRejected):
            transition(ms, ev.ReviewApprovedVerified(evidence(_K.REVIEW_RESULT, "r-1")))


class TestX4MergingCancel:
    """X4: MERGING中のcancelは結果照会を経由し、未実行確認（cancel起点）でのみCANCELLEDになる。"""

    def test_cancel_in_merging_queries_outcome(self) -> None:
        ms = to_merging()
        ms, commands = transition(ms, ev.UserCancelVerified(evidence(_K.USER_CANCEL, "cx-1")))
        assert ms.state is State.MERGING and names(commands) == ("QueryMergeOutcome",)
        assert ms.awaiting is Awaiting.MERGE_OUTCOME_CANCEL
        cancelled, _ = transition(ms, ev.MergeNotExecutedConfirmed())
        assert cancelled.state is State.CANCELLED

    def test_cancel_after_execution_confirms_merged(self) -> None:
        ms = to_merging()
        ms, _ = transition(ms, ev.MergePreconditionsOk())
        ms, _ = transition(ms, ev.UserCancelVerified(evidence(_K.USER_CANCEL, "cx-1")))
        merged, _ = transition(ms, ev.MergeConfirmed())
        assert merged.state is State.MERGED
