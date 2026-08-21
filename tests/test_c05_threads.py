# SPDX-License-Identifier: Apache-2.0
"""review thread操作の受入test（AC-C05-03: 取得・解決状態・line reply・fallback）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from c05_support.helpers import make_context, make_policy, read_state, reset_call_counter, seed_state

from claude_code_codex_review_loop.errors import ErrorCategory
from claude_code_codex_review_loop.transport import (
    GhApiError,
    PostRoute,
    PostVerified,
    ReplyRoute,
    RepoRef,
    TransportError,
    attach_marker,
    ensure_thread_reply,
    fetch_review_threads,
    post_thread_reply,
    reply_with_fallback,
)

_REPO = RepoRef(owner="o", name="r")


def _thread(thread_id: str = "T1", *, resolved: bool = False, inner_next: bool = False) -> dict[str, object]:
    return {
        "id": thread_id,
        "isResolved": resolved,
        "innerHasNext": inner_next,
        "comments": [
            {
                "databaseId": 900,
                "url": "https://example.invalid/rc/900",
                "body": "指摘の本文",
                "path": "src/a.py",
                "author": {"login": "codex-bot"},
            }
        ],
    }


def _reply_kwargs(**overrides):  # type: ignore[no-untyped-def]
    kwargs = dict(
        idempotency_key="turn-1",
        search_attempts=1,
        search_backoff_seconds=0.01,
        search_max_pages=5,
        policy=make_policy(),
    )
    kwargs.update(overrides)
    return kwargs


class TestFetch:
    def test_threads_with_resolution_state(self, tmp_path: Path) -> None:
        seed_state(tmp_path, threads=[_thread("T1", resolved=False), _thread("T2", resolved=True)])
        context = make_context(tmp_path)
        threads = fetch_review_threads(context, _REPO, 7, policy=make_policy(), max_pages=3)
        assert [t.thread_id for t in threads] == ["T1", "T2"]
        assert [t.is_resolved for t in threads] == [False, True]
        comment = threads[0].comments[0]
        assert (comment.comment_id, comment.path, comment.author_login) == ("900", "src/a.py", "codex-bot")

    def test_inner_truncation_is_surfaced(self, tmp_path: Path) -> None:
        """thread内commentsの未取得の続きはtruncatedで顕在化する（silent truncationしない）。"""
        seed_state(tmp_path, threads=[_thread("T1", inner_next=True)])
        context = make_context(tmp_path)
        threads = fetch_review_threads(context, _REPO, 7, policy=make_policy(), max_pages=3)
        assert threads[0].truncated is True

    def test_thread_level_pagination(self, tmp_path: Path) -> None:
        seed_state(tmp_path, threads=[_thread(f"T{n}") for n in range(1, 6)])
        context = make_context(tmp_path, page_size=2)
        threads = fetch_review_threads(context, _REPO, 7, policy=make_policy(), max_pages=5)
        assert len(threads) == 5

    def test_exceeding_max_pages_is_an_error(self, tmp_path: Path) -> None:
        seed_state(tmp_path, threads=[_thread(f"T{n}") for n in range(1, 6)])
        context = make_context(tmp_path, page_size=2)
        with pytest.raises(TransportError) as excinfo:
            fetch_review_threads(context, _REPO, 7, policy=make_policy(), max_pages=1)
        assert excinfo.value.stage == "pagination"

    def test_malformed_graphql_shape_is_structured_error(self, tmp_path: Path) -> None:
        seed_state(tmp_path, threads=[{"id": "T1", "isResolved": "yes", "comments": []}])
        context = make_context(tmp_path)
        with pytest.raises(TransportError) as excinfo:
            fetch_review_threads(context, _REPO, 7, policy=make_policy(), max_pages=3)
        assert excinfo.value.stage == "metadata"


class TestReply:
    def test_reply_targets_top_level_comment_and_verifies(self, tmp_path: Path) -> None:
        seed_state(tmp_path, threads=[_thread("T1")])
        context = make_context(tmp_path)
        threads = fetch_review_threads(context, _REPO, 7, policy=make_policy(), max_pages=3)
        body = attach_marker("回答の本文", {"key": "turn-1"})
        outcome = ensure_thread_reply(context, _REPO, 7, threads[0], body, **_reply_kwargs())
        assert isinstance(outcome, PostVerified)
        assert outcome.route is PostRoute.POSTED
        assert outcome.comment.reply_to == "900"  # 先頭commentへのreply
        assert outcome.comment.review_id == "555"  # review IDを区別して保持

    def test_empty_thread_cannot_be_replied(self, tmp_path: Path) -> None:
        seed_state(tmp_path, threads=[{"id": "T0", "isResolved": False, "innerHasNext": False, "comments": []}])
        context = make_context(tmp_path)
        threads = fetch_review_threads(context, _REPO, 7, policy=make_policy(), max_pages=3)
        with pytest.raises(TransportError) as excinfo:
            post_thread_reply(context, _REPO, 7, threads[0], "x")
        assert excinfo.value.stage == "thread"

    def test_timeout_with_persisted_reply_is_found_not_duplicated(self, tmp_path: Path) -> None:
        """replyの冪等flow: timeoutでもthread再取得の検索で発見し、重複させない。"""
        seed_state(tmp_path, threads=[_thread("T1")])
        context = make_context(tmp_path, timeout_seconds=2.0)
        threads = fetch_review_threads(context, _REPO, 7, policy=make_policy(), max_pages=3)
        # 以降のstepを先頭から固定: reply(persist_then_hang) -> 検索(graphql ok) -> 再取得(get ok)
        reset_call_counter(tmp_path)
        context2 = make_context(tmp_path, scenario="persist_then_hang,ok,ok,ok", timeout_seconds=2.0)
        body = attach_marker("回答", {"key": "turn-1"})
        outcome = ensure_thread_reply(context2, _REPO, 7, threads[0], body, **_reply_kwargs())
        assert isinstance(outcome, PostVerified)
        assert outcome.route is PostRoute.FOUND_AFTER_TIMEOUT
        assert len(read_state(tmp_path)["pull_comments"]) == 1

    def test_oversized_reply_is_rejected(self, tmp_path: Path) -> None:
        seed_state(tmp_path, threads=[_thread("T1")])
        context = make_context(tmp_path)
        threads = fetch_review_threads(context, _REPO, 7, policy=make_policy(), max_pages=3)
        with pytest.raises(TransportError) as excinfo:
            post_thread_reply(context, _REPO, 7, threads[0], "x" * 70000)
        assert excinfo.value.stage == "body"


class TestReplyIdempotency:
    def test_timeout_without_persistence_reposts(self, tmp_path: Path) -> None:
        """reply timeout後、thread再取得で見つからなければ同一keyで再投稿する。"""
        seed_state(tmp_path, threads=[_thread("T1")])
        context = make_context(tmp_path)
        threads = fetch_review_threads(context, _REPO, 7, policy=make_policy(), max_pages=3)
        reset_call_counter(tmp_path)
        context2 = make_context(tmp_path, scenario="timeout,ok,ok,ok", timeout_seconds=2.0)
        body = attach_marker("回答", {"key": "turn-1"})
        outcome = ensure_thread_reply(context2, _REPO, 7, threads[0], body, **_reply_kwargs())
        assert isinstance(outcome, PostVerified)
        assert outcome.route is PostRoute.REPOSTED_AFTER_TIMEOUT
        assert len(read_state(tmp_path)["pull_comments"]) == 1

    def test_reply_verify_mismatch_is_reported(self, tmp_path: Path) -> None:
        from claude_code_codex_review_loop.transport import PostHashMismatch

        seed_state(tmp_path, threads=[_thread("T1")])
        context = make_context(tmp_path)
        threads = fetch_review_threads(context, _REPO, 7, policy=make_policy(), max_pages=3)
        reset_call_counter(tmp_path)
        context2 = make_context(tmp_path, scenario="ok,mutate_get")
        body = attach_marker("回答", {"key": "turn-1"})
        outcome = ensure_thread_reply(context2, _REPO, 7, threads[0], body, **_reply_kwargs())
        assert isinstance(outcome, PostHashMismatch)

    def test_search_ignores_other_threads_and_mismatched_markers(self, tmp_path: Path) -> None:
        """検索はthread ID・marker key・body hashの全一致だけを採用する。"""
        body = attach_marker("回答", {"key": "turn-1"})
        other_thread = _thread("T-other")
        other_thread["comments"].append(
            {
                "databaseId": 950,
                "url": "u",
                "body": body,  # 同一key・同一hashだが別thread → 無視される
                "path": None,
                "author": {"login": "x"},
            }
        )
        target = _thread("T1")
        target["comments"].extend(
            [
                {"databaseId": 951, "url": "u", "body": "markerなし", "path": None, "author": {"login": "x"}},
                {
                    "databaseId": 952,
                    "url": "u",
                    "body": attach_marker("別turn", {"key": "other"}),
                    "path": None,
                    "author": {"login": "x"},
                },
                {
                    "databaseId": 953,
                    "url": "u",
                    "body": attach_marker("偽の本文", {"key": "turn-1"}),  # key一致・hash不一致（偽造）
                    "path": None,
                    "author": {"login": "mallory"},
                },
            ]
        )
        seed_state(tmp_path, threads=[other_thread, target])
        context = make_context(tmp_path)
        threads = fetch_review_threads(context, _REPO, 7, policy=make_policy(), max_pages=3)
        target_thread = next(t for t in threads if t.thread_id == "T1")
        reset_call_counter(tmp_path)
        from c05_support.helpers import SleepRecorder

        recorder = SleepRecorder()
        context2 = make_context(tmp_path, scenario="timeout,ok,ok,ok,ok", timeout_seconds=2.0)
        outcome = ensure_thread_reply(
            context2, _REPO, 7, target_thread, body,
            **_reply_kwargs(search_attempts=2, policy=make_policy(sleep=recorder)),
        )
        assert isinstance(outcome, PostVerified)
        assert outcome.route is PostRoute.REPOSTED_AFTER_TIMEOUT  # 別thread・別keyは採用されない
        assert 0.01 in recorder.calls  # 検索間のbackoff


class TestShapeUnits:
    def test_helper_validators_reject_wrong_types(self) -> None:
        from claude_code_codex_review_loop.transport.threads import _as_bool, _as_dict, _as_list, _as_str

        with pytest.raises(TransportError):
            _as_dict([], "x")
        with pytest.raises(TransportError):
            _as_list({}, "x")
        with pytest.raises(TransportError):
            _as_str(1, "x")
        with pytest.raises(TransportError):
            _as_bool("true", "x")

    def test_bad_comment_nodes_are_rejected(self) -> None:
        from claude_code_codex_review_loop.transport.threads import _thread_comment_from_node

        with pytest.raises(TransportError):
            _thread_comment_from_node({"databaseId": True, "url": "u", "body": "b", "author": {"login": "x"}})
        with pytest.raises(TransportError):
            _thread_comment_from_node(
                {"databaseId": 1, "url": "u", "body": "b", "author": {"login": "x"}, "path": 5}
            )


class TestFallback:
    def test_permanent_reply_failure_falls_back_to_conversation_comment(self, tmp_path: Path) -> None:
        """thread不可（恒久分類）はcomment URL付きconversation commentへfallback（AC-C05-03）。"""
        seed_state(tmp_path, threads=[_thread("T1")])
        context = make_context(tmp_path)
        threads = fetch_review_threads(context, _REPO, 7, policy=make_policy(), max_pages=3)
        # reply(u422) -> fallback post(ok) -> verify(ok)
        reset_call_counter(tmp_path)
        context2 = make_context(tmp_path, scenario="u422,ok,ok")
        body = attach_marker("回答", {"key": "turn-1"})
        outcome = reply_with_fallback(
            context2,
            _REPO,
            7,
            threads[0],
            body,
            source_comment_url="https://example.invalid/rc/900",
            search_since=None,
            **_reply_kwargs(),
        )
        assert outcome.route is ReplyRoute.FALLBACK_COMMENT
        assert isinstance(outcome.outcome, PostVerified)
        posted_body = outcome.outcome.comment.body
        assert posted_body.startswith("> 元comment: https://example.invalid/rc/900")
        assert len(read_state(tmp_path)["comments"]) == 1  # conversation側へ1件

    def test_direct_reply_success_does_not_fall_back(self, tmp_path: Path) -> None:
        seed_state(tmp_path, threads=[_thread("T1")])
        context = make_context(tmp_path)
        threads = fetch_review_threads(context, _REPO, 7, policy=make_policy(), max_pages=3)
        body = attach_marker("回答", {"key": "turn-1"})
        outcome = reply_with_fallback(
            context,
            _REPO,
            7,
            threads[0],
            body,
            source_comment_url="https://example.invalid/rc/900",
            search_since=None,
            **_reply_kwargs(),
        )
        assert outcome.route is ReplyRoute.DIRECT_REPLY
        assert read_state(tmp_path)["comments"] == []  # conversation側へは投稿しない

    def test_transient_exhaustion_propagates_instead_of_fallback(self, tmp_path: Path) -> None:
        """TRANSIENTの尽きはfallbackしない（恒久 / 一時の混同を避け、FAILED化は呼び出し側）。"""
        seed_state(tmp_path, threads=[_thread("T1")])
        context = make_context(tmp_path)
        threads = fetch_review_threads(context, _REPO, 7, policy=make_policy(), max_pages=3)
        # reply(s500) -> 検索flowのgraphql取得もs500連続でTRANSIENT尽き
        reset_call_counter(tmp_path)
        context2 = make_context(tmp_path, scenario="s500")
        body = attach_marker("回答", {"key": "turn-1"})
        with pytest.raises(GhApiError) as excinfo:
            reply_with_fallback(
                context2,
                _REPO,
                7,
                threads[0],
                body,
                source_comment_url="https://example.invalid/rc/900",
                search_since=None,
                **_reply_kwargs(),
            )
        assert excinfo.value.category is ErrorCategory.TRANSIENT
        assert read_state(tmp_path)["comments"] == []
