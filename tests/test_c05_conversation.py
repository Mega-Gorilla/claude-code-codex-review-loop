# SPDX-License-Identifier: Apache-2.0
"""conversation投稿・取得・冪等flowの受入test（AC-C05-01 / 02 / 05）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from c05_support.helpers import make_context, make_policy, read_state, seed_state

from claude_code_codex_review_loop.errors import ErrorCategory
from claude_code_codex_review_loop.transport import (
    GhApiError,
    PostHashMismatch,
    PostRoute,
    PostVerified,
    RepoRef,
    TransportError,
    attach_marker,
    body_hash_of,
    ensure_comment_posted,
    fetch_comments_since,
    find_comment_by_marker,
    post_issue_comment,
    verify_comment,
)

_REPO = RepoRef(owner="o", name="r")


def _seed_comment(number: int, *, issue: int = 5, body: str = "x", updated: str | None = None) -> dict[str, object]:
    stamp = updated if updated is not None else f"2026-08-21T09:00:{number:02d}Z"
    return {
        "id": number,
        "issue": issue,
        "html_url": f"https://example.invalid/c/{number}",
        "body": body,
        "created_at": f"2026-08-21T08:00:{number:02d}Z",
        "updated_at": stamp,
        "user": {"login": "alice"},
    }


def _ensure(context, body: str, **overrides):  # type: ignore[no-untyped-def]
    kwargs = dict(
        search_since=None,
        search_attempts=2,
        search_backoff_seconds=0.01,
        search_max_pages=5,
        policy=make_policy(),
    )
    kwargs.update(overrides)
    return ensure_comment_posted(context, _REPO, 5, body, **kwargs)


class TestPostAndVerify:
    def test_post_refetch_hash_match_records_ids(self, tmp_path: Path) -> None:
        """AC-C05-01: 投稿→再取得→hash一致→ID記録。"""
        context = make_context(tmp_path)
        body = attach_marker("review結果の本文", {"key": "turn-1", "head": "abc123"})
        outcome = _ensure(context, body)
        assert isinstance(outcome, PostVerified)
        assert outcome.route is PostRoute.POSTED
        assert outcome.comment.comment_id == "1001"
        assert outcome.comment.url.startswith("https://")
        assert outcome.body_hash == body_hash_of(body)
        assert outcome.comment.body == body  # 加工されていない（AC-C05-05）

    def test_unverified_metadata_preserves_raw_fields(self, tmp_path: Path) -> None:
        """AC-C05-05: createdAt / updatedAtを別々に保持し、未検証のまま返す。"""
        seed_state(tmp_path, comments=[_seed_comment(1, body="original")])
        context = make_context(tmp_path)
        fetched, matched = verify_comment(context, _REPO, "1", body_hash_of("original"), policy=make_policy())
        assert matched is True
        assert fetched.created_at == "2026-08-21T08:00:01Z"
        assert fetched.updated_at == "2026-08-21T09:00:01Z"
        assert fetched.author_login == "alice"

    def test_hash_mismatch_is_reported_not_retried(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="ok,mutate_get")
        body = attach_marker("本文", {"key": "turn-1"})
        outcome = _ensure(context, body)
        assert isinstance(outcome, PostHashMismatch)
        assert outcome.expected_hash == body_hash_of(body)
        assert "[tampered]" in outcome.comment.body

    def test_oversized_body_is_rejected_before_posting(self, tmp_path: Path) -> None:
        context = make_context(tmp_path)
        with pytest.raises(TransportError) as excinfo:
            post_issue_comment(context, _REPO, 5, "x" * 70000)
        assert excinfo.value.stage == "body"
        assert read_state(tmp_path)["comments"] == []


class TestIdempotentFlow:
    def test_timeout_with_persisted_comment_is_found_not_duplicated(self, tmp_path: Path) -> None:
        """AC-C05-02: 投稿がtimeoutしたがserver側で成立していた場合、検索で発見し重複させない。"""
        context = make_context(tmp_path, scenario="ok,persist_then_hang,ok", timeout_seconds=2.0)
        body = attach_marker("本文", {"key": "turn-1"})
        outcome = _ensure(context, body)
        assert isinstance(outcome, PostVerified)
        assert outcome.route is PostRoute.FOUND_AFTER_TIMEOUT
        assert len(read_state(tmp_path)["comments"]) == 1  # 重複なし

    def test_timeout_without_persistence_reposts_with_same_key(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="ok,timeout,ok", timeout_seconds=2.0)
        body = attach_marker("本文", {"key": "turn-1"})
        outcome = _ensure(context, body, search_attempts=1)
        assert isinstance(outcome, PostVerified)
        assert outcome.route is PostRoute.REPOSTED_AFTER_TIMEOUT
        comments = read_state(tmp_path)["comments"]
        assert len(comments) == 1

    def test_transient_post_failure_uses_search_flow(self, tmp_path: Path) -> None:
        """5xxも成否不明として扱い、検索flowへ入る（blind retryで重複させない）。"""
        context = make_context(tmp_path, scenario="ok,s500,ok,ok", timeout_seconds=10.0)
        body = attach_marker("本文", {"key": "turn-1"})
        outcome = _ensure(context, body, search_attempts=1)
        assert isinstance(outcome, PostVerified)
        assert outcome.route is PostRoute.REPOSTED_AFTER_TIMEOUT
        assert len(read_state(tmp_path)["comments"]) == 1

    def test_permanent_post_failure_propagates(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="ok,u422")
        body = attach_marker("本文", {"key": "turn-1"})
        with pytest.raises(GhApiError) as excinfo:
            _ensure(context, body)
        assert excinfo.value.category is ErrorCategory.PERMANENT

    def test_forged_marker_with_different_hash_does_not_satisfy_search(self, tmp_path: Path) -> None:
        """偽造marker（key一致・hash不一致）ではrepostを抑止できない。"""
        body = attach_marker("正しい本文", {"key": "turn-1"})
        forged = attach_marker("偽の本文", {"key": "turn-1"})
        seed_state(tmp_path, comments=[_seed_comment(1, body=forged)])
        context = make_context(tmp_path, scenario="ok,timeout,ok", timeout_seconds=2.0)
        outcome = _ensure(context, body, search_attempts=1)
        assert isinstance(outcome, PostVerified)
        assert outcome.route is PostRoute.REPOSTED_AFTER_TIMEOUT
        assert len(read_state(tmp_path)["comments"]) == 2  # 偽物 + 正規repost


class TestResumeIdempotency:
    """round 2指摘: 別processからのPersistRecord再発行（resume）で重複しないsearch-first。"""

    def test_resume_after_persist_and_recovery_failure_finds_existing(self, tmp_path: Path) -> None:
        """1回目: persist成立→timeout→recovery検索も失敗。2回目: 同一key/bodyで再実行しても件数1。"""
        from c05_support.helpers import reset_call_counter

        body = attach_marker("本文", {"key": "turn-1"})
        # 1回目: 事前検索ok(空) -> POSTがpersist後hang -> recovery検索がs500で尽きる
        context1 = make_context(tmp_path, scenario="ok,persist_then_hang,s500", timeout_seconds=2.0)
        with pytest.raises(GhApiError):
            _ensure(context1, body, search_attempts=1, policy=make_policy(max_attempts=1))
        assert len(read_state(tmp_path)["comments"]) == 1  # persist済み
        # 2回目（別processのresume相当）: 事前検索が既存を発見し、POSTを呼ばない
        reset_call_counter(tmp_path)
        context2 = make_context(tmp_path, scenario="ok")
        outcome = _ensure(context2, body)
        assert isinstance(outcome, PostVerified)
        assert outcome.route is PostRoute.FOUND_EXISTING
        assert len(read_state(tmp_path)["comments"]) == 1  # 重複なし

    def test_existing_record_is_verified_without_posting(self, tmp_path: Path) -> None:
        """既存record発見時はPOST endpointを呼ばず、GET確認のみを行う。"""
        body = attach_marker("既存の本文", {"key": "turn-1"})
        existing = _seed_comment(1, body=body)
        seed_state(tmp_path, comments=[existing])
        context = make_context(tmp_path)
        outcome = _ensure(context, body)
        assert isinstance(outcome, PostVerified)
        assert outcome.route is PostRoute.FOUND_EXISTING
        assert outcome.comment.comment_id == "1"
        state = read_state(tmp_path)
        assert len(state["comments"]) == 1  # POSTされていない
        assert state["counter"] == 0  # 新規採番なし = POST endpoint未到達

    def test_existing_record_with_mutated_body_reports_mismatch(self, tmp_path: Path) -> None:
        body = attach_marker("既存の本文", {"key": "turn-1"})
        seed_state(tmp_path, comments=[_seed_comment(1, body=body)])
        context = make_context(tmp_path, scenario="ok,mutate_get")
        outcome = _ensure(context, body)
        assert isinstance(outcome, PostHashMismatch)
        assert outcome.route is PostRoute.FOUND_EXISTING


class TestMarkerKeyDerivation:
    """検索keyは本文markerから導出する（round 1指摘: 二重入力による不一致の排除）。"""

    def test_body_without_marker_is_rejected_before_posting(self, tmp_path: Path) -> None:
        context = make_context(tmp_path)
        with pytest.raises(TransportError) as excinfo:
            _ensure(context, "markerの無い本文")
        assert excinfo.value.stage == "marker"
        assert read_state(tmp_path)["comments"] == []  # 投稿前に拒否

    def test_marker_without_key_is_rejected(self, tmp_path: Path) -> None:
        context = make_context(tmp_path)
        body = attach_marker("本文", {"kind": "REVIEW_RESULT"})  # keyなし
        with pytest.raises(TransportError) as excinfo:
            _ensure(context, body)
        assert excinfo.value.stage == "marker"

    def test_search_uses_the_key_from_the_body_marker(self, tmp_path: Path) -> None:
        """persist後timeoutでも本文のkeyで検索するため、必ず発見され重複しない。"""
        context = make_context(tmp_path, scenario="ok,persist_then_hang,ok", timeout_seconds=2.0)
        body = attach_marker("本文", {"key": "body-key"})
        outcome = _ensure(context, body)
        assert isinstance(outcome, PostVerified)
        assert outcome.route is PostRoute.FOUND_AFTER_TIMEOUT
        assert len(read_state(tmp_path)["comments"]) == 1


class TestNewlineNormalization:
    """ADR-0007: 投稿前にCRLF / 単独CRをLFへ正規化する単一choke point。"""

    def test_crlf_body_is_normalized_before_hash_and_post(self, tmp_path: Path) -> None:
        from claude_code_codex_review_loop.transport import normalize_newlines

        context = make_context(tmp_path)
        body = attach_marker("first\r\nsecond\rthird", {"key": "turn-1"})
        outcome = _ensure(context, body)
        assert isinstance(outcome, PostVerified)
        posted = outcome.comment.body
        assert "\r" not in posted
        assert outcome.body_hash == body_hash_of(normalize_newlines(body))  # hashは正規化後bytesから

    def test_prepare_public_body_normalizes_newlines(self) -> None:
        from claude_code_codex_review_loop.transport import attach_marker as attach
        from claude_code_codex_review_loop.transport import prepare_public_body

        prepared = prepare_public_body("first\r\nsecond", speaker="Codex", model="m")
        final = attach(prepared.text, {"key": "turn-1"})
        assert "\r" not in final

    def test_nullable_user_is_preserved_not_rejected(self, tmp_path: Path) -> None:
        """削除済みaccount等のnull userは未検証metadataとしてNoneで保持する（C-06が判断）。"""
        ghost = _seed_comment(1, body="ghost")
        ghost["user"] = None
        seed_state(tmp_path, comments=[ghost])
        context = make_context(tmp_path)
        result = fetch_comments_since(context, _REPO, 5, None, policy=make_policy(), max_pages=1)
        assert result.comments[0].author_login is None


class TestFetchSince:
    def test_since_filters_and_cursor_advances(self, tmp_path: Path) -> None:
        seed_state(
            tmp_path,
            comments=[
                _seed_comment(1, updated="2026-08-21T09:00:01Z"),
                _seed_comment(2, updated="2026-08-21T09:00:02Z"),
                _seed_comment(3, updated="2026-08-21T09:00:03Z"),
            ],
        )
        context = make_context(tmp_path)
        result = fetch_comments_since(
            context, _REPO, 5, "2026-08-21T09:00:02Z", policy=make_policy(), max_pages=3
        )
        assert [c.comment_id for c in result.comments] == ["2", "3"]  # inclusive（境界は再配送）
        assert result.next_cursor == "2026-08-21T09:00:03Z"

    def test_empty_result_keeps_cursor(self, tmp_path: Path) -> None:
        seed_state(tmp_path, comments=[])
        context = make_context(tmp_path)
        result = fetch_comments_since(context, _REPO, 5, "2026-08-21T09:00:00Z", policy=make_policy(), max_pages=3)
        assert result.comments == ()
        assert result.next_cursor == "2026-08-21T09:00:00Z"

    def test_multi_page_loop_collects_all(self, tmp_path: Path) -> None:
        seed_state(tmp_path, comments=[_seed_comment(n) for n in range(1, 6)])
        context = make_context(tmp_path, page_size=2)
        result = fetch_comments_since(context, _REPO, 5, None, policy=make_policy(), max_pages=5)
        assert len(result.comments) == 5

    def test_exceeding_max_pages_is_an_error(self, tmp_path: Path) -> None:
        seed_state(tmp_path, comments=[_seed_comment(n) for n in range(1, 6)])
        context = make_context(tmp_path, page_size=2)
        with pytest.raises(TransportError) as excinfo:
            fetch_comments_since(context, _REPO, 5, None, policy=make_policy(), max_pages=1)
        assert excinfo.value.stage == "pagination"

    def test_invalid_cursor_is_rejected(self, tmp_path: Path) -> None:
        context = make_context(tmp_path)
        with pytest.raises(TransportError) as excinfo:
            fetch_comments_since(context, _REPO, 5, "2026-01-01T00:00:00Z&evil=1", policy=make_policy(), max_pages=1)
        assert excinfo.value.stage == "cursor"


class TestSearchBackoff:
    def test_two_search_attempts_sleep_between(self, tmp_path: Path) -> None:
        """検索はbounded N回で、間にbackoffを挟む（list取得の遅延race緩和）。"""
        from c05_support.helpers import SleepRecorder

        recorder = SleepRecorder()
        context = make_context(tmp_path, scenario="ok,timeout,ok,ok,ok,ok", timeout_seconds=2.0)
        body = attach_marker("本文", {"key": "turn-1"})
        outcome = _ensure(
            context, body, search_attempts=2, policy=make_policy(sleep=recorder), search_backoff_seconds=0.2
        )
        assert isinstance(outcome, PostVerified)
        assert outcome.route is PostRoute.REPOSTED_AFTER_TIMEOUT
        assert 0.2 in recorder.calls  # 検索間のbackoff

    def test_list_response_that_is_not_an_array_is_rejected(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="list_object")
        with pytest.raises(TransportError) as excinfo:
            fetch_comments_since(context, _REPO, 5, None, policy=make_policy(), max_pages=1)
        assert excinfo.value.stage == "metadata"


class TestCommentJsonUnits:
    def test_non_object_response_is_rejected(self) -> None:
        from claude_code_codex_review_loop.transport.conversation import comment_from_json

        with pytest.raises(TransportError):
            comment_from_json(["not", "dict"])

    def test_non_integer_id_is_rejected(self) -> None:
        from claude_code_codex_review_loop.transport.conversation import comment_from_json

        broken = _seed_comment(1)
        broken["id"] = True
        with pytest.raises(TransportError):
            comment_from_json(broken)

    def test_missing_user_is_rejected(self) -> None:
        from claude_code_codex_review_loop.transport.conversation import comment_from_json

        broken = _seed_comment(1)
        broken["user"] = "alice"
        with pytest.raises(TransportError):
            comment_from_json(broken)

    def test_bad_optional_id_is_rejected(self) -> None:
        from claude_code_codex_review_loop.transport.conversation import comment_from_json

        broken = _seed_comment(1)
        broken["in_reply_to_id"] = 1.5
        with pytest.raises(TransportError):
            comment_from_json(broken)

    def test_optional_ids_accept_int_and_str(self) -> None:
        from claude_code_codex_review_loop.transport.conversation import comment_from_json

        data = _seed_comment(1)
        data["in_reply_to_id"] = 42
        data["pull_request_review_id"] = "77"
        comment = comment_from_json(data)
        assert (comment.reply_to, comment.review_id) == ("42", "77")


class TestMetadataParsing:
    def test_malformed_comment_json_is_structured_error(self, tmp_path: Path) -> None:
        broken = _seed_comment(1)
        broken["body"] = 5  # 文字列でないbody
        seed_state(tmp_path, comments=[broken])
        context = make_context(tmp_path)
        with pytest.raises(TransportError) as excinfo:
            fetch_comments_since(context, _REPO, 5, None, policy=make_policy(), max_pages=1)
        assert excinfo.value.stage == "metadata"

    def test_find_by_marker_ignores_markerless_and_mismatched(self) -> None:
        from claude_code_codex_review_loop.transport.conversation import comment_from_json

        plain = comment_from_json(_seed_comment(1, body="marker無し"))
        other_key = comment_from_json(_seed_comment(2, body=attach_marker("x", {"key": "other"})))
        target_body = attach_marker("y", {"key": "turn-1"})
        target = comment_from_json(_seed_comment(3, body=target_body))
        found = find_comment_by_marker((plain, other_key, target), "turn-1", body_hash_of(target_body))
        assert found is target
        assert find_comment_by_marker((plain, other_key), "turn-1", "deadbeef") is None
