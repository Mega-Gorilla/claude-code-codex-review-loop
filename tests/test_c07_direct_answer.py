# SPDX-License-Identifier: Apache-2.0
"""GitHub直接回答の候補列挙の受入test（**AC-C07-06** / D-021。ADR-0013）。

- 受理検証はC-06の`accept_user_decision`をそのまま使う（C-07は再実装しない）
- C-07が持つのは構造的な絞り込みだけ（markerつきcomment・`after`・消費済みIDの除外）
- **有効な候補が2件以上なら停止**し、「最新を採る」等の推測をしない
"""

from __future__ import annotations

from c06_support.helpers import HEAD, chain_comments, make_comment

from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.identity import (
    AllowlistUnavailable,
    DecisionAllowlist,
    DecisionContext,
    DecisionRejection,
)
from claude_code_codex_review_loop.state import (
    DirectAnswerAbsent,
    DirectAnswerAccepted,
    DirectAnswerAmbiguous,
    DirectAnswerUnavailable,
    enumerate_direct_answers,
)
from claude_code_codex_review_loop.transport.conversation import UnverifiedComment

_ALLOWLIST = DecisionAllowlist(logins=frozenset({"Mega-Gorilla"}))
_CONTEXT = DecisionContext(
    kind=RecordKind.MERGE_APPROVAL,
    repository="Mega-Gorilla/claude-code-codex-review-loop",
    number=42,
    head_sha=HEAD,
    merge_method="merge",
)
_AFTER = "2026-08-24T10:00:00Z"


def _answer(
    comment_id: int, *, author: str | None = "mega-gorilla", created_at: str = "2026-08-24T11:00:00Z"
) -> UnverifiedComment:
    return make_comment(comment_id, "approve", author=author, created_at=created_at)


def _enumerate(
    comments: tuple[UnverifiedComment, ...],
    *,
    allowlist: DecisionAllowlist | AllowlistUnavailable = _ALLOWLIST,
    consumed: frozenset[str] = frozenset(),
    after: str | None = _AFTER,
) -> object:
    return enumerate_direct_answers(
        comments,
        allowlist=allowlist,
        context=_CONTEXT,
        consumed_comment_ids=consumed,
        after=after,
    )


class TestEnumerateDirectAnswers:
    def test_single_valid_answer_is_accepted(self) -> None:
        outcome = _enumerate((_answer(3001),))
        assert isinstance(outcome, DirectAnswerAccepted)
        assert outcome.decision.comment_id == "3001" and outcome.rejected == ()

    def test_no_candidate_is_absent(self) -> None:
        assert isinstance(_enumerate(()), DirectAnswerAbsent)

    def test_multiple_valid_answers_stop(self) -> None:
        """**曖昧なら停止**（最新を採る等の推測をしない）。"""
        outcome = _enumerate((_answer(3001), _answer(3002, created_at="2026-08-24T12:00:00Z")))
        assert isinstance(outcome, DirectAnswerAmbiguous)
        assert [decision.comment_id for decision in outcome.decisions] == ["3001", "3002"]

    def test_unavailable_allowlist_stops_before_evaluating(self) -> None:
        """allowlistが未設定・取得不能なら候補を評価しない（fail closed。AC-C06-02）。"""
        outcome = _enumerate((_answer(3001),), allowlist=AllowlistUnavailable(detail="未設定"))
        assert isinstance(outcome, DirectAnswerUnavailable) and outcome.detail == "未設定"

    def test_controller_records_are_not_candidates(self) -> None:
        """予約markerを持つcomment（chain record）は候補にしない。"""
        outcome = _enumerate(chain_comments(2))
        assert isinstance(outcome, DirectAnswerAbsent) and outcome.rejected == ()

    def test_comments_before_the_boundary_are_not_candidates(self) -> None:
        """`after`（最新recordのcreated_at）以前のcommentは対象外。"""
        outcome = _enumerate((_answer(3001, created_at="2026-08-24T09:00:00Z"),))
        assert isinstance(outcome, DirectAnswerAbsent) and outcome.rejected == ()

    def test_boundary_is_exclusive(self) -> None:
        outcome = _enumerate((_answer(3001, created_at=_AFTER),))
        assert isinstance(outcome, DirectAnswerAbsent)

    def test_absent_boundary_considers_the_whole_window(self) -> None:
        outcome = _enumerate((_answer(3001, created_at="2026-08-01T00:00:00Z"),), after=None)
        assert isinstance(outcome, DirectAnswerAccepted)

    def test_consumed_comments_are_not_candidates(self) -> None:
        outcome = _enumerate((_answer(3001),), consumed=frozenset({"3001"}))
        assert isinstance(outcome, DirectAnswerAbsent) and outcome.rejected == ()

    def test_rejected_candidates_are_retained_with_reasons(self) -> None:
        """受理されなかった候補をsilentに捨てない（診断のため理由つきで保持する）。"""
        outcome = _enumerate((_answer(3001, author="outsider"),))
        assert isinstance(outcome, DirectAnswerAbsent)
        assert [candidate.reason for candidate in outcome.rejected] == [DecisionRejection.NOT_IN_ALLOWLIST]
        assert outcome.rejected[0].comment_id == "3001"

    def test_valid_answer_is_reported_with_rejected_ones(self) -> None:
        outcome = _enumerate((_answer(3001, author="outsider"), _answer(3002)))
        assert isinstance(outcome, DirectAnswerAccepted)
        assert outcome.decision.comment_id == "3002" and len(outcome.rejected) == 1

    def test_bot_actor_is_rejected_by_c06(self) -> None:
        """actor判定はC-06の責務（C-07は再実装しない）。"""
        outcome = _enumerate((_answer(3001, author="dependabot[bot]"),))
        assert isinstance(outcome, DirectAnswerAbsent)
        assert outcome.rejected[0].reason is DecisionRejection.BOT_ACTOR
