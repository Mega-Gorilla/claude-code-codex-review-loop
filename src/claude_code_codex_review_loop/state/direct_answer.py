# SPDX-License-Identifier: Apache-2.0
"""GitHub直接commentへ書かれたユーザー回答の候補列挙（AC-C07-06 / D-021。ADR-0013）。

resume時に「ユーザーがGitHubへ直接書いた回答」を取得する。**受理の検証はC-06の
`accept_user_decision`をそのまま使い**（allowlist完全一致・編集・消費済み・観測元・
埋め込みtoken）、本moduleは再実装しない。

C-07が持つのは**構造的な絞り込み**だけ:

- Controllerが投稿したrecord（予約markerを持つcomment）は候補にしない
- `after`（既定は最新の検証済みrecordのcreated_at）より後のcommentに限る
- 消費済みcomment IDを除く

「どの質問に対する回答か」という**意味解釈はC-11**の責務なので、期待するuser-input
record種別・head・fingerprintを表す`DecisionContext`は呼び出し側が注入する。

**有効な候補が2件以上なら停止する**（「最新を採る」等の推測をしない）。これは不変条件
「曖昧な肯定を承認と解釈しない」の具体化であり、正本を緩めない側の解釈である。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..identity.allowlist import (
    AcceptedUserDecision,
    AllowlistUnavailable,
    DecisionAllowlistState,
    DecisionContext,
    DecisionRejection,
    RejectedUserDecision,
    accept_user_decision,
)
from ..transport.conversation import UnverifiedComment
from ..transport.marker import MARKER_TOKEN


@dataclass(frozen=True)
class RejectedCandidate:
    """受理されなかった候補（理由つきで保持し、silentに捨てない）。"""

    comment_id: str
    reason: DecisionRejection
    detail: str


@dataclass(frozen=True)
class DirectAnswerAbsent:
    """受理できる回答が無い（rejectedは診断用）。"""

    rejected: tuple[RejectedCandidate, ...] = ()


@dataclass(frozen=True)
class DirectAnswerAccepted:
    """一意に受理できたユーザー回答（external evidence）。"""

    decision: AcceptedUserDecision
    rejected: tuple[RejectedCandidate, ...] = ()


@dataclass(frozen=True)
class DirectAnswerAmbiguous:
    """有効な候補が複数ある（推測せず停止し、候補を提示する）。"""

    decisions: tuple[AcceptedUserDecision, ...]


@dataclass(frozen=True)
class DirectAnswerUnavailable:
    """allowlistが未設定・取得不能で判定できない（fail closed。AC-C06-02）。"""

    detail: str


DirectAnswerOutcome = (
    DirectAnswerAbsent | DirectAnswerAccepted | DirectAnswerAmbiguous | DirectAnswerUnavailable
)


def _is_candidate(comment: UnverifiedComment, *, after: str | None, consumed: frozenset[str]) -> bool:
    """構造的な絞り込み（意味解釈をしない）。"""
    if MARKER_TOKEN in comment.body.upper():
        return False  # Controllerが投稿したrecord（chain対象）
    if comment.comment_id in consumed:
        return False
    return after is None or comment.created_at > after


def enumerate_direct_answers(
    comments: Sequence[UnverifiedComment],
    *,
    allowlist: DecisionAllowlistState,
    context: DecisionContext,
    consumed_comment_ids: frozenset[str] = frozenset(),
    after: str | None = None,
) -> DirectAnswerOutcome:
    """直接回答の候補を列挙し、C-06の受理判定を通す（pure）。

    `after`はcreated_atの下限（排他）。既定のNoneは「窓全体を候補にする」で、通常は
    最新の検証済みrecordのcreated_atを渡す。受理できた候補が2件以上なら停止する。
    """
    if isinstance(allowlist, AllowlistUnavailable):
        return DirectAnswerUnavailable(detail=allowlist.detail)
    accepted: list[AcceptedUserDecision] = []
    rejected: list[RejectedCandidate] = []
    for comment in comments:
        if not _is_candidate(comment, after=after, consumed=consumed_comment_ids):
            continue
        outcome = accept_user_decision(
            comment,
            allowlist=allowlist,
            context=context,
            consumed_comment_ids=consumed_comment_ids,
        )
        if isinstance(outcome, RejectedUserDecision):
            rejected.append(
                RejectedCandidate(
                    comment_id=outcome.comment_id, reason=outcome.reason, detail=outcome.detail
                )
            )
            continue
        accepted.append(outcome)
    if len(accepted) > 1:
        return DirectAnswerAmbiguous(decisions=tuple(accepted))
    if accepted:
        return DirectAnswerAccepted(decision=accepted[0], rejected=tuple(rejected))
    return DirectAnswerAbsent(rejected=tuple(rejected))
