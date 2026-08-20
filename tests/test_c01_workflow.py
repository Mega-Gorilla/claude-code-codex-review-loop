# SPDX-License-Identifier: Apache-2.0
"""W系列: main workflowとbounded progress（AC-C01-03 / 09 / 11）。"""

from __future__ import annotations

import pytest
from c01_support.helpers import (
    binding,
    evidence,
    names,
    produced_verified,
    report,
    start,
    to_applying_fixes,
    to_changes_requested,
    to_gate,
    to_progress_blocked,
    to_waiting_ci,
)

from claude_code_codex_review_loop.domain import State, TransitionRejected, initialize, transition
from claude_code_codex_review_loop.domain import events as ev
from claude_code_codex_review_loop.domain.commands import CodexPurpose, RequestCodexReview
from claude_code_codex_review_loop.domain.values import (
    Awaiting,
    BlockResolutionEvidence,
    Budget,
    OpaqueBinding,
    OpaqueFingerprint,
    OpaqueRef,
    OpaqueSnapshot,
    Progress,
    ProgressBlock,
    RecordKind,
)

_K = RecordKind


def _rewrite(resolution: BlockResolutionEvidence, **changes: object) -> BlockResolutionEvidence:
    from dataclasses import replace

    return replace(resolution, **changes)  # type: ignore[arg-type]


class TestW0Initialize:
    """W0: initializeの正常系（I9の失敗系列と対。AC-C01-03）。"""

    def test_preflight_ok_starts_first_review(self) -> None:
        ms, commands = initialize(ev.PreflightOk())
        assert ms.state is State.RUNNING_REVIEW
        assert ms.awaiting is Awaiting.CODEX_CODE_REVIEW
        assert commands == (RequestCodexReview(CodexPurpose.CODE_REVIEW),)

    def test_preflight_ng_returns_failed_without_commands(self) -> None:
        ms, commands = initialize(ev.PreflightNg())
        assert ms.state is State.FAILED
        assert commands == ()
        assert ms.recovery_to is None and ms.awaiting is None and ms.pending_record is None


class TestW1ReviewRounds:
    """W1: 既定3 review roundの開始から停止までの系列。roundの二重計上がない（AC-C01-09）。"""

    def test_three_rounds_then_limit_blocks_without_new_agent_command(self) -> None:
        ms = start()
        for i in range(3):
            # round開始（REVIEW_ROUND消費）: blocking -> fix -> re-review
            ms, _ = produced_verified(
                ms,
                _K.REVIEW_RESULT,
                f"rv-{i}",
                ev.ReviewBlockingVerified(evidence(_K.REVIEW_RESULT, f"rv-{i}"), report()),
            )
            assert ms.state is State.CHANGES_REQUESTED
            ms, _ = transition(ms, ev.FixStarted())
            # 同一roundの完了は判定のみ（CONTINUEで次のre-reviewへ。二重計上しない）
            ms, commands = produced_verified(
                ms, _K.FIX_RESULT, f"fx-{i}", ev.FixResultVerified(evidence(_K.FIX_RESULT, f"fx-{i}"), report())
            )
            assert ms.state is State.RUNNING_REVIEW
            assert names(commands) == ("RequestCodexReview",)
        # 4 round目の開始がLIMIT_REACHED -> BLOCKED、commandなし
        ms, commands = produced_verified(
            ms,
            _K.REVIEW_RESULT,
            "rv-limit",
            ev.ReviewBlockingVerified(evidence(_K.REVIEW_RESULT, "rv-limit"), report(Progress.LIMIT_REACHED)),
        )
        assert ms.state is State.BLOCKED
        assert commands == ()
        assert isinstance(ms.block, ProgressBlock)
        assert ms.block.budget is Budget.REVIEW_ROUND


class TestW2ClarificationBudget:
    """W2: 5回目turn開始の許可と6回目開始の停止。resubmitの共通counter（AC-C01-09）。"""

    def _one_turn(self, ms: object, i: int) -> object:
        ms, _ = produced_verified(
            ms,  # type: ignore[arg-type]
            _K.CLARIFICATION_QUESTION,
            f"cq-{i}",
            ev.ClarificationQuestionVerified(evidence(_K.CLARIFICATION_QUESTION, f"cq-{i}"), report()),
        )
        assert ms.state is State.CLARIFYING_REVIEW  # type: ignore[union-attr]
        ms, _ = produced_verified(
            ms,
            _K.CLARIFICATION_ANSWER,
            f"ca-{i}",
            ev.ClarificationConfirmedVerified(evidence(_K.CLARIFICATION_ANSWER, f"ca-{i}")),
        )
        assert ms.state is State.CHANGES_REQUESTED  # type: ignore[union-attr]
        return ms

    def test_fifth_turn_start_allowed_sixth_blocked(self) -> None:
        ms = to_changes_requested()
        for i in range(5):
            # 5回目のturn開始も許可される（C-10 / C-11がCONTINUEと判定する）
            ms = self._one_turn(ms, i)
        # 6回目の開始はLIMIT_REACHED -> BLOCKED
        ms2, commands = produced_verified(
            ms,  # type: ignore[arg-type]
            _K.CLARIFICATION_QUESTION,
            "cq-6",
            ev.ClarificationQuestionVerified(
                evidence(_K.CLARIFICATION_QUESTION, "cq-6"), report(Progress.LIMIT_REACHED)
            ),
        )
        assert ms2.state is State.BLOCKED and commands == ()
        assert isinstance(ms2.block, ProgressBlock)
        assert ms2.block.budget is Budget.CLARIFICATION_TURN

    def test_resubmit_shares_the_common_counter(self) -> None:
        """5 turn消費後のresubmitが共通counterでBLOCKEDになる。"""
        ms = to_applying_fixes()
        ms, _ = produced_verified(
            ms, _K.DECISION_REQUEST, "dr-1", ev.DecisionRequestVerified(evidence(_K.DECISION_REQUEST, "dr-1"))
        )
        assert ms.state is State.REVIEWING_DECISION_REQUEST
        # 共通counterの消費済みをC-11がLIMIT_REACHEDとして判定した場合、resubmitはblockされる
        ms, commands = produced_verified(
            ms,
            _K.DECISION_VERDICT,
            "dv-1",
            ev.VerdictResubmitVerified(evidence(_K.DECISION_VERDICT, "dv-1"), report(Progress.LIMIT_REACHED)),
        )
        assert ms.state is State.BLOCKED and commands == ()
        assert isinstance(ms.block, ProgressBlock)
        assert ms.block.budget is Budget.CLARIFICATION_TURN


class TestW3LoopExitResults:
    """W3: loopを終了する結果は上限turnでも常に処理される（AC-C01-09）。"""

    @pytest.mark.parametrize(
        ("event_factory", "to_state"),
        [
            (
                lambda b: ev.ClarificationConfirmedVerified(evidence(_K.CLARIFICATION_ANSWER, b)),
                State.CHANGES_REQUESTED,
            ),
            (
                lambda b: ev.ClarificationRevisedVerified(evidence(_K.CLARIFICATION_ANSWER, b)),
                State.CHANGES_REQUESTED,
            ),
            (lambda b: ev.ClarificationWithdrawnVerified(evidence(_K.CLARIFICATION_ANSWER, b)), State.RUNNING_REVIEW),
            (
                lambda b: ev.ClarificationEscalatedVerified(evidence(_K.CLARIFICATION_ANSWER, b)),
                State.REVIEWING_DECISION_REQUEST,
            ),
        ],
    )
    def test_clarification_outcomes_always_processed(self, event_factory, to_state) -> None:  # type: ignore[no-untyped-def]
        ms = to_changes_requested()
        ms, _ = produced_verified(
            ms,
            _K.CLARIFICATION_QUESTION,
            "cq-1",
            ev.ClarificationQuestionVerified(evidence(_K.CLARIFICATION_QUESTION, "cq-1"), report()),
        )
        ms, _ = produced_verified(ms, _K.CLARIFICATION_ANSWER, "ca-1", event_factory("ca-1"))
        assert ms.state is to_state

    def test_verdict_ask_user_and_proceed_processed_without_progress(self) -> None:
        ms = to_applying_fixes()
        ms, _ = produced_verified(
            ms, _K.DECISION_REQUEST, "dr-1", ev.DecisionRequestVerified(evidence(_K.DECISION_REQUEST, "dr-1"))
        )
        ms_ask, commands = produced_verified(
            ms, _K.DECISION_VERDICT, "dv-a", ev.VerdictAskUserVerified(evidence(_K.DECISION_VERDICT, "dv-a"))
        )
        assert ms_ask.state is State.REVIEWING_DECISION_REQUEST
        assert names(commands) == ("RequestHostAction",)


class TestW4BlockedGate:
    """W4: 上限到達後のcommand不発行、単純resumeのBLOCKED維持、解消経路の完全binding一致（AC-C01-11）。"""

    def _resolution(self, block: ProgressBlock) -> BlockResolutionEvidence:
        return BlockResolutionEvidence(
            target_block_binding=block.binding,
            head=block.head,
            reason=block.reason,
            budget=block.budget,
            counter_snapshot=block.counter_snapshot,
            fingerprint=block.fingerprint,
        )

    def test_simple_resume_keeps_blocked_without_commands(self) -> None:
        ms = to_progress_blocked()
        ms2, commands = transition(ms, ev.ResumeValidated())
        assert ms2 == ms and commands == ()

    def test_limit_raise_replays_saved_continuation_once(self) -> None:
        ms = to_progress_blocked()
        block = ms.block
        assert isinstance(block, ProgressBlock)
        ms2, commands = transition(ms, ev.BlockResolvedLimitRaised(self._resolution(block)))
        assert ms2.state is State.CHANGES_REQUESTED
        assert ms2.awaiting is Awaiting.HOST_APPLY_FINDINGS
        assert names(commands) == ("RequestHostAction",)
        assert ms2.block is None
        # 消費済みblockへのreplayは拒否される
        with pytest.raises(TransitionRejected):
            transition(ms2, ev.BlockResolvedLimitRaised(self._resolution(block)))

    def test_ci_code_failure_block_replays_invalidate_and_apply(self) -> None:
        """CI系のblockでは、保存された継続にCMD_INVALIDATE_APPROVALSが含まれ再現される。"""
        ms = start()
        ms, _ = produced_verified(
            ms, _K.REVIEW_RESULT, "rv-1", ev.ReviewApprovedVerified(evidence(_K.REVIEW_RESULT, "rv-1"))
        )
        ms, commands = produced_verified(
            ms,
            _K.CI_CODE_FAILURE,
            "ci-1",
            ev.CiCodeFailureVerified(evidence(_K.CI_CODE_FAILURE, "ci-1"), report(Progress.LIMIT_REACHED)),
        )
        assert ms.state is State.BLOCKED and commands == ()
        block = ms.block
        assert isinstance(block, ProgressBlock)
        ms2, commands = transition(ms, ev.BlockResolvedLimitRaised(self._resolution(block)))
        assert ms2.state is State.CHANGES_REQUESTED
        assert names(commands) == ("InvalidateApprovals", "RequestHostAction")

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda r: _rewrite(r, target_block_binding=OpaqueBinding("other")),
            lambda r: _rewrite(r, head=OpaqueRef("other-head")),
            lambda r: _rewrite(r, counter_snapshot=OpaqueSnapshot("other")),
            lambda r: _rewrite(r, fingerprint=OpaqueFingerprint("other")),
            lambda r: _rewrite(r, reason=Progress.NO_PROGRESS),
        ],
    )
    def test_mismatched_resolution_is_rejected(self, mutate) -> None:  # type: ignore[no-untyped-def]
        ms = to_progress_blocked()
        block = ms.block
        assert isinstance(block, ProgressBlock)
        with pytest.raises(TransitionRejected):
            transition(ms, ev.BlockResolvedLimitRaised(mutate(self._resolution(block))))

    def test_fallback_discards_continuation_and_starts_fresh_review(self) -> None:
        ms = to_progress_blocked()
        ms2, commands = transition(ms, ev.ResumeFallbackRequired())
        assert ms2.state is State.RUNNING_REVIEW
        assert names(commands) == ("InvalidateApprovals", "RequestCodexReview")
        assert ms2.block is None

    def test_intervention_only_for_no_progress_block(self) -> None:
        """LIMIT_REACHEDのblockはlimit引き上げのみが解消経路で、interventionは拒否される。"""
        ms = to_progress_blocked(Progress.LIMIT_REACHED)
        block = ms.block
        assert isinstance(block, ProgressBlock)
        resolution = BlockResolutionEvidence(
            target_block_binding=block.binding,
            head=block.head,
            reason=block.reason,
            budget=block.budget,
            counter_snapshot=block.counter_snapshot,
            fingerprint=block.fingerprint,
        )
        with pytest.raises(TransitionRejected):
            transition(ms, ev.BlockResolvedIntervention(resolution))

    def test_no_progress_block_resolved_by_intervention_two_routes(self) -> None:
        ms = to_progress_blocked(Progress.NO_PROGRESS)
        block = ms.block
        assert isinstance(block, ProgressBlock)
        resolution = BlockResolutionEvidence(
            target_block_binding=block.binding,
            head=block.head,
            record=evidence(_K.BLOCK_INTERVENTION, "bi-1"),
            reason=block.reason,
            budget=block.budget,
            counter_snapshot=block.counter_snapshot,
            fingerprint=block.fingerprint,
        )
        # 経路2: GitHub直接comment（pendingなしで直接受理）
        direct, commands = transition(ms, ev.BlockResolvedIntervention(resolution))
        assert direct.state is State.CHANGES_REQUESTED and names(commands) == ("RequestHostAction",)
        # 経路1: PowerShell転記（PRODUCED -> persist -> 解消event）
        ms1, commands = transition(ms, ev.RecordProduced(_K.BLOCK_INTERVENTION, binding("bi-1")))
        assert names(commands) == ("PersistRecord",)
        via_record, commands = transition(ms1, ev.BlockResolvedIntervention(resolution))
        assert via_record == direct


class TestW5DecisionConversationOrder:
    """W5: decision flowの会話順序。verdict確認前のbrief / record投稿は拒否される。"""

    def test_full_ask_user_flow_posts_individual_records(self) -> None:
        ms = to_applying_fixes()
        ms, _ = produced_verified(
            ms, _K.DECISION_REQUEST, "dr-1", ev.DecisionRequestVerified(evidence(_K.DECISION_REQUEST, "dr-1"))
        )
        ms, _ = produced_verified(
            ms, _K.DECISION_VERDICT, "dv-1", ev.VerdictAskUserVerified(evidence(_K.DECISION_VERDICT, "dv-1"))
        )
        ms, _ = produced_verified(
            ms, _K.DECISION_BRIEF, "db-1", ev.DecisionBriefVerified(evidence(_K.DECISION_BRIEF, "db-1"))
        )
        assert ms.state is State.AWAITING_USER_DECISION
        ms, commands = produced_verified(
            ms, _K.USER_DECISION, "ud-1", ev.UserDecisionVerified(evidence(_K.USER_DECISION, "ud-1"))
        )
        assert ms.state is State.APPLYING_FIXES and names(commands) == ("RequestHostAction",)

    def test_brief_before_verdict_is_rejected(self) -> None:
        ms = to_applying_fixes()
        ms, _ = produced_verified(
            ms, _K.DECISION_REQUEST, "dr-1", ev.DecisionRequestVerified(evidence(_K.DECISION_REQUEST, "dr-1"))
        )
        # verdict待ち（awaiting = CODEX_DECISION_VERDICT）でのbrief / record投稿は順序違反
        with pytest.raises(TransitionRejected):
            transition(ms, ev.RecordProduced(_K.DECISION_BRIEF, binding("db-x")))
        with pytest.raises(TransitionRejected):
            transition(ms, ev.RecordProduced(_K.DECISION_RECORD, binding("dc-x")))

    def test_proceed_and_resubmit_paths(self) -> None:
        ms = to_applying_fixes()
        ms, _ = produced_verified(
            ms, _K.DECISION_REQUEST, "dr-1", ev.DecisionRequestVerified(evidence(_K.DECISION_REQUEST, "dr-1"))
        )
        proceed, _ = produced_verified(
            ms, _K.DECISION_VERDICT, "dv-p", ev.VerdictProceedVerified(evidence(_K.DECISION_VERDICT, "dv-p"))
        )
        proceed, _ = produced_verified(
            proceed, _K.DECISION_RECORD, "dc-1", ev.DecisionRecordVerified(evidence(_K.DECISION_RECORD, "dc-1"))
        )
        assert proceed.state is State.APPLYING_FIXES
        resubmit, _ = produced_verified(
            ms, _K.DECISION_VERDICT, "dv-r", ev.VerdictResubmitVerified(evidence(_K.DECISION_VERDICT, "dv-r"), report())
        )
        assert resubmit.awaiting is Awaiting.HOST_REVISE_DECISION_REQUEST
        resubmit, commands = produced_verified(
            resubmit, _K.DECISION_REQUEST, "dr-2", ev.DecisionRequestVerified(evidence(_K.DECISION_REQUEST, "dr-2"))
        )
        assert resubmit.state is State.REVIEWING_DECISION_REQUEST
        assert names(commands) == ("RequestCodexReview",)


class TestPermissionAndHeadChange:
    """permission停止からの復帰と、CI待ち / gate中のhead変更失効。"""

    def test_permission_block_and_resume_from_review(self) -> None:
        ms = start()
        ms, _ = produced_verified(
            ms, _K.PERMISSION_BLOCK, "pb-1", ev.ToolPermissionBlocked(evidence(_K.PERMISSION_BLOCK, "pb-1"))
        )
        assert ms.state is State.AWAITING_TOOL_PERMISSION and ms.return_to is State.RUNNING_REVIEW
        ms, commands = transition(ms, ev.PermissionResumeValidated())
        assert ms.state is State.RUNNING_REVIEW
        assert ms.awaiting is Awaiting.CODEX_CODE_REVIEW
        assert names(commands) == ("RequestCodexReview",)

    def test_permission_block_during_fixes_returns_to_fixes(self) -> None:
        ms = to_applying_fixes()
        ms, _ = produced_verified(
            ms, _K.PERMISSION_BLOCK, "pb-2", ev.ToolPermissionBlocked(evidence(_K.PERMISSION_BLOCK, "pb-2"))
        )
        assert ms.return_to is State.APPLYING_FIXES
        ms, commands = transition(ms, ev.PermissionResumeValidated())
        assert ms.state is State.APPLYING_FIXES and names(commands) == ("RequestHostAction",)

    def test_head_change_invalidates_and_restarts_review(self) -> None:
        ms = to_waiting_ci()
        ms2, commands = transition(ms, ev.HeadChangedExternally())
        assert ms2.state is State.RUNNING_REVIEW
        assert names(commands) == ("InvalidateApprovals", "RequestCodexReview")

    def test_ci_infra_failure_retries_and_timeout_waits(self) -> None:
        ms = to_waiting_ci()
        retried, commands = transition(ms, ev.CiInfraFailure())
        assert retried.state is State.WAITING_CI and names(commands) == ("CheckCi",)
        timed_out, commands = produced_verified(
            ms, _K.CI_TIMEOUT, "ct-1", ev.CiTimeoutRecorded(evidence(_K.CI_TIMEOUT, "ct-1"))
        )
        assert timed_out.state is State.WAITING_CI and timed_out.awaiting is None and commands == ()

    def test_gate_question_answer_and_changes(self) -> None:
        ms = to_gate()
        ms, _ = produced_verified(
            ms, _K.GATE_QUESTION, "gq-1", ev.GateQuestionVerified(evidence(_K.GATE_QUESTION, "gq-1"))
        )
        ms, _ = produced_verified(ms, _K.GATE_ANSWER, "ga-1", ev.GateAnswerVerified(evidence(_K.GATE_ANSWER, "ga-1")))
        assert ms.state is State.READY_FOR_HUMAN_MERGE and ms.awaiting is Awaiting.USER_INPUT_GATE
        ms, commands = produced_verified(
            ms, _K.GATE_CHANGES, "gc-1", ev.GateChangesVerified(evidence(_K.GATE_CHANGES, "gc-1"))
        )
        assert ms.state is State.CHANGES_REQUESTED
        assert names(commands) == ("InvalidateApprovals", "RequestHostAction")
