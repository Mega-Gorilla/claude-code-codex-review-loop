# SPDX-License-Identifier: Apache-2.0
"""M系列: merge transaction（AC-C01-07 / 08）。"""

from __future__ import annotations

import pytest
from c01_support.helpers import evidence, names, produced_verified, start, to_gate, to_merge_outcome, to_merging

from claude_code_codex_review_loop.domain import REGISTRY, State, TransitionRejected, transition
from claude_code_codex_review_loop.domain import events as ev
from claude_code_codex_review_loop.domain.values import Awaiting, RecordKind

_K = RecordKind


class TestM1SingleExecutePath:
    """M1: merge実行commandの発行経路が「preconditions一致eventの消費」の1経路のみ。"""

    def test_registry_has_exactly_one_execute_merge_rule(self) -> None:
        issuers = [rule.rule_id for rule in REGISTRY if "ExecuteMerge" in rule.command_names]
        assert issuers == ["M-34"]

    def test_preconditions_ok_issues_execute_once(self) -> None:
        ms = to_merging()
        ms, commands = transition(ms, ev.MergePreconditionsOk())
        assert names(commands) == ("ExecuteMerge",)
        assert ms.awaiting is Awaiting.MERGE_OUTCOME_EXECUTE
        # 消費済み応答の再入力（preconditions一致eventの重複）は拒否される
        with pytest.raises(TransitionRejected):
            transition(ms, ev.MergePreconditionsOk())

    def test_completion_event_before_execute_is_rejected(self) -> None:
        ms = to_merging()  # awaiting = MERGE_PRECONDITIONS
        with pytest.raises(TransitionRejected):
            transition(ms, ev.MergeConfirmed())

    def test_merge_events_rejected_outside_merging(self) -> None:
        ms = to_gate()
        with pytest.raises(TransitionRejected):
            transition(ms, ev.MergePreconditionsOk())
        with pytest.raises(TransitionRejected):
            transition(start(), ev.MergeConfirmed())


class TestM2OutcomeAndHeadChange:
    """M2: 成否不明の安全停止と、各局面のhead変更失効。"""

    def test_unknown_outcome_fails_safe(self) -> None:
        ms = to_merge_outcome()
        failed, commands = transition(ms, ev.MergeOutcomeUnknown())
        assert failed.state is State.MERGE_FAILED and commands == ()

    def test_mismatch_and_failure_origin_not_executed(self) -> None:
        ms = to_merging()
        mismatch, _ = transition(ms, ev.MergePreconditionMismatch())
        assert mismatch.state is State.MERGE_FAILED
        failure_origin = to_merge_outcome(Awaiting.MERGE_OUTCOME_FAILURE)
        not_executed, _ = transition(failure_origin, ev.MergeNotExecutedConfirmed())
        assert not_executed.state is State.MERGE_FAILED

    @pytest.mark.parametrize(
        "build",
        [
            lambda: to_merging(),  # preconditions段階
            lambda: transition(to_merging(), ev.MergePreconditionMismatch())[0],  # MERGE_FAILED
        ],
    )
    def test_head_change_invalidates_and_restarts(self, build) -> None:  # type: ignore[no-untyped-def]
        ms = build()
        ms2, commands = transition(ms, ev.HeadChangedExternally())
        assert ms2.state is State.RUNNING_REVIEW
        assert names(commands) == ("InvalidateApprovals", "RequestCodexReview")

    def test_same_head_resume_returns_to_gate(self) -> None:
        ms, _ = transition(to_merging(), ev.MergePreconditionMismatch())
        ms2, commands = transition(ms, ev.ResumeSameHeadValidated())
        assert ms2.state is State.READY_FOR_HUMAN_MERGE
        assert ms2.awaiting is Awaiting.USER_INPUT_GATE and commands == ()

    def test_gate_after_resume_can_reapprove(self) -> None:
        ms, _ = transition(to_merging(), ev.MergePreconditionMismatch())
        ms, _ = transition(ms, ev.ResumeSameHeadValidated())
        ms, commands = produced_verified(
            ms, _K.MERGE_APPROVAL, "ap-2", ev.MergeApprovalVerified(evidence(_K.MERGE_APPROVAL, "ap-2"))
        )
        assert ms.state is State.MERGING and names(commands) == ("VerifyMergePreconditions",)


class TestM3MergingResume:
    """M3: MERGINGのcheckpoint -> 明示resumeが、局面に対応するcommandだけを再発行する。"""

    @pytest.mark.parametrize(
        "origin",
        [Awaiting.MERGE_OUTCOME_EXECUTE, Awaiting.MERGE_OUTCOME_CANCEL, Awaiting.MERGE_OUTCOME_FAILURE],
    )
    def test_outcome_stage_resume_reissues_query_only(self, origin: Awaiting) -> None:
        ms = to_merge_outcome(origin)
        resumed, commands = transition(ms, ev.ResumeValidated())
        assert resumed == ms  # originを維持する
        assert names(commands) == ("QueryMergeOutcome",)

    def test_preconditions_stage_resume_reissues_verification_only(self) -> None:
        ms = to_merging()
        resumed, commands = transition(ms, ev.ResumeValidated())
        assert resumed == ms
        assert names(commands) == ("VerifyMergePreconditions",)

    def test_run_failure_in_merging_switches_to_failure_origin_query(self) -> None:
        ms = to_merge_outcome()
        failed, commands = transition(ms, ev.RunFailed())
        assert failed.state is State.MERGING
        assert failed.awaiting is Awaiting.MERGE_OUTCOME_FAILURE
        assert names(commands) == ("QueryMergeOutcome",)
