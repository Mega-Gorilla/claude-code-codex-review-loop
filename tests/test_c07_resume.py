# SPDX-License-Identifier: Apache-2.0
"""resume contextの組み立ての受入test（**AC-C07-01**。ADR-0013）。

段階ごとの停止（run選択 / integrity / head / pending / 直接回答）と、artifact不一致を
停止ではなくcache破棄として載せることを固定する。判定はすべて注入した観測だけで決まる。
"""

from __future__ import annotations

import hashlib

import pytest
from c06_support.helpers import HEAD, make_comment
from c07_support.helpers import (
    NUMBER,
    REPOSITORY,
    RUN,
    approved_review_payload,
    chain_comments_of,
    checkpoint_payload,
    conversation_section,
    pending_fixture,
    verified_chain,
)

from claude_code_codex_review_loop.domain.states import State
from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.identity import (
    AllowlistUnavailable,
    DecisionAllowlist,
    DecisionContext,
    IdentityError,
)
from claude_code_codex_review_loop.state import (
    ArtifactStatus,
    CheckpointLoaded,
    DirectAnswerAccepted,
    PendingAbsent,
    PendingReissueRequired,
    ResumeContext,
    ResumeObservation,
    ResumeStage,
    ResumeStopped,
    ResumeVerdict,
    RunSummary,
    build_resume_context,
    read_chain_checkpoint,
)
from claude_code_codex_review_loop.transport.conversation import UnverifiedComment, body_hash_of
from claude_code_codex_review_loop.transport.pull_request import UnverifiedPullRequest

_K = RecordKind
_NEW_HEAD = "b" * 40
_BASE = "c" * 40
_CONTENT = "artifactの中身"
_CONTENT_HASH = hashlib.sha256(_CONTENT.encode("utf-8")).hexdigest()
_ALLOWLIST = DecisionAllowlist(logins=frozenset({"Mega-Gorilla"}))
_CONTEXT = DecisionContext(
    kind=_K.MERGE_APPROVAL, repository=REPOSITORY, number=NUMBER, head_sha=HEAD, merge_method="merge"
)


def _pull(head: str = HEAD) -> UnverifiedPullRequest:
    return UnverifiedPullRequest(
        number=NUMBER,
        state="open",
        merged=False,
        head_sha=head,
        head_ref="topic",
        head_repository=REPOSITORY,
        base_sha=_BASE,
        base_ref="main",
        base_repository=REPOSITORY,
        author_login="alice",
        updated_at="2026-08-24T09:00:00Z",
    )


def _loaded(**sections: object) -> CheckpointLoaded:
    return CheckpointLoaded(payload=checkpoint_payload(**sections), version=1)


def _digest(run_id: str, path: str) -> str | None:
    return _CONTENT_HASH if path == "review.json" else None


def _observation(
    *,
    summaries: tuple[RunSummary, ...] | None = None,
    pull: UnverifiedPullRequest | None = None,
    comments: tuple[UnverifiedComment, ...] = (),
    decision_context: DecisionContext | None = None,
    allowlist: DecisionAllowlist | AllowlistUnavailable | None = None,
) -> ResumeObservation:
    if summaries is None:
        summaries = (RunSummary(run_id=RUN, verification=verified_chain([_K.REVIEW_RESULT])),)
    return ResumeObservation(
        repository=REPOSITORY,
        number=NUMBER,
        pull=pull if pull is not None else _pull(),
        comments=comments,
        summaries=summaries,
        artifact_digest=_digest,
        decision_context=decision_context,
        decision_allowlist=allowlist,
    )


def _context(observation: ResumeObservation) -> ResumeContext:
    result = build_resume_context(observation)
    assert isinstance(result, ResumeContext)
    return result


def _stopped(observation: ResumeObservation) -> ResumeStopped:
    result = build_resume_context(observation)
    assert isinstance(result, ResumeStopped)
    return result


class TestObservationConstruction:
    def test_decision_context_requires_an_allowlist(self) -> None:
        with pytest.raises(IdentityError):
            _observation(decision_context=_CONTEXT)


class TestResumeContext:
    def test_assembles_from_verified_records(self) -> None:
        context = _context(_observation())
        assert (context.run_id, context.repository, context.number) == (RUN, REPOSITORY, NUMBER)
        assert context.verdict is ResumeVerdict.VALIDATED
        assert [record.seq for record in context.records] == [1]
        assert context.next_seq == 2
        assert isinstance(context.pending, PendingAbsent)
        assert context.artifacts == () and context.direct_answer is None

    def test_fresh_run_without_github_records(self) -> None:
        """checkpointだけがあるrun（未投稿）。次のseqは1。"""
        summaries = (RunSummary(run_id=RUN, checkpoint=_loaded()),)
        context = _context(_observation(summaries=summaries))
        assert context.records == () and context.next_seq == 1

    def test_selected_run_is_found_among_several_summaries(self) -> None:
        """選択されたrunが先頭のsummaryとは限らない（terminalな候補が先にある場合）。"""
        summaries = (
            RunSummary(
                run_id="run-terminal", verification=verified_chain([_K.USER_CANCEL], run_id="run-terminal")
            ),
            RunSummary(run_id=RUN, verification=verified_chain([_K.REVIEW_RESULT])),
        )
        assert _context(_observation(summaries=summaries)).run_id == RUN

    def test_head_reconciliation_is_carried(self) -> None:
        """head変更はcontextの判定として載る（停止ではない）。"""
        summaries = (
            RunSummary(
                run_id=RUN,
                verification=verified_chain([_K.REVIEW_RESULT], payloads={1: approved_review_payload()}),
                checkpoint=_loaded(heads={"observed_sha": HEAD}),
            ),
        )
        context = _context(_observation(summaries=summaries, pull=_pull(_NEW_HEAD)))
        assert context.verdict is ResumeVerdict.FALLBACK_REQUIRED
        assert len(context.head.voided_approvals) == 1


class TestPendingWiring:
    def _summaries(self, **overrides: object) -> tuple[RunSummary, ...]:
        comments = chain_comments_of([_K.REVIEW_RESULT])
        fixture = pending_fixture(seq=2, prev=body_hash_of(comments[0].body))
        payload: dict[str, object] = {"transaction": fixture.transaction}
        payload.update(overrides)
        return (
            RunSummary(
                run_id=RUN,
                verification=verified_chain([_K.REVIEW_RESULT]),
                checkpoint=_loaded(**payload),
            ),
        )

    def test_pending_directive_is_carried(self) -> None:
        context = _context(_observation(summaries=self._summaries()))
        assert isinstance(context.pending, PendingReissueRequired)
        assert context.pending.transaction.seq == 2

    def test_uninterpretable_transaction_stops(self) -> None:
        stopped = _stopped(_observation(summaries=self._summaries(transaction={"binding": ""})))
        assert stopped.stage is ResumeStage.PENDING and stopped.run_id == RUN

    def test_undecidable_reissue_stops(self) -> None:
        """直前seqがchainに無い等、再発行の可否を決められない場合は停止する。"""
        comments = chain_comments_of([_K.REVIEW_RESULT] * 2)
        fixture = pending_fixture(seq=3, prev=body_hash_of(comments[1].body))
        summaries = (
            RunSummary(
                run_id=RUN,
                verification=verified_chain([_K.REVIEW_RESULT]),
                checkpoint=_loaded(transaction=fixture.transaction),
            ),
        )
        stopped = _stopped(_observation(summaries=summaries))
        assert stopped.stage is ResumeStage.PENDING and "直前" in stopped.detail


class TestArtifactWiring:
    def _summaries(self, path: str, head: str = HEAD) -> tuple[RunSummary, ...]:
        verification = verified_chain([_K.REVIEW_RESULT])
        record = verification.records[0]
        return (
            RunSummary(
                run_id=RUN,
                verification=verification,
                checkpoint=_loaded(
                    artifact_records=[
                        {
                            "path": path,
                            "kind": "REVIEW_RESULT",
                            "content_hash": _CONTENT_HASH,
                            "approved_head_sha": head,
                            "record_binding": record.key,
                            "comment_id": record.comment_id,
                        }
                    ]
                ),
            ),
        )

    def test_bound_artifact_is_usable(self) -> None:
        context = _context(_observation(summaries=self._summaries("review.json")))
        assert [check.status for check in context.artifacts] == [ArtifactStatus.BOUND]
        assert len(context.usable_artifacts) == 1

    def test_mismatch_is_discarded_without_stopping(self) -> None:
        """artifactの不一致は停止ではなくcacheの破棄（GitHub側が上位）。"""
        context = _context(_observation(summaries=self._summaries("missing.json")))
        assert [check.status for check in context.artifacts] == [ArtifactStatus.MISSING]
        assert context.usable_artifacts == ()


class TestDirectAnswerWiring:
    def _observation_with(self, *answers: UnverifiedComment) -> ResumeObservation:
        return _observation(
            summaries=(RunSummary(run_id=RUN, verification=verified_chain([_K.REVIEW_RESULT])),),
            comments=chain_comments_of([_K.REVIEW_RESULT]) + answers,
            decision_context=_CONTEXT,
            allowlist=_ALLOWLIST,
        )

    def _answer(self, comment_id: int, *, created_at: str = "2026-08-24T11:00:00Z") -> UnverifiedComment:
        return make_comment(
            comment_id,
            "approve",
            author="mega-gorilla",
            created_at=created_at,
            repository=REPOSITORY,
            number=NUMBER,
        )

    def test_accepted_answer_is_carried(self) -> None:
        context = _context(self._observation_with(self._answer(3001)))
        assert isinstance(context.direct_answer, DirectAnswerAccepted)
        assert context.direct_answer.decision.comment_id == "3001"

    def test_multiple_answers_stop(self) -> None:
        stopped = _stopped(
            self._observation_with(
                self._answer(3001), self._answer(3002, created_at="2026-08-24T12:00:00Z")
            )
        )
        assert stopped.stage is ResumeStage.DIRECT_ANSWER and "2件" in stopped.detail

    def test_unavailable_allowlist_stops(self) -> None:
        observation = _observation(
            comments=(self._answer(3001),),
            decision_context=_CONTEXT,
            allowlist=AllowlistUnavailable(detail="未設定"),
        )
        stopped = _stopped(observation)
        assert stopped.stage is ResumeStage.DIRECT_ANSWER and stopped.detail == "未設定"

    def test_answers_before_the_latest_record_are_not_considered(self) -> None:
        """境界は最新の検証済みrecordのcreated_at（構造的規則）。"""
        context = _context(self._observation_with(self._answer(3001, created_at="2026-08-24T09:00:00Z")))
        assert not isinstance(context.direct_answer, DirectAnswerAccepted)


class TestStops:
    def test_ambiguous_run_stops(self) -> None:
        summaries = (
            RunSummary(run_id="run-a", verification=verified_chain([_K.REVIEW_RESULT])),
            RunSummary(run_id="run-b", verification=verified_chain([_K.REVIEW_RESULT], run_id="run-b")),
        )
        stopped = _stopped(_observation(summaries=summaries))
        assert stopped.stage is ResumeStage.RUN_SELECTION and stopped.run_id is None

    def test_unreadable_checkpoint_stops_with_run_id(self) -> None:
        from claude_code_codex_review_loop.state.store import CheckpointUnreadable

        summaries = (
            RunSummary(run_id=RUN, checkpoint=CheckpointUnreadable(path=None, detail="x")),  # type: ignore[arg-type]
        )
        stopped = _stopped(_observation(summaries=summaries))
        assert stopped.stage is ResumeStage.RUN_SELECTION and stopped.run_id == RUN

    def test_chain_violation_stops(self) -> None:
        verification = verified_chain([_K.REVIEW_RESULT], author="attacker")
        assert not verification.is_intact
        summaries = (RunSummary(run_id=RUN, verification=verification),)
        stopped = _stopped(_observation(summaries=summaries))
        assert stopped.stage is ResumeStage.INTEGRITY and "violation" in stopped.detail

    def test_unobservable_head_stops(self) -> None:
        stopped = _stopped(_observation(pull=_pull("abc")))
        assert stopped.stage is ResumeStage.HEAD

    def test_unconfirmed_merge_approval_stops(self) -> None:
        """`MERGE_FAILED`で現headの承認を確認できない場合（ADR-0012 決定13）。"""
        summaries = (
            RunSummary(
                run_id=RUN,
                verification=verified_chain([_K.FIX_RESULT]),
                checkpoint=_loaded(
                    heads={"observed_sha": HEAD, "approved_sha": HEAD},
                    state={"state": State.MERGE_FAILED.value},
                ),
            ),
        )
        stopped = _stopped(_observation(summaries=summaries))
        assert stopped.stage is ResumeStage.HEAD and "承認" in stopped.detail


class TestReadChainCheckpoint:
    def test_builds_from_conversation(self) -> None:
        comments = chain_comments_of([_K.REVIEW_RESULT] * 2)
        checkpoint = read_chain_checkpoint(checkpoint_payload(conversation=conversation_section(comments)))
        assert checkpoint is not None
        assert checkpoint.high_water_mark == 2
        assert [known.seq for known in checkpoint.known_records] == [1, 2]

    @pytest.mark.parametrize(
        "payload",
        [{}, {"conversation": "x"}, {"conversation": {}}, {"conversation": {"high_water_mark": True}}],
        ids=["absent", "not_object", "no_high_water", "bool_high_water"],
    )
    def test_absent_high_water_mark_means_fresh_resume(self, payload: dict[str, object]) -> None:
        assert read_chain_checkpoint(payload) is None

    @pytest.mark.parametrize(
        "entry",
        [
            "not-an-object",
            {"comment_id": "1", "seq": 0, "body_hash": "h"},
            {"comment_id": "1", "seq": 5, "body_hash": "h"},
            {"comment_id": "1", "seq": True, "body_hash": "h"},
            {"comment_id": 1, "seq": 1, "body_hash": "h"},
            {"comment_id": "1", "seq": 1},
        ],
        ids=["not_object", "seq_zero", "beyond_high_water", "seq_bool", "id_not_str", "no_hash"],
    )
    def test_unusable_entries_are_not_known_records(self, entry: object) -> None:
        checkpoint = read_chain_checkpoint(
            {"conversation": {"high_water_mark": 2, "records": [entry]}}
        )
        assert checkpoint is not None and checkpoint.known_records == ()

    def test_non_list_records_are_ignored(self) -> None:
        checkpoint = read_chain_checkpoint({"conversation": {"high_water_mark": 1, "records": "x"}})
        assert checkpoint is not None and checkpoint.known_records == ()
