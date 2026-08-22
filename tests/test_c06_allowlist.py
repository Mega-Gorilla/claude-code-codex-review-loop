# SPDX-License-Identifier: Apache-2.0
"""ユーザー判断受理の受入test（AC-C06-01 / 02。D-031完全一致・fail closed）。"""

from __future__ import annotations

import pytest
from c06_support.helpers import make_comment

from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.identity import (
    AcceptedUserDecision,
    AllowlistUnavailable,
    DecisionAllowlist,
    DecisionContext,
    DecisionRejection,
    DecisionValidity,
    IdentityError,
    ProducerAllowlist,
    RejectedUserDecision,
    accept_user_decision,
    revalidate_user_decision,
)

_ALLOWLIST = DecisionAllowlist(logins=frozenset({"Mega-Gorilla"}))
_CONTEXT = DecisionContext(
    kind=RecordKind.MERGE_APPROVAL,
    repository="Mega-Gorilla/claude-code-codex-review-loop",
    number=42,
    head_sha="a" * 40,
    merge_method="merge",
    candidate_fingerprint="fp-1",
)
_NO_CONSUMED: frozenset[str] = frozenset()


def _accept(
    comment_author: str | None = "mega-gorilla",
    *,
    allowlist: DecisionAllowlist | AllowlistUnavailable = _ALLOWLIST,
    body: str = "approve",
    created_at: str = "2026-08-21T10:00:00Z",
    updated_at: str | None = None,
    consumed: frozenset[str] = _NO_CONSUMED,
) -> AcceptedUserDecision | RejectedUserDecision:
    comment = make_comment(2001, body, author=comment_author, created_at=created_at, updated_at=updated_at)
    return accept_user_decision(comment, allowlist=allowlist, context=_CONTEXT, consumed_comment_ids=consumed)


class TestConstruction:
    def test_allowlist_normalizes_on_construction(self) -> None:
        assert _ALLOWLIST.logins == frozenset({"mega-gorilla"})

    def test_allowlist_rejects_invalid_login_entry(self) -> None:
        """設定誤り（charset外のentry）は構築時にIdentityError（silentに落とさない）。"""
        with pytest.raises(IdentityError) as excinfo:
            DecisionAllowlist(logins=frozenset({"bad login"}))
        assert excinfo.value.stage == "allowlist"

    def test_producer_rejects_empty(self) -> None:
        """空のproducer集合は設定誤り。全recordを不正actor violationとして捏造させない。"""
        with pytest.raises(IdentityError) as excinfo:
            ProducerAllowlist(logins=frozenset())
        assert excinfo.value.stage == "producer"

    def test_producer_normalizes(self) -> None:
        assert ProducerAllowlist(logins=frozenset({"Controller-Bot"})).logins == frozenset({"controller-bot"})

    def test_context_rejects_internal_record_kind(self) -> None:
        with pytest.raises(IdentityError) as excinfo:
            DecisionContext(kind=RecordKind.REVIEW_RESULT, repository="o/r", number=1, head_sha="b" * 40)
        assert excinfo.value.stage == "context"


class TestAcceptUserDecision:
    """AC-C06-01: allowlist外loginの承認commentはいかなる用途にも使われない。"""

    def test_accepts_exact_match_via_normalization(self) -> None:
        """設定と投稿のcase差は正規化で吸収する（別accountは存在し得ないため安全）。"""
        result = _accept("Mega-Gorilla")
        assert isinstance(result, AcceptedUserDecision)
        assert result.author_login == "mega-gorilla"
        assert result.head_sha == _CONTEXT.head_sha
        assert result.evidence.kind is RecordKind.MERGE_APPROVAL
        assert result.evidence.ref.value == "https://example.invalid/c/2001"

    def test_rejects_login_outside_allowlist(self) -> None:
        result = _accept("someone-else")
        assert isinstance(result, RejectedUserDecision)
        assert result.reason is DecisionRejection.NOT_IN_ALLOWLIST

    def test_rejects_bot(self) -> None:
        result = _accept("github-actions[bot]")
        assert isinstance(result, RejectedUserDecision)
        assert result.reason is DecisionRejection.BOT_ACTOR

    def test_rejects_missing_actor(self) -> None:
        result = _accept(None)
        assert isinstance(result, RejectedUserDecision)
        assert result.reason is DecisionRejection.MISSING_ACTOR

    def test_rejects_invalid_login(self) -> None:
        result = _accept("bad login")
        assert isinstance(result, RejectedUserDecision)
        assert result.reason is DecisionRejection.INVALID_LOGIN


class TestFailClosed:
    """AC-C06-02: allowlistを取得できない場合はユーザー判断を受理しない。"""

    def test_unavailable_rejects_with_distinct_reason(self) -> None:
        result = _accept(allowlist=AllowlistUnavailable(detail="設定を取得できない"))
        assert isinstance(result, RejectedUserDecision)
        assert result.reason is DecisionRejection.ALLOWLIST_UNAVAILABLE

    def test_empty_allowlist_always_denies(self) -> None:
        """空集合はUnavailableとは別で、常にNOT_IN_ALLOWLIST（deny）になる。"""
        result = _accept(allowlist=DecisionAllowlist(logins=frozenset()))
        assert isinstance(result, RejectedUserDecision)
        assert result.reason is DecisionRejection.NOT_IN_ALLOWLIST


class TestConsumedAndEdited:
    def test_rejects_edited_comment(self) -> None:
        result = _accept(updated_at="2026-08-21T11:00:00Z")
        assert isinstance(result, RejectedUserDecision)
        assert result.reason is DecisionRejection.EDITED

    def test_rejects_consumed_comment(self) -> None:
        result = _accept(consumed=frozenset({"2001"}))
        assert isinstance(result, RejectedUserDecision)
        assert result.reason is DecisionRejection.ALREADY_CONSUMED

    def test_rejects_embedded_marker_token(self) -> None:
        """ユーザーcommentが予約tokenを含むことはない（大小無視で偽装を拒否）。"""
        result = _accept(body="approve <!-- cc_review_meta:v1 {} -->")
        assert isinstance(result, RejectedUserDecision)
        assert result.reason is DecisionRejection.EMBEDDED_MARKER


class TestBindingDerivation:
    def test_same_input_yields_same_binding(self) -> None:
        first = _accept()
        second = _accept()
        assert isinstance(first, AcceptedUserDecision) and isinstance(second, AcceptedUserDecision)
        assert first.binding == second.binding
        assert first.binding.value == (
            "ud:MERGE_APPROVAL:Mega-Gorilla/claude-code-codex-review-loop#42:" + "a" * 40 + ":merge:fp-1:c2001"
        )

    def test_optional_context_fields_use_placeholder(self) -> None:
        context = DecisionContext(kind=RecordKind.USER_DECISION, repository="o/r", number=1, head_sha="b" * 40)
        comment = make_comment(2002, "yes", author="mega-gorilla")
        result = accept_user_decision(
            comment, allowlist=_ALLOWLIST, context=context, consumed_comment_ids=_NO_CONSUMED
        )
        assert isinstance(result, AcceptedUserDecision)
        assert result.binding.value == "ud:USER_DECISION:o/r#1:" + "b" * 40 + ":-:-:c2002"

    def test_different_context_yields_different_binding(self) -> None:
        """headが変われば別binding（head binding不変条件の基礎）。"""
        moved = DecisionContext(
            kind=_CONTEXT.kind,
            repository=_CONTEXT.repository,
            number=_CONTEXT.number,
            head_sha="c" * 40,
            merge_method=_CONTEXT.merge_method,
            candidate_fingerprint=_CONTEXT.candidate_fingerprint,
        )
        comment = make_comment(2001, "approve", author="mega-gorilla")
        first = accept_user_decision(comment, allowlist=_ALLOWLIST, context=_CONTEXT, consumed_comment_ids=_NO_CONSUMED)
        second = accept_user_decision(comment, allowlist=_ALLOWLIST, context=moved, consumed_comment_ids=_NO_CONSUMED)
        assert isinstance(first, AcceptedUserDecision) and isinstance(second, AcceptedUserDecision)
        assert first.binding != second.binding


class TestRevalidate:
    """受理済み判断の失効: 編集・削除・binding不一致（head変更等）。"""

    def _accepted(self) -> AcceptedUserDecision:
        result = _accept()
        assert isinstance(result, AcceptedUserDecision)
        return result

    def test_valid_when_unchanged(self) -> None:
        accepted = self._accepted()
        current = make_comment(2001, "approve", author="mega-gorilla")
        assert revalidate_user_decision(accepted, current=current, expected=_CONTEXT) is DecisionValidity.VALID

    def test_deleted_voids(self) -> None:
        accepted = self._accepted()
        assert revalidate_user_decision(accepted, current=None, expected=_CONTEXT) is DecisionValidity.VOIDED_DELETED

    def test_body_change_voids_as_edited(self) -> None:
        accepted = self._accepted()
        current = make_comment(2001, "approve!!", author="mega-gorilla")
        assert (
            revalidate_user_decision(accepted, current=current, expected=_CONTEXT)
            is DecisionValidity.VOIDED_EDITED
        )

    def test_edit_timestamp_voids_even_with_same_hash(self) -> None:
        accepted = self._accepted()
        current = make_comment(2001, "approve", author="mega-gorilla", updated_at="2026-08-21T12:00:00Z")
        assert (
            revalidate_user_decision(accepted, current=current, expected=_CONTEXT)
            is DecisionValidity.VOIDED_EDITED
        )

    def test_head_change_voids_as_binding_mismatch(self) -> None:
        accepted = self._accepted()
        moved = DecisionContext(
            kind=_CONTEXT.kind,
            repository=_CONTEXT.repository,
            number=_CONTEXT.number,
            head_sha="c" * 40,
            merge_method=_CONTEXT.merge_method,
            candidate_fingerprint=_CONTEXT.candidate_fingerprint,
        )
        current = make_comment(2001, "approve", author="mega-gorilla")
        assert (
            revalidate_user_decision(accepted, current=current, expected=moved)
            is DecisionValidity.VOIDED_BINDING_MISMATCH
        )
