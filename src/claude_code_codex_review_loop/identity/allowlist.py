# SPDX-License-Identifier: Apache-2.0
"""allowlistとユーザー判断の受理（D-031、fail closed）。

ユーザー決定として受理するcommentは、設定したGitHub login allowlistとの完全一致に限る。
`authorAssociation`とrepository permissionは補助条件であり判定に使わない（D-031）。
allowlistが未設定または取得できない場合は`AllowlistUnavailable`として受理しない
（AC-C06-02）。bot・login欠如・charset外は全てdeny側へ倒す（fail closed）。

- 受理はGitHubへの直接comment経路のexternal evidence（Phase 1計画 節5.2の2経路目）。
  受理してもPersistRecord（再投稿）は発行しない
- 受理したcommentのbody hashを記録し、編集・削除・binding不一致（head変更等）は
  `revalidate_user_decision`で失効させる
- bindingはC-06が期待contextから決定論的に導出する（同一comment・同一contextの再受理は
  同一binding = 冪等。contextが変われば別binding）。規約の正本はADR-0008
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum, unique

from ..domain.values import (
    USER_INPUT_RECORD_KINDS,
    OpaqueBinding,
    OpaqueRef,
    RecordEvidence,
    RecordKind,
)
from ..transport.conversation import UnverifiedComment
from ..transport.marker import MARKER_TOKEN
from .actor import ActorClass, resolve_actor
from .errors import IdentityError


def _normalized_logins(stage: str, raw_logins: Iterable[str]) -> frozenset[str]:
    """設定されたlogin集合の検証と正規化。charset外のentryは設定誤り（IdentityError）。"""
    normalized = set()
    for raw in raw_logins:
        resolved = resolve_actor(raw)
        if resolved.klass is not ActorClass.USER or resolved.login is None:
            raise IdentityError(stage, f"allowlistのentryがGitHub loginとして不正: {raw!r}")
        normalized.add(resolved.login)
    return frozenset(normalized)


@dataclass(frozen=True)
class DecisionAllowlist:
    """ユーザー判断受理用のlogin集合（D-031）。構築時に正規化する。空集合は常にdeny。"""

    logins: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "logins", _normalized_logins("allowlist", self.logins))


@dataclass(frozen=True)
class AllowlistUnavailable:
    """allowlistが未設定または取得不能（AC-C06-02）。detailは診断用で判定に使わない。"""

    detail: str


DecisionAllowlistState = DecisionAllowlist | AllowlistUnavailable


@dataclass(frozen=True)
class ProducerAllowlist:
    """内部record（chain）の正当な投稿者集合（通常はControllerの認証login 1件）。

    空はviolationではなく設定誤りとして構築時に拒否する: 空集合で検証を走らせると
    全recordが「不正actor」になり、設定誤りを改ざん証拠として捏造してしまう。
    """

    logins: frozenset[str]

    def __post_init__(self) -> None:
        if not self.logins:
            raise IdentityError("producer", "producer allowlistは空にできない")
        object.__setattr__(self, "logins", _normalized_logins("producer", self.logins))


@dataclass(frozen=True)
class DecisionContext:
    """承認bindの期待値（intent・対象・head。D-031「承認はintent、repository、番号、head SHA、
    merge method、candidate fingerprintへbindする」）。kindはuser-input record種別に限る。"""

    kind: RecordKind
    repository: str
    number: int
    head_sha: str
    merge_method: str | None = None
    candidate_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in USER_INPUT_RECORD_KINDS:
            raise IdentityError("context", f"user-input record種別ではない: {self.kind.value}")


@unique
class DecisionRejection(Enum):
    """受理拒否の型付き理由。全てdeny側の終端であり、補助条件へのfallbackはない。"""

    ALLOWLIST_UNAVAILABLE = "ALLOWLIST_UNAVAILABLE"
    NOT_IN_ALLOWLIST = "NOT_IN_ALLOWLIST"
    BOT_ACTOR = "BOT_ACTOR"
    INVALID_LOGIN = "INVALID_LOGIN"
    MISSING_ACTOR = "MISSING_ACTOR"
    EDITED = "EDITED"
    ALREADY_CONSUMED = "ALREADY_CONSUMED"
    EMBEDDED_MARKER = "EMBEDDED_MARKER"


@dataclass(frozen=True)
class AcceptedUserDecision:
    """受理済みユーザー判断（external evidence）。bindingとbody hashが失効照合の基準になる。"""

    binding: OpaqueBinding
    evidence: RecordEvidence
    comment_id: str
    author_login: str
    body: str
    body_hash: str
    head_sha: str


@dataclass(frozen=True)
class RejectedUserDecision:
    """受理拒否。reasonは型付き、detailは診断用の説明（本文を含めない）。"""

    reason: DecisionRejection
    comment_id: str
    detail: str


def _derive_binding(context: DecisionContext, comment_id: str) -> str:
    """受理bindingの決定論的導出。contextとcomment IDのみに依存する（冪等）。"""
    method = context.merge_method if context.merge_method is not None else "-"
    fingerprint = context.candidate_fingerprint if context.candidate_fingerprint is not None else "-"
    return (
        f"ud:{context.kind.value}:{context.repository}#{context.number}"
        f":{context.head_sha}:{method}:{fingerprint}:c{comment_id}"
    )


def accept_user_decision(
    comment: UnverifiedComment,
    *,
    allowlist: DecisionAllowlistState,
    context: DecisionContext,
    consumed_comment_ids: frozenset[str],
) -> AcceptedUserDecision | RejectedUserDecision:
    """GitHub直接commentをユーザー判断として受理する（D-031完全一致、fail closed）。

    検査は取得不能 -> actor -> allowlist -> 編集 -> 消費済み -> 埋め込みtokenの順で、
    最初の違反で型付き理由を返す。受理はexternal evidenceでありPersistRecordを発行しない。
    """
    if isinstance(allowlist, AllowlistUnavailable):
        return RejectedUserDecision(
            reason=DecisionRejection.ALLOWLIST_UNAVAILABLE,
            comment_id=comment.comment_id,
            detail=f"allowlistが利用できない: {allowlist.detail}",
        )
    actor = resolve_actor(comment.author_login)
    if actor.klass is ActorClass.MISSING:
        return RejectedUserDecision(
            reason=DecisionRejection.MISSING_ACTOR, comment_id=comment.comment_id, detail="author loginがない"
        )
    if actor.klass is ActorClass.BOT:
        return RejectedUserDecision(
            reason=DecisionRejection.BOT_ACTOR, comment_id=comment.comment_id, detail="bot accountは受理しない"
        )
    if actor.klass is ActorClass.INVALID or actor.login is None:
        return RejectedUserDecision(
            reason=DecisionRejection.INVALID_LOGIN, comment_id=comment.comment_id, detail="loginのcharsetが不正"
        )
    if actor.login not in allowlist.logins:
        return RejectedUserDecision(
            reason=DecisionRejection.NOT_IN_ALLOWLIST,
            comment_id=comment.comment_id,
            detail="allowlistと完全一致しない",
        )
    if comment.updated_at != comment.created_at:
        return RejectedUserDecision(
            reason=DecisionRejection.EDITED, comment_id=comment.comment_id, detail="編集済みcommentは受理しない"
        )
    if comment.comment_id in consumed_comment_ids:
        return RejectedUserDecision(
            reason=DecisionRejection.ALREADY_CONSUMED,
            comment_id=comment.comment_id,
            detail="消費済みcommentの再提示",
        )
    if MARKER_TOKEN in comment.body.upper():
        return RejectedUserDecision(
            reason=DecisionRejection.EMBEDDED_MARKER,
            comment_id=comment.comment_id,
            detail="予約markerを含むユーザーcommentは受理しない",
        )
    binding = OpaqueBinding(_derive_binding(context, comment.comment_id))
    return AcceptedUserDecision(
        binding=binding,
        evidence=RecordEvidence(kind=context.kind, binding=binding, ref=OpaqueRef(comment.url)),
        comment_id=comment.comment_id,
        author_login=actor.login,
        body=comment.body,
        body_hash=comment.body_hash,
        head_sha=context.head_sha,
    )


@unique
class DecisionValidity(Enum):
    """受理済み判断の再検証結果。VALID以外は失効（silent repairしない）。"""

    VALID = "VALID"
    VOIDED_EDITED = "VOIDED_EDITED"
    VOIDED_DELETED = "VOIDED_DELETED"
    VOIDED_BINDING_MISMATCH = "VOIDED_BINDING_MISMATCH"


def revalidate_user_decision(
    accepted: AcceptedUserDecision,
    *,
    current: UnverifiedComment | None,
    expected: DecisionContext,
) -> DecisionValidity:
    """受理済み判断の失効検証。currentはGitHub再取得の結果（None = 404 = 削除）。

    編集（body hash差または編集timestamp）と削除は失効。expectedが受理時のcontextと
    異なる場合（head変更等）はbinding不一致として失効する（head binding不変条件）。
    """
    if current is None:
        return DecisionValidity.VOIDED_DELETED
    if current.body_hash != accepted.body_hash or current.updated_at != current.created_at:
        return DecisionValidity.VOIDED_EDITED
    if _derive_binding(expected, accepted.comment_id) != accepted.binding.value:
        return DecisionValidity.VOIDED_BINDING_MISMATCH
    return DecisionValidity.VALID
