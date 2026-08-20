# SPDX-License-Identifier: Apache-2.0
"""C系列: canonical record persistence（AC-C01-03）。"""

from __future__ import annotations

import pytest
from c01_support.helpers import binding, evidence, names, start, to_gate

from claude_code_codex_review_loop.domain import State, TransitionRejected, transition
from claude_code_codex_review_loop.domain import events as ev
from claude_code_codex_review_loop.domain.values import Awaiting, RecordKind

_K = RecordKind


class TestC1RecordGate:
    """C1: PRODUCED -> 冪等persist -> VERIFIEDの正常系と各種拒否。"""

    def test_produced_then_verified_normal_path(self) -> None:
        ms = start()
        ms, commands = transition(ms, ev.RecordProduced(_K.REVIEW_RESULT, binding("r-1")))
        assert names(commands) == ("PersistRecord",)
        assert ms.awaiting is None and ms.pending_record is not None
        ms, _ = transition(ms, ev.ReviewApprovedVerified(evidence(_K.REVIEW_RESULT, "r-1")))
        assert ms.state is State.WAITING_CI and ms.pending_record is None

    def test_verified_without_produced_is_rejected(self) -> None:
        ms = start()
        with pytest.raises(TransitionRejected):
            transition(ms, ev.ReviewApprovedVerified(evidence(_K.REVIEW_RESULT, "r-x")))

    def test_binding_mismatch_is_rejected(self) -> None:
        ms = start()
        ms, _ = transition(ms, ev.RecordProduced(_K.REVIEW_RESULT, binding("r-1")))
        with pytest.raises(TransitionRejected):
            transition(ms, ev.ReviewApprovedVerified(evidence(_K.REVIEW_RESULT, "r-other")))

    def test_consumed_evidence_cannot_be_replayed(self) -> None:
        ms = start()
        ms, _ = transition(ms, ev.RecordProduced(_K.REVIEW_RESULT, binding("r-1")))
        ms, _ = transition(ms, ev.ReviewApprovedVerified(evidence(_K.REVIEW_RESULT, "r-1")))
        with pytest.raises(TransitionRejected):
            transition(ms, ev.ReviewApprovedVerified(evidence(_K.REVIEW_RESULT, "r-1")))

    def test_pending_blocks_other_semantic_events(self) -> None:
        """単一pendingの保持中は、当該手続きを進めるevent以外のsemantic eventを拒否する。"""
        ms = start()
        ms, _ = transition(ms, ev.RecordProduced(_K.REVIEW_RESULT, binding("r-1")))
        with pytest.raises(TransitionRejected):
            transition(ms, ev.RecordProduced(_K.REVIEW_RESULT, binding("r-2")))
        with pytest.raises(TransitionRejected):
            transition(ms, ev.HeadChangedExternally())

    def test_produced_requires_matching_awaiting(self) -> None:
        """順序を飛ばしたPRODUCED（awaiting不一致）は拒否される。"""
        ms = start()  # awaiting = CODEX_CODE_REVIEW
        with pytest.raises(TransitionRejected):
            transition(ms, ev.RecordProduced(_K.FIX_RESULT, binding("fx-1")))


class TestC2PartialTurn:
    """C2: partial turn（pending保持）からのresumeは永続化確認の再発行のみを返す。"""

    def test_resume_with_pending_reissues_persist_only(self) -> None:
        ms = start()
        ms, _ = transition(ms, ev.RecordProduced(_K.REVIEW_RESULT, binding("r-1")))
        resumed, commands = transition(ms, ev.ResumeValidated())
        assert resumed.state is State.RUNNING_REVIEW
        assert resumed.pending_record == ms.pending_record
        assert names(commands) == ("PersistRecord",)
        # 再開後も同一turnとしてVERIFIEDを受理できる
        done, _ = transition(resumed, ev.ReviewApprovedVerified(evidence(_K.REVIEW_RESULT, "r-1")))
        assert done.state is State.WAITING_CI


class TestC3TwoRoutes:
    """C3: user-input recordの2経路が同一のsemantic遷移へ合流する。"""

    @pytest.mark.parametrize(
        ("kind", "make_event", "expected_state"),
        [
            (
                _K.GATE_QUESTION,
                lambda b: ev.GateQuestionVerified(evidence(_K.GATE_QUESTION, b)),
                State.READY_FOR_HUMAN_MERGE,
            ),
            (_K.GATE_CHANGES, lambda b: ev.GateChangesVerified(evidence(_K.GATE_CHANGES, b)), State.CHANGES_REQUESTED),
            (_K.MERGE_APPROVAL, lambda b: ev.MergeApprovalVerified(evidence(_K.MERGE_APPROVAL, b)), State.MERGING),
        ],
    )
    def test_gate_records_converge(self, kind, make_event, expected_state) -> None:  # type: ignore[no-untyped-def]
        ms = to_gate()
        # 経路1: PRODUCED -> persist -> VERIFIED
        via_transcription, _ = transition(ms, ev.RecordProduced(kind, binding("u-1")))
        via_transcription, commands1 = transition(via_transcription, make_event("u-1"))
        # 経路2: GitHub直接comment（外部evidence。永続化commandなし）
        direct, commands2 = transition(ms, make_event("u-1"))
        assert via_transcription == direct
        assert via_transcription.state is expected_state
        assert commands1 == commands2
        assert "PersistRecord" not in names(commands2)

    def test_user_decision_converges(self) -> None:
        from c01_support.helpers import produced_verified, to_applying_fixes

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
        assert ms.awaiting is Awaiting.USER_INPUT_DECISION
        via_transcription, _ = transition(ms, ev.RecordProduced(_K.USER_DECISION, binding("ud-1")))
        via_transcription, _ = transition(
            via_transcription, ev.UserDecisionVerified(evidence(_K.USER_DECISION, "ud-1"))
        )
        direct, commands = transition(ms, ev.UserDecisionVerified(evidence(_K.USER_DECISION, "ud-1")))
        assert via_transcription == direct and direct.state is State.APPLYING_FIXES
        assert "PersistRecord" not in names(commands)
