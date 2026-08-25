# SPDX-License-Identifier: Apache-2.0
"""I系列: integrity violationとincident監査（AC-C01-12）。"""

from __future__ import annotations

import pytest
from c01_support.helpers import (
    binding,
    evidence,
    names,
    start,
    to_applying_fixes,
    to_merge_outcome,
    to_merging,
    to_progress_blocked,
    to_waiting_ci,
    violation,
)

from claude_code_codex_review_loop.domain import State, TransitionRejected, initialize, transition
from claude_code_codex_review_loop.domain import events as ev
from claude_code_codex_review_loop.domain.commands import InvalidateApprovals, RecordIntegrityIncident
from claude_code_codex_review_loop.domain.values import (
    Awaiting,
    BlockResolutionEvidence,
    CancellingProcedure,
    HaltingForBlockProcedure,
    MachineState,
    RecordingIncidentProcedure,
    RecordIntegrityBlock,
    RecordKind,
)

_K = RecordKind


def _detect(bind: str = "v-1") -> ev.RecordIntegrityViolationDetected:
    return ev.RecordIntegrityViolationDetected(violation(bind))


def _integrity_resolution(block: RecordIntegrityBlock) -> BlockResolutionEvidence:
    return BlockResolutionEvidence(
        target_block_binding=block.representative_binding,
        head=block.head,
        violation_bindings=tuple(ref.binding for ref in block.violations),
    )


def _record_incident(ms: MachineState, bind: str, recorded: tuple[str, ...]) -> tuple[MachineState, tuple]:
    """incident recordのPRODUCED -> persist -> VERIFIEDを実行する。"""
    ms, commands = transition(ms, ev.RecordProduced(_K.INTEGRITY_INCIDENT, binding(bind)))
    assert names(commands) == ("PersistRecord",)
    return transition(
        ms,
        ev.IntegrityIncidentVerified(
            evidence(_K.INTEGRITY_INCIDENT, bind), recorded_bindings=tuple(binding(b) for b in recorded)
        ),
    )


class TestI1ImmediateInvalidation:
    """I1: 検出を受理する全経路で承認が即時・冪等に失効する。"""

    def test_all_accepting_paths_issue_invalidate(self) -> None:
        cases: list[tuple[str, MachineState]] = []
        cases.append(("active", start()))
        cases.append(("resumable", to_waiting_ci()))
        cases.append(("merging-preconditions", to_merging()))
        cases.append(("merging-outcome", to_merge_outcome()))
        cases.append(("blocked-progress", to_progress_blocked()))
        cancelling, _ = transition(start(), ev.UserCancelVerified(evidence(_K.USER_CANCEL, "cx-1")))
        cases.append(("cancelling", cancelling))
        for label, ms in cases:
            _, commands = transition(ms, _detect())
            assert InvalidateApprovals() in commands, label


class TestI2MergingEndToEnd:
    """I2: MERGINGの3局面のend-to-end（安全停止 / 照会継続 / 確定後の終端処理）。"""

    def test_preconditions_stage_blocks_safely(self) -> None:
        ms = to_merging()
        blocked, commands = transition(ms, _detect())
        assert blocked.state is State.BLOCKED
        assert isinstance(blocked.block, RecordIntegrityBlock)
        assert names(commands) == ("InvalidateApprovals",)  # mergeは実行されない

    def test_outcome_stage_keeps_querying_and_confirm_records_incident(self) -> None:
        ms = to_merge_outcome()
        ms, commands = transition(ms, _detect())
        assert ms.state is State.MERGING and ms.awaiting is Awaiting.MERGE_OUTCOME_EXECUTE
        assert names(commands) == ("InvalidateApprovals", "QueryMergeOutcome")
        # unknownが続く限り照会のみ（agent / merge commandは発行されない）
        unknown, commands = transition(ms, ev.MergeOutcomeUnknown())
        assert unknown == ms and names(commands) == ("QueryMergeOutcome",)
        # resumeも照会のみ
        resumed, commands = transition(unknown, ev.ResumeValidated())
        assert resumed == ms and names(commands) == ("QueryMergeOutcome",)
        # 確定（merge完了）でincident記録へ。MERGEDへは検証後にのみ進む
        recording, commands = transition(unknown, ev.MergeConfirmed())
        assert recording.state is State.MERGING
        assert isinstance(recording.procedure, RecordingIncidentProcedure)
        assert names(commands) == ("RecordIntegrityIncident",)
        merged, _ = _record_incident(recording, "ic-1", ("v-1",))
        assert merged.state is State.MERGED

    def test_failure_origin_not_executed_goes_to_integrity_block(self) -> None:
        """failure起点の未実行確認（deferredあり）はMERGE_FAILEDではなくRECORD_INTEGRITYのBLOCKEDへ。"""
        ms = to_merge_outcome(Awaiting.MERGE_OUTCOME_FAILURE)
        ms, _ = transition(ms, _detect())
        blocked, _ = transition(ms, ev.MergeNotExecutedConfirmed())
        assert blocked.state is State.BLOCKED
        assert isinstance(blocked.block, RecordIntegrityBlock)
        # MERGE_FAILED経由の通常resume（ResumeSameHeadValidated）でgateを迂回できない
        with pytest.raises(TransitionRejected):
            transition(blocked, ev.ResumeSameHeadValidated())

    def test_cancel_origin_not_executed_records_incident_before_cancelled(self) -> None:
        ms = to_merge_outcome(Awaiting.MERGE_OUTCOME_CANCEL)
        ms, _ = transition(ms, _detect())
        recording, commands = transition(ms, ev.MergeNotExecutedConfirmed())
        assert recording.state is State.MERGING
        assert isinstance(recording.procedure, RecordingIncidentProcedure)
        assert names(commands) == ("RecordIntegrityIncident",)
        cancelled, _ = _record_incident(recording, "ic-1", ("v-1",))
        assert cancelled.state is State.CANCELLED


class TestI3HaltGate:
    """I3: active stateは停止gate経由、resumable stateは直接BLOCKED。旧resume情報は残らない。"""

    def test_active_state_uses_halt_gate(self) -> None:
        ms = to_applying_fixes()
        ms, commands = transition(ms, _detect())
        assert ms.state is State.APPLYING_FIXES  # 停止完了までBLOCKEDにしない
        assert isinstance(ms.procedure, HaltingForBlockProcedure)
        assert names(commands) == ("InvalidateApprovals", "HaltRun")
        blocked, _ = transition(ms, ev.BlockHaltCompleted(ms.procedure.attempt_binding))
        assert blocked.state is State.BLOCKED and isinstance(blocked.block, RecordIntegrityBlock)

    def test_halt_completion_binding_mismatch_rejected(self) -> None:
        ms, _ = transition(start(), _detect())
        with pytest.raises(TransitionRejected):
            transition(ms, ev.BlockHaltCompleted(attempt_binding=binding("other")))

    def test_resumable_state_blocks_directly_and_drops_old_resume_info(self) -> None:
        failed, _ = transition(start(), ev.RunFailed())
        assert failed.recovery_to is State.RUNNING_REVIEW
        blocked, _ = transition(failed, _detect())
        assert blocked.state is State.BLOCKED and blocked.recovery_to is None
        assert blocked.awaiting is None and blocked.pending_record is None


class TestI4FailClosed:
    """I4: RECORD_INTEGRITYのblockはgeneric fallbackを拒否し、専用evidenceのみで出られる。"""

    def _blocked(self) -> MachineState:
        ms, _ = transition(to_waiting_ci(), _detect())
        assert isinstance(ms.block, RecordIntegrityBlock)
        return ms

    def test_generic_fallback_is_rejected(self) -> None:
        ms = self._blocked()
        with pytest.raises(TransitionRejected):
            transition(ms, ev.ResumeFallbackRequired())

    def test_simple_resume_keeps_blocked(self) -> None:
        ms = self._blocked()
        kept, commands = transition(ms, ev.ResumeValidated())
        assert kept == ms and commands == ()

    @pytest.mark.parametrize("exit_event", [ev.IntegrityRestoredValidated, ev.IntegritySalvageEstablished])
    def test_dedicated_evidence_exits_to_fresh_review(self, exit_event) -> None:  # type: ignore[no-untyped-def]
        ms = self._blocked()
        assert isinstance(ms.block, RecordIntegrityBlock)
        ms2, commands = transition(ms, exit_event(_integrity_resolution(ms.block)))
        assert ms2.state is State.RUNNING_REVIEW
        assert names(commands) == ("InvalidateApprovals", "RequestCodexReview")

    def test_mismatched_dedicated_evidence_rejected(self) -> None:
        ms = self._blocked()
        bad = BlockResolutionEvidence(
            target_block_binding=binding("other"),
            head=violation().head,
            violation_bindings=(binding("other"),),
        )
        with pytest.raises(TransitionRejected):
            transition(ms, ev.IntegrityRestoredValidated(bad))

    @pytest.mark.parametrize("exit_event", [ev.IntegrityRestoredValidated, ev.IntegritySalvageEstablished])
    def test_stale_resolution_from_before_union_is_rejected(self, exit_event) -> None:  # type: ignore[no-untyped-def]
        """集合が拡大した後は、拡大前のviolation集合へbindしたevidenceで退出できない。"""
        ms, _ = transition(to_waiting_ci(), _detect("v-1"))
        assert isinstance(ms.block, RecordIntegrityBlock)
        stale = exit_event(_integrity_resolution(ms.block))  # [v-1]時点のevidence
        grown, _ = transition(ms, _detect("v-2"))
        with pytest.raises(TransitionRejected):
            transition(grown, stale)
        # 拡大後の集合全体へbindしたfresh evidenceのみが受理される
        assert isinstance(grown.block, RecordIntegrityBlock)
        fresh = exit_event(_integrity_resolution(grown.block))
        exited, _ = transition(grown, fresh)
        assert exited.state is State.RUNNING_REVIEW

    def test_intervention_rejected_for_integrity_block(self) -> None:
        from dataclasses import replace as dc_replace

        ms = self._blocked()
        assert isinstance(ms.block, RecordIntegrityBlock)
        with pytest.raises(TransitionRejected):
            transition(ms, ev.RecordProduced(_K.BLOCK_INTERVENTION, binding("bi-1")))
        with_record = dc_replace(
            _integrity_resolution(ms.block), record=evidence(_K.BLOCK_INTERVENTION, "bi-1")
        )
        with pytest.raises(TransitionRejected):
            transition(ms, ev.BlockResolvedIntervention(with_record))


class TestI5UnionNoSilentLoss:
    """I5: E1 -> E2の多重検出で上書き・silent lossがなく、全violation記録までterminalへ進まない。"""

    def test_union_during_outcome_query_and_serialized_recording(self) -> None:
        ms = to_merge_outcome()
        ms, _ = transition(ms, _detect("v-1"))
        ms, _ = transition(ms, _detect("v-2"))
        assert [ref.binding.value for ref in ms.deferred_integrity] == ["v-1", "v-2"]
        # 同一bindingの再検出は冪等
        ms, _ = transition(ms, _detect("v-1"))
        assert len(ms.deferred_integrity) == 2
        recording, _ = transition(ms, ev.MergeConfirmed())
        # 部分記録（v-1のみ）はREMAINDERとして残余の作成依頼を再発行し、terminalへ進まない
        partial, commands = _record_incident(recording, "ic-1", ("v-1",))
        assert partial.state is State.MERGING
        assert [ref.binding.value for ref in partial.deferred_integrity] == ["v-2"]
        assert names(commands) == ("RecordIntegrityIncident",)
        # 残余の記録が完了して初めてterminalへ
        merged, _ = _record_incident(partial, "ic-2", ("v-2",))
        assert merged.state is State.MERGED

    def test_union_during_cancelling_and_incident_recording(self) -> None:
        ms, _ = transition(start(), ev.UserCancelVerified(evidence(_K.USER_CANCEL, "cx-1")))
        ms, _ = transition(ms, _detect("v-1"))
        ms, _ = transition(ms, _detect("v-2"))
        assert len(ms.deferred_integrity) == 2
        procedure = ms.procedure
        assert isinstance(procedure, CancellingProcedure)
        recording, _ = transition(ms, ev.CancellationCompleted(attempt_binding=procedure.attempt_binding))
        assert isinstance(recording.procedure, RecordingIncidentProcedure)
        # incident記録中の追加検出も集合へunionされる
        recording, commands = transition(recording, _detect("v-3"))
        assert len(recording.deferred_integrity) == 3
        assert names(commands) == ("InvalidateApprovals",)
        cancelled, _ = _record_incident(recording, "ic-1", ("v-1", "v-2", "v-3"))
        assert cancelled.state is State.CANCELLED

    def test_halt_attempt_binding_survives_a_lower_ordered_violation(self) -> None:
        """停止gate中に**辞書順で前になる**違反を検出しても、attemptのidentityは変わらない。

        violation bindingは`iv:<condition>:<run>:<subject>`で、辞書順はcondition名が主キー
        になるため検出順と単調でない。identityを集合の代表値に載せていると、発行済みの停止の
        完了報告が拒否される（ADR-0016）。
        """
        ms, commands = transition(to_applying_fixes(), _detect("v-2"))
        assert names(commands) == ("InvalidateApprovals", "HaltRun")
        issued = [c for c in commands if type(c).__name__ == "HaltRun"][0].binding
        ms, _ = transition(ms, _detect("v-1"))
        procedure = ms.procedure
        assert isinstance(procedure, HaltingForBlockProcedure)
        # 集合は伸びて代表は入れ替わるが（I5）、attemptのidentityは動かない
        assert procedure.block.representative_binding == binding("v-1")
        assert procedure.attempt_binding == issued == binding("v-2")

    def test_reissued_halt_keeps_the_same_attempt(self) -> None:
        """resumeは同じattemptの停止commandを冪等に再発行する（別attemptを作らない）。"""
        ms, _ = transition(to_applying_fixes(), _detect("v-2"))
        ms, _ = transition(ms, _detect("v-1"))
        _, commands = transition(ms, ev.RunFailed())
        assert names(commands) == ("HaltRun",)
        assert commands[0].binding == binding("v-2")

    def test_completion_of_the_issued_attempt_is_accepted(self) -> None:
        """発行した停止の完了報告が受理され、追加違反もblockへ残る（I5と両立する）。"""
        ms, _ = transition(to_applying_fixes(), _detect("v-2"))
        ms, _ = transition(ms, _detect("v-1"))
        blocked, _ = transition(ms, ev.BlockHaltCompleted(attempt_binding=binding("v-2")))
        assert blocked.state is State.BLOCKED
        assert isinstance(blocked.block, RecordIntegrityBlock)
        assert [ref.binding.value for ref in blocked.block.violations] == ["v-1", "v-2"]

    def test_completion_of_the_moved_representative_is_rejected(self) -> None:
        """代表bindingは停止attemptの識別子ではない（過去・別attemptの完了を受理しない）。"""
        ms, _ = transition(to_applying_fixes(), _detect("v-2"))
        ms, _ = transition(ms, _detect("v-1"))
        with pytest.raises(TransitionRejected):
            transition(ms, ev.BlockHaltCompleted(attempt_binding=binding("v-1")))

    def test_union_during_halt_gate(self) -> None:
        ms, _ = transition(start(), _detect("v-1"))
        ms, commands = transition(ms, _detect("v-2"))
        procedure = ms.procedure
        assert isinstance(procedure, HaltingForBlockProcedure)
        assert [ref.binding.value for ref in procedure.block.violations] == ["v-1", "v-2"]
        assert names(commands) == ("InvalidateApprovals",)


class TestI6IncidentRecordGate:
    """I6: incident recordのcanonical record gate通過と、各段階失敗からの冪等再発行。"""

    def test_stage_failures_reissue_stage_command_only(self) -> None:
        ms = to_merge_outcome()
        ms, _ = transition(ms, _detect("v-1"))
        recording, _ = transition(ms, ev.MergeConfirmed())
        # 作成前の失敗 / resume: 作成依頼のみを再発行
        for event in (ev.RunFailed(), ev.ResumeValidated()):
            same, commands = transition(recording, event)
            assert same == recording
            assert names(commands) == ("RecordIntegrityIncident",)
        # 永続化待ちの失敗 / resume: persistのみを再発行
        pending, _ = transition(recording, ev.RecordProduced(_K.INTEGRITY_INCIDENT, binding("ic-1")))
        for event in (ev.RunFailed(), ev.ResumeValidated()):
            same, commands = transition(pending, event)
            assert same == pending
            assert names(commands) == ("PersistRecord",)

    def test_non_merging_cancel_origin_also_recovers(self) -> None:
        ms, _ = transition(to_waiting_ci(), ev.UserCancelVerified(evidence(_K.USER_CANCEL, "cx-1")))
        ms, _ = transition(ms, _detect("v-1"))
        procedure = ms.procedure
        assert isinstance(procedure, CancellingProcedure)
        recording, _ = transition(ms, ev.CancellationCompleted(attempt_binding=procedure.attempt_binding))
        same, commands = transition(recording, ev.ResumeValidated())
        assert same == recording and names(commands) == ("RecordIntegrityIncident",)
        cancelled, _ = _record_incident(recording, "ic-1", ("v-1",))
        assert cancelled.state is State.CANCELLED


class TestI7FullCancelIncidentSequence:
    """I7: pending中の外部cancel -> 検出 -> 停止完了 -> stale pending破棄（監査参照保持）-> 記録 -> CANCELLED。"""

    def test_full_sequence_preserves_audit_reference(self) -> None:
        ms = start()
        ms, _ = transition(ms, ev.RecordProduced(_K.REVIEW_RESULT, binding("r-1")))
        stale = ms.pending_record
        ms, _ = transition(ms, ev.UserCancelVerified(evidence(_K.USER_CANCEL, "cx-1")))
        assert ms.pending_record == stale  # 監査のため保持
        ms, _ = transition(ms, _detect("v-1"))
        procedure = ms.procedure
        assert isinstance(procedure, CancellingProcedure)
        recording, commands = transition(ms, ev.CancellationCompleted(attempt_binding=procedure.attempt_binding))
        assert recording.pending_record is None  # pending slotをincidentへ明け渡す
        record_command = commands[0]
        assert isinstance(record_command, RecordIntegrityIncident)
        assert record_command.audit == stale  # 破棄したpendingの監査参照がpayloadへ含まれる
        # 作成失敗 -> 新processからのresumeでも監査参照が失われない
        same, commands = transition(recording, ev.ResumeValidated())
        reissued = commands[0]
        assert isinstance(reissued, RecordIntegrityIncident)
        assert reissued.audit == stale
        cancelled, _ = _record_incident(recording, "ic-1", ("v-1",))
        assert cancelled.state is State.CANCELLED


class TestI8ViolationIdempotency:
    """I8: 404 / sequence gap / hash mismatchの各種別で、同一violationの再検出が冪等になる。"""

    @pytest.mark.parametrize("descriptor", ["404-not-found", "sequence-gap", "hash-mismatch"])
    def test_same_binding_is_idempotent_and_new_binding_is_separate(self, descriptor: str) -> None:
        ms = to_merge_outcome()
        ms, _ = transition(ms, ev.RecordIntegrityViolationDetected(violation("v-1", descriptor)))
        again, _ = transition(ms, ev.RecordIntegrityViolationDetected(violation("v-1", descriptor)))
        assert again.deferred_integrity == ms.deferred_integrity
        other, _ = transition(ms, ev.RecordIntegrityViolationDetected(violation("v-2", descriptor)))
        assert len(other.deferred_integrity) == 2

    def test_detection_while_integrity_blocked_unions_into_block(self) -> None:
        """RECORD_INTEGRITY block滞在中の検出はblock集合へunionされ、silentに失われない。"""
        ms, _ = transition(to_waiting_ci(), _detect("v-1"))
        grown, commands = transition(ms, _detect("v-2"))
        assert isinstance(grown.block, RecordIntegrityBlock)
        assert [ref.binding.value for ref in grown.block.violations] == ["v-1", "v-2"]
        assert names(commands) == ("InvalidateApprovals",)
        # 同一bindingの再検出は冪等（最初のevidenceを保持し、上書きしない）
        same, _ = transition(grown, _detect("v-2"))
        assert same.block == grown.block
        # 解消evidenceは拡大後の集合（代表binding）と照合される
        resolution = _integrity_resolution(grown.block)
        exited, _ = transition(grown, ev.IntegrityRestoredValidated(resolution))
        assert exited.state is State.RUNNING_REVIEW

    def test_detection_replaces_progress_block(self) -> None:
        """PROGRESS / EXTERNAL block滞在中の検出はRECORD_INTEGRITY blockへ切り替わる（旧blockは破棄）。"""
        ms = to_progress_blocked()
        blocked, _ = transition(ms, _detect("v-1"))
        assert isinstance(blocked.block, RecordIntegrityBlock)


class TestI9PreflightNg:
    """I9: preflight NGのFAILEDはresume系eventを拒否する（W0の失敗系列と対）。"""

    def test_resume_events_rejected_on_unstarted_failed(self) -> None:
        ms, _ = initialize(ev.PreflightNg())
        for event in (
            ev.ResumeValidated(),
            ev.ResumeFallbackRequired(),
            ev.ResumeSameHeadValidated(),
            ev.CiResumeRequested(),
            ev.PermissionResumeValidated(),
            ev.ReporterRetryRequested(),
        ):
            with pytest.raises(TransitionRejected):
                transition(ms, event)

    def test_new_run_initialize_is_the_only_recovery(self) -> None:
        _, _ = initialize(ev.PreflightNg())
        ms, commands = initialize(ev.PreflightOk())
        assert ms.state is State.RUNNING_REVIEW and names(commands) == ("RequestCodexReview",)
