# SPDX-License-Identifier: Apache-2.0
"""PR review threadの取得・解決状態判定・line findingへのreply（AC-C05-03）。

- 取得はGraphQL（isResolvedはRESTに無い）。thread levelはcursorでpage loopし
  （max_pages有界）、thread内commentsの続きがある場合は`truncated`で顕在化する
  （silent truncationしない。内側のpaginationはv1では実装せずADR-0007に記録）
- replyはREST（`POST .../pulls/{pr}/comments/{id}/replies`）。**reply対象は
  threadの先頭comment（top-level）のdatabaseId**でなければならない
- replyの冪等flowはconversationと同型: timeout / TRANSIENTはthread再取得 ->
  marker key+hash検索 -> 確認 or 同一keyで再投稿
- thread操作が恒久的に不可能な場合（NOT_FOUND / PERMANENT / AUTH）だけ、元comment
  URLを前置したconversation commentへfallbackする。TRANSIENTの尽きはfallbackせず
  伝播する（呼び出し側がFAILED化。恒久 / 一時の混同を避ける）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import Final, cast

from ..errors import ErrorCategory
from .conversation import (
    MAX_COMMENT_CHARS,
    MAX_LIST_RESPONSE_BYTES,
    MAX_SINGLE_RESPONSE_BYTES,
    EnsureOutcome,
    PostHashMismatch,
    PostRoute,
    PostVerified,
    UnverifiedComment,
    body_hash_of,
    comment_from_json,
    ensure_comment_posted,
)
from .gh import (
    GhApiError,
    GhContext,
    GhTimeoutError,
    RepoRef,
    RetryPolicy,
    TransportError,
    run_gh_api,
    run_gh_api_with_retry,
    write_private_file,
)
from .marker import ExtractedMarker, extract_marker

_THREADS_QUERY: Final = """\
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          comments(first: 100) {
            pageInfo { hasNextPage }
            nodes { databaseId url body path author { login } }
          }
        }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class ThreadComment:
    """review thread内の1 comment（未検証metadata）。"""

    comment_id: str
    url: str
    author_login: str
    body: str
    path: str | None
    body_hash: str
    marker: ExtractedMarker | None


@dataclass(frozen=True)
class UnverifiedThread:
    """1つのreview thread。truncatedはthread内commentsに未取得の続きがあることを示す。"""

    thread_id: str
    is_resolved: bool
    comments: tuple[ThreadComment, ...]
    truncated: bool


@unique
class ReplyRoute(Enum):
    DIRECT_REPLY = "DIRECT_REPLY"
    FALLBACK_COMMENT = "FALLBACK_COMMENT"


@dataclass(frozen=True)
class ReplyOutcome:
    """replyの結果。routeがFALLBACK_COMMENTのときはconversation commentとして投稿済み。"""

    route: ReplyRoute
    outcome: EnsureOutcome


def _as_dict(value: object, detail: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TransportError("metadata", detail, ErrorCategory.PERMANENT)
    return cast(dict[str, object], value)


def _as_list(value: object, detail: str) -> list[object]:
    if not isinstance(value, list):
        raise TransportError("metadata", detail, ErrorCategory.PERMANENT)
    return cast(list[object], value)


def _as_str(value: object, detail: str) -> str:
    if not isinstance(value, str):
        raise TransportError("metadata", detail, ErrorCategory.PERMANENT)
    return value


def _as_bool(value: object, detail: str) -> bool:
    if not isinstance(value, bool):
        raise TransportError("metadata", detail, ErrorCategory.PERMANENT)
    return value


def _thread_comment_from_node(raw: object) -> ThreadComment:
    node = _as_dict(raw, "comment nodeがobjectでない")
    database_id = node.get("databaseId")
    if isinstance(database_id, bool) or not isinstance(database_id, int):
        raise TransportError("metadata", "databaseIdが整数でない", ErrorCategory.PERMANENT)
    body = _as_str(node.get("body"), "comment bodyが文字列でない")
    author = _as_dict(node.get("author"), "authorがobjectでない")
    path = node.get("path")
    if path is not None and not isinstance(path, str):
        raise TransportError("metadata", "pathが文字列でもnullでもない", ErrorCategory.PERMANENT)
    return ThreadComment(
        comment_id=str(database_id),
        url=_as_str(node.get("url"), "comment urlが文字列でない"),
        author_login=_as_str(author.get("login"), "author.loginが文字列でない"),
        body=body,
        path=path,
        body_hash=body_hash_of(body),
        marker=extract_marker(body),
    )


def _thread_from_node(raw: object) -> UnverifiedThread:
    node = _as_dict(raw, "thread nodeがobjectでない")
    comments_obj = _as_dict(node.get("comments"), "commentsがobjectでない")
    page_info = _as_dict(comments_obj.get("pageInfo"), "comments.pageInfoがobjectでない")
    nodes = _as_list(comments_obj.get("nodes"), "comments.nodesがarrayでない")
    return UnverifiedThread(
        thread_id=_as_str(node.get("id"), "thread idが文字列でない"),
        is_resolved=_as_bool(node.get("isResolved"), "isResolvedがbooleanでない"),
        comments=tuple(_thread_comment_from_node(entry) for entry in nodes),
        truncated=_as_bool(page_info.get("hasNextPage"), "comments.pageInfo.hasNextPageがbooleanでない"),
    )


def fetch_review_threads(
    context: GhContext,
    repo: RepoRef,
    pr_number: int,
    *,
    policy: RetryPolicy,
    max_pages: int,
) -> tuple[UnverifiedThread, ...]:
    """PRの全review threadをGraphQLで取得する（thread levelのpage loop、有界）。"""
    threads: list[UnverifiedThread] = []
    cursor: str | None = None
    page = 1
    while True:
        rest: tuple[str, ...] = (
            "graphql",
            "-f",
            f"query={_THREADS_QUERY}",
            "-F",
            f"owner={repo.owner}",
            "-F",
            f"name={repo.name}",
            "-F",
            f"number={pr_number}",
        )
        if cursor is not None:
            rest = (*rest, "-F", f"cursor={cursor}")
        response = run_gh_api_with_retry(
            context, rest, max_output_bytes=MAX_LIST_RESPONSE_BYTES, policy=policy
        )
        body = _as_dict(response.body, "GraphQL応答がobjectでない")
        data = _as_dict(body.get("data"), "GraphQL応答にdataが無い")
        repository = _as_dict(data.get("repository"), "repositoryが取得できない")
        pull_request = _as_dict(repository.get("pullRequest"), "pullRequestが取得できない")
        review_threads = _as_dict(pull_request.get("reviewThreads"), "reviewThreadsが取得できない")
        page_info = _as_dict(review_threads.get("pageInfo"), "reviewThreads.pageInfoがobjectでない")
        nodes = _as_list(review_threads.get("nodes"), "reviewThreads.nodesがarrayでない")
        threads.extend(_thread_from_node(node) for node in nodes)
        if not _as_bool(page_info.get("hasNextPage"), "pageInfo.hasNextPageがbooleanでない"):
            break
        cursor = _as_str(page_info.get("endCursor"), "pageInfo.endCursorが文字列でない")
        page += 1
        if page > max_pages:
            raise TransportError(
                "pagination", f"thread page数が上限を超えた（max_pages={max_pages}）", ErrorCategory.PERMANENT
            )
    return tuple(threads)


def _reply_target(thread: UnverifiedThread) -> ThreadComment:
    """reply対象はthreadの先頭comment（top-level）。replyのIDへのreplyはAPIが拒否する。"""
    if not thread.comments:
        raise TransportError("thread", "threadにcommentが無くreplyできない", ErrorCategory.PERMANENT)
    return thread.comments[0]


def post_thread_reply(
    context: GhContext, repo: RepoRef, pr_number: int, thread: UnverifiedThread, body: str
) -> UnverifiedComment:
    """review threadへのreplyを1件投稿する（retryしない。冪等flowはensure側）。"""
    if len(body) > MAX_COMMENT_CHARS:
        raise TransportError("body", f"本文がGitHubの上限を超えた（{len(body)}字）", ErrorCategory.PERMANENT)
    target = _reply_target(thread)
    body_file = write_private_file(context.workdir, "reply", body)
    try:
        response = run_gh_api(
            context,
            (
                "-X",
                "POST",
                f"repos/{repo.slug}/pulls/{pr_number}/comments/{target.comment_id}/replies",
                "-F",
                f"body=@{body_file}",
            ),
            max_output_bytes=MAX_SINGLE_RESPONSE_BYTES,
        )
    finally:
        body_file.unlink(missing_ok=True)
    return comment_from_json(response.body)


def get_pull_comment(
    context: GhContext, repo: RepoRef, comment_id: str, *, policy: RetryPolicy
) -> UnverifiedComment:
    """pull review commentのID直接取得（replyのread-after-write用）。"""
    response = run_gh_api_with_retry(
        context,
        ("-X", "GET", f"repos/{repo.slug}/pulls/comments/{comment_id}"),
        max_output_bytes=MAX_SINGLE_RESPONSE_BYTES,
        policy=policy,
    )
    return comment_from_json(response.body)


def ensure_thread_reply(
    context: GhContext,
    repo: RepoRef,
    pr_number: int,
    thread: UnverifiedThread,
    body: str,
    *,
    idempotency_key: str,
    search_attempts: int,
    search_backoff_seconds: float,
    search_max_pages: int,
    policy: RetryPolicy,
) -> EnsureOutcome:
    """replyの冪等投稿: post -> read-after-write。成否不明時はthread再取得で検索する。"""
    expected_hash = body_hash_of(body)
    try:
        posted_id = post_thread_reply(context, repo, pr_number, thread, body).comment_id
        route = PostRoute.POSTED
    except (GhTimeoutError, GhApiError) as exc:
        if isinstance(exc, GhApiError) and exc.category is not ErrorCategory.TRANSIENT:
            raise
        found = _search_reply(
            context,
            repo,
            pr_number,
            thread_id=thread.thread_id,
            idempotency_key=idempotency_key,
            expected_hash=expected_hash,
            attempts=search_attempts,
            backoff_seconds=search_backoff_seconds,
            max_pages=search_max_pages,
            policy=policy,
        )
        if found is None:
            posted_id = post_thread_reply(context, repo, pr_number, thread, body).comment_id
            route = PostRoute.REPOSTED_AFTER_TIMEOUT
        else:
            posted_id = found.comment_id
            route = PostRoute.FOUND_AFTER_TIMEOUT
    fetched, matched = _verify_pull_comment(context, repo, posted_id, expected_hash, policy=policy)
    if not matched:
        return PostHashMismatch(route=route, comment=fetched, expected_hash=expected_hash)
    return PostVerified(route=route, comment=fetched, body_hash=expected_hash)


def _verify_pull_comment(
    context: GhContext, repo: RepoRef, comment_id: str, expected_hash: str, *, policy: RetryPolicy
) -> tuple[UnverifiedComment, bool]:
    fetched = get_pull_comment(context, repo, comment_id, policy=policy)
    return fetched, fetched.body_hash == expected_hash


def _search_reply(
    context: GhContext,
    repo: RepoRef,
    pr_number: int,
    *,
    thread_id: str,
    idempotency_key: str,
    expected_hash: str,
    attempts: int,
    backoff_seconds: float,
    max_pages: int,
    policy: RetryPolicy,
) -> ThreadComment | None:
    for attempt in range(attempts):
        threads = fetch_review_threads(context, repo, pr_number, policy=policy, max_pages=max_pages)
        for candidate in threads:
            if candidate.thread_id != thread_id:
                continue
            for comment in candidate.comments:
                if comment.marker is None or comment.marker.payload is None:
                    continue
                if comment.marker.payload.get("key") != idempotency_key:
                    continue
                if comment.body_hash == expected_hash:
                    return comment
        if attempt < attempts - 1:
            policy.sleep(backoff_seconds)
    return None


def reply_with_fallback(
    context: GhContext,
    repo: RepoRef,
    pr_number: int,
    thread: UnverifiedThread,
    body: str,
    *,
    source_comment_url: str,
    idempotency_key: str,
    search_since: str | None,
    search_attempts: int,
    search_backoff_seconds: float,
    search_max_pages: int,
    policy: RetryPolicy,
) -> ReplyOutcome:
    """threadへのreplyを試み、恒久的に不可能な場合だけconversation commentへfallbackする。

    fallback本文は元comment URLを前置する（AC-C05-03）。TRANSIENTの尽きは
    fallbackせず伝播する（呼び出し側がFAILED化する）。
    """
    try:
        outcome = ensure_thread_reply(
            context,
            repo,
            pr_number,
            thread,
            body,
            idempotency_key=idempotency_key,
            search_attempts=search_attempts,
            search_backoff_seconds=search_backoff_seconds,
            search_max_pages=search_max_pages,
            policy=policy,
        )
        return ReplyOutcome(route=ReplyRoute.DIRECT_REPLY, outcome=outcome)
    except GhApiError as exc:
        if exc.category is ErrorCategory.TRANSIENT:
            raise
    fallback_body = f"> 元comment: {source_comment_url}\n\n{body}"
    outcome = ensure_comment_posted(
        context,
        repo,
        pr_number,
        fallback_body,
        idempotency_key=idempotency_key,
        search_since=search_since,
        search_attempts=search_attempts,
        search_backoff_seconds=search_backoff_seconds,
        search_max_pages=search_max_pages,
        policy=policy,
    )
    return ReplyOutcome(route=ReplyRoute.FALLBACK_COMMENT, outcome=outcome)
