# SPDX-License-Identifier: Apache-2.0
"""Z系列: resume整合（AC-C01-05 / 06）。"""

from __future__ import annotations

import pytest
from c01_support.helpers import binding, evidence, names, produced_verified, start, to_waiting_ci

from claude_code_codex_review_loop.domain import State, TransitionRejected, transition
from claude_code_codex_review_loop.domain import events as ev
from claude_code_codex_review_loop.domain.values import Awaiting, RecordKind

_K = RecordKind


class TestZ1ResumePriorities:
    """Z1: pending中は永続化確認、応答待ち中は対応command、いずれも無ければ復帰先の駆動command。"""

    def test_failed_with_pending_returns_to_source_and_reissues_persist(self) -> None:
        ms = start()
        ms, _ = transition(ms, ev.RecordProduced(_K.REVIEW_RESULT, binding("r-1")))
        failed, _ = transition(ms, ev.RunFailed())
        assert failed.state is State.FAILED and failed.recovery_to is State.RUNNING_REVIEW
        resumed, commands = transition(failed, ev.ResumeValidated())
        assert resumed.state is State.RUNNING_REVIEW and resumed.recovery_to is None
        assert names(commands) == ("PersistRecord",)
        done, _ = transition(resumed, ev.ReviewApprovedVerified(evidence(_K.REVIEW_RESULT, "r-1")))
        assert done.state is State.WAITING_CI

    def test_failed_with_awaiting_reissues_awaiting_command(self) -> None:
        failed, _ = transition(start(), ev.RunFailed())  # awaiting = CODEX_CODE_REVIEW引継
        assert failed.awaiting is Awaiting.CODEX_CODE_REVIEW
        resumed, commands = transition(failed, ev.ResumeValidated())
        assert resumed.state is State.RUNNING_REVIEW
        assert names(commands) == ("RequestCodexReview",)

    def test_failed_without_pending_or_awaiting_uses_drive_table(self) -> None:
        ms = to_waiting_ci()
        timed_out, _ = produced_verified(
            ms, _K.CI_TIMEOUT, "ct-1", ev.CiTimeoutRecorded(evidence(_K.CI_TIMEOUT, "ct-1"))
        )
        assert timed_out.awaiting is None
        # WAITING_CIはresumableでFAILEDへは落ちない。駆動表の検証には
        # GENERATING_REPORT（awaiting消費後にRunFailed）を使う
        report_pending, _ = transition(to_waiting_ci(), ev.CiSucceeded())
        report_pending, _ = transition(report_pending, ev.RecordProduced(_K.FINAL_REPORT, binding("rp-1")))
        report_verified, _ = transition(
            report_pending, ev.ReportVerified(evidence(_K.FINAL_REPORT, "rp-1"))
        )
        assert report_verified.state is State.READY_FOR_HUMAN_MERGE

    def test_reporter_retry_and_ci_resume(self) -> None:
        report_failed, _ = transition(transition(to_waiting_ci(), ev.CiSucceeded())[0], ev.ReportFailed())
        assert report_failed.state is State.REPORT_FAILED
        retried, commands = transition(report_failed, ev.ReporterRetryRequested())
        assert retried.state is State.GENERATING_REPORT and names(commands) == ("GenerateReport",)
        ci = to_waiting_ci()
        resumed, commands = transition(ci, ev.CiResumeRequested())
        assert resumed.state is State.WAITING_CI and names(commands) == ("CheckCi",)
        # timeout後（awaitingなし）のresumeも再確認を発行する
        timed_out, _ = produced_verified(
            to_waiting_ci(), _K.CI_TIMEOUT, "ct-1", ev.CiTimeoutRecorded(evidence(_K.CI_TIMEOUT, "ct-1"))
        )
        resumed, commands = transition(timed_out, ev.CiResumeRequested())
        assert resumed.awaiting is Awaiting.CI_RESULT and names(commands) == ("CheckCi",)

    def test_permission_resume_sets_command_and_awaiting_together(self) -> None:
        ms = start()
        ms, _ = produced_verified(
            ms, _K.PERMISSION_BLOCK, "pb-1", ev.ToolPermissionBlocked(evidence(_K.PERMISSION_BLOCK, "pb-1"))
        )
        resumed, commands = transition(ms, ev.PermissionResumeValidated())
        assert resumed.state is State.RUNNING_REVIEW
        assert resumed.awaiting is Awaiting.CODEX_CODE_REVIEW
        assert names(commands) == ("RequestCodexReview",)

    def test_failed_fallback_discards_and_restarts(self) -> None:
        failed, _ = transition(start(), ev.RunFailed())
        ms, commands = transition(failed, ev.ResumeFallbackRequired())
        assert ms.state is State.RUNNING_REVIEW
        assert names(commands) == ("InvalidateApprovals", "RequestCodexReview")
        assert ms.pending_record is None and ms.recovery_to is None


class TestZ2ResumablePreservation:
    """Z2: resumable stateでの失敗は状態と付随情報を保持する。"""

    def test_run_failed_on_resumable_state_preserves_everything(self) -> None:
        ms = to_waiting_ci()  # awaiting = CI_RESULT
        same, commands = transition(ms, ev.RunFailed())
        assert same == ms and commands == ()

    def test_run_failed_on_awaiting_tool_permission_keeps_return_to(self) -> None:
        ms = start()
        ms, _ = produced_verified(
            ms, _K.PERMISSION_BLOCK, "pb-1", ev.ToolPermissionBlocked(evidence(_K.PERMISSION_BLOCK, "pb-1"))
        )
        same, _ = transition(ms, ev.RunFailed())
        assert same == ms and same.return_to is State.RUNNING_REVIEW

    def test_active_failure_carries_pending_and_awaiting_into_failed(self) -> None:
        ms = start()
        ms, _ = transition(ms, ev.RecordProduced(_K.REVIEW_RESULT, binding("r-1")))
        failed, commands = transition(ms, ev.RunFailed())
        assert failed.state is State.FAILED and commands == ()
        assert failed.pending_record == ms.pending_record
        assert failed.recovery_to is State.RUNNING_REVIEW

    def test_undefined_resume_on_report_failed_is_rejected(self) -> None:
        """REPORT_FAILEDの復帰はreporter再実行のみで、genericなresumeは未定義として拒否される。"""
        report_failed, _ = transition(transition(to_waiting_ci(), ev.CiSucceeded())[0], ev.ReportFailed())
        with pytest.raises(TransitionRejected):
            transition(report_failed, ev.ResumeValidated())
