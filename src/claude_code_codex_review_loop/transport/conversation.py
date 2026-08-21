# SPDX-License-Identifier: Apache-2.0
"""Issue / PR conversation commentの投稿・取得・read-after-write確認（冪等）。

- 戻り値は**未検証metadata**（`UnverifiedComment`）であり、canonical recordとしての
  受理判定はC-06が行う（AC-C05-05）。取得fieldは加工せず保持する（createdAtと
  updatedAtを別々に保つことでC-06の編集検知が成立する）
- 投稿は`ensure_comment_posted`の冪等flowで行う: post -> read-after-write検証。
  timeoutまたはTRANSIENT失敗（成否不明）はidempotency markerで検索し、既存が
  見つかれば確認のみ、無ければ**同一key**で再投稿する（AC-C05-01 / 02）
- marker検索のpredicateは「key一致 AND body hash一致」。書込権限を持つ第三者が
  同一keyのmarkerを偽造して再投稿を抑止する攻撃を無効化する（真正性の最終判定は
  C-06のallowlist。重複が生じた場合のcanonical選択もC-06。C-05はdeleteしない）
- 取得は差分cursor（`since` = updated_atのinclusive filter）+ 自前page loop。
  境界のcommentは再配送されるため、(comment_id, updated_at)のdedupeは呼び出し側の
  責務（C-05はrawで返す）。規約の正本はADR-0007
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final

from ..errors import ErrorCategory
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
from .render import normalize_newlines

# 応答sizeの機構上限（設定値ではない）。comment本文は最大65,536字（多byte + JSON escape考慮）
MAX_SINGLE_RESPONSE_BYTES: Final = 2_097_152
MAX_LIST_RESPONSE_BYTES: Final = 33_554_432
MAX_COMMENT_CHARS: Final = 65_536

_CURSOR_PATTERN = re.compile(r"[0-9A-Za-z:+.TZ-]+")


@dataclass(frozen=True)
class UnverifiedComment:
    """GitHubから取得した未検証のcomment metadata。

    comment_id等は取得値の文字列表現で、意味的な加工をしない。body_hashとmarkerは
    C-05が計算した付随情報（bodyそのものは無加工で保持する）。
    """

    comment_id: str
    url: str
    author_login: str | None
    created_at: str
    updated_at: str
    body: str
    reply_to: str | None
    review_id: str | None
    body_hash: str
    marker: ExtractedMarker | None


@unique
class PostRoute(Enum):
    """投稿が完了へ至った経路。"""

    POSTED = "POSTED"
    FOUND_AFTER_TIMEOUT = "FOUND_AFTER_TIMEOUT"
    REPOSTED_AFTER_TIMEOUT = "REPOSTED_AFTER_TIMEOUT"


@dataclass(frozen=True)
class PostVerified:
    """read-after-write検証済みの投稿結果。turnをcompletedにできる根拠。"""

    route: PostRoute
    comment: UnverifiedComment
    body_hash: str


@dataclass(frozen=True)
class PostHashMismatch:
    """read-after-writeでhashが一致しなかった（編集・改変の疑い。呼び出し側がBLOCKED化）。"""

    route: PostRoute
    comment: UnverifiedComment
    expected_hash: str


EnsureOutcome = PostVerified | PostHashMismatch


@dataclass(frozen=True)
class FetchResult:
    """差分取得の結果。next_cursorは結果中のmax(updated_at)（結果が空なら入力のまま）。"""

    comments: tuple[UnverifiedComment, ...]
    next_cursor: str | None


def body_hash_of(text: str) -> str:
    """本文hash: UTF-8生bytesのSHA-256 hex（正規化なし。marker含む全body）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise TransportError("metadata", f"応答の{key}が文字列でない", ErrorCategory.PERMANENT)
    return value


def _optional_id(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise TransportError("metadata", f"応答の{key}がIDとして解釈できない", ErrorCategory.PERMANENT)
    return str(value)


def comment_from_json(data: object) -> UnverifiedComment:
    """GitHubのcomment応答（issue / pull review comment共通の関心field）を型へ写す。"""
    if not isinstance(data, dict):
        raise TransportError("metadata", "comment応答がobjectでない", ErrorCategory.PERMANENT)
    raw_id = data.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, int):
        raise TransportError("metadata", "comment応答のidが整数でない", ErrorCategory.PERMANENT)
    # 削除済みaccount等でuserはnullになり得る。未検証metadataとしてnullを保持し、
    # actorの受理判断（fail closed）はC-06が行う（AC-C05-05）
    user = data.get("user")
    author_login: str | None
    if user is None:
        author_login = None
    elif isinstance(user, dict):
        author_login = _require_str(user, "login")
    else:
        raise TransportError("metadata", "comment応答のuserがobjectでもnullでもない", ErrorCategory.PERMANENT)
    body = _require_str(data, "body")
    return UnverifiedComment(
        comment_id=str(raw_id),
        url=_require_str(data, "html_url"),
        author_login=author_login,
        created_at=_require_str(data, "created_at"),
        updated_at=_require_str(data, "updated_at"),
        body=body,
        reply_to=_optional_id(data, "in_reply_to_id"),
        review_id=_optional_id(data, "pull_request_review_id"),
        body_hash=body_hash_of(body),
        marker=extract_marker(body),
    )


def post_issue_comment(context: GhContext, repo: RepoRef, number: int, body: str) -> UnverifiedComment:
    """conversation commentを1件投稿する（本文はfile経由 = P-005）。retryしない。

    TRANSIENT失敗とtimeoutは成否不明として扱い、冪等flow（ensure_comment_posted）が
    marker検索で回復する。
    """
    body = normalize_newlines(body)
    if len(body) > MAX_COMMENT_CHARS:
        raise TransportError("body", f"本文がGitHubの上限を超えた（{len(body)}字）", ErrorCategory.PERMANENT)
    body_file = write_private_file(context.workdir, "body", body)
    try:
        response = run_gh_api(
            context,
            (
                "-X",
                "POST",
                f"repos/{repo.slug}/issues/{number}/comments",
                "-F",
                f"body=@{body_file}",
            ),
            max_output_bytes=MAX_SINGLE_RESPONSE_BYTES,
        )
    finally:
        body_file.unlink(missing_ok=True)
    return comment_from_json(response.body)


def get_issue_comment(
    context: GhContext, repo: RepoRef, comment_id: str, *, policy: RetryPolicy
) -> UnverifiedComment:
    """comment IDでの直接取得（read-after-write用。読み取りはbounded retry可）。"""
    response = run_gh_api_with_retry(
        context,
        ("-X", "GET", f"repos/{repo.slug}/issues/comments/{comment_id}"),
        max_output_bytes=MAX_SINGLE_RESPONSE_BYTES,
        policy=policy,
    )
    return comment_from_json(response.body)


def verify_comment(
    context: GhContext,
    repo: RepoRef,
    comment_id: str,
    expected_body_hash: str,
    *,
    policy: RetryPolicy,
) -> tuple[UnverifiedComment, bool]:
    """投稿→再取得→hash一致のread-after-write検証（AC-C05-01）。"""
    fetched = get_issue_comment(context, repo, comment_id, policy=policy)
    return fetched, fetched.body_hash == expected_body_hash


def fetch_comments_since(
    context: GhContext,
    repo: RepoRef,
    number: int,
    since: str | None,
    *,
    policy: RetryPolicy,
    max_pages: int,
) -> FetchResult:
    """差分cursorでのcomment取得（自前page loop。silent truncationしない）。

    sinceはupdated_atのinclusive filterのため境界のcommentが再配送される。
    (comment_id, updated_at)によるdedupeは呼び出し側の責務。
    """
    if since is not None and not _CURSOR_PATTERN.fullmatch(since):
        raise TransportError("cursor", "cursorが不正な文字を含む", ErrorCategory.PERMANENT)
    collected: list[UnverifiedComment] = []
    page = 1
    while True:
        path = f"repos/{repo.slug}/issues/{number}/comments?per_page=100&page={page}"
        if since is not None:
            path += f"&since={since}"
        response = run_gh_api_with_retry(
            context, ("-X", "GET", path), max_output_bytes=MAX_LIST_RESPONSE_BYTES, policy=policy
        )
        if not isinstance(response.body, list):
            raise TransportError("metadata", "comment一覧応答がarrayでない", ErrorCategory.PERMANENT)
        collected.extend(comment_from_json(entry) for entry in response.body)
        # 継続判定にはLink headerのrel="next"の有無だけを使い、server提供URLを実行しない
        link = response.headers.get("link", "")
        if 'rel="next"' not in link:
            break
        page += 1
        if page > max_pages:
            raise TransportError(
                "pagination", f"page数が上限を超えた（max_pages={max_pages}）", ErrorCategory.PERMANENT
            )
    next_cursor = max((comment.updated_at for comment in collected), default=since)
    return FetchResult(comments=tuple(collected), next_cursor=next_cursor)


def _require_marker_key(body: str) -> str:
    """冪等投稿の前提: 本文末尾の正規markerからidempotency keyを取り出す。

    markerまたはkeyが無い本文をensure系へ渡すのは呼び出し側の誤りであり、
    timeout時に検索できず重複投稿へ至るため投稿前に拒否する。
    """
    marker = extract_marker(body)
    if marker is None or marker.payload is None:
        raise TransportError("marker", "本文末尾に正規のmarkerが必要（冪等投稿の前提）", ErrorCategory.PERMANENT)
    key = marker.payload.get("key")
    if not isinstance(key, str) or not key:
        raise TransportError("marker", "markerのkeyが無いか文字列でない", ErrorCategory.PERMANENT)
    return key


def find_comment_by_marker(
    comments: tuple[UnverifiedComment, ...], idempotency_key: str, expected_body_hash: str
) -> UnverifiedComment | None:
    """marker key一致かつbody hash一致のcommentを探す（偽造markerは一致しないため無視される）。"""
    for comment in comments:
        if comment.marker is None or comment.marker.payload is None:
            continue
        if comment.marker.payload.get("key") != idempotency_key:
            continue
        if comment.body_hash == expected_body_hash:
            return comment
    return None


def ensure_comment_posted(
    context: GhContext,
    repo: RepoRef,
    number: int,
    body: str,
    *,
    search_since: str | None,
    search_attempts: int,
    search_backoff_seconds: float,
    search_max_pages: int,
    policy: RetryPolicy,
) -> EnsureOutcome:
    """冪等な投稿: post -> read-after-write。成否不明時はmarker検索 -> 確認 or 再投稿。

    - bodyは事前にmarker（key入り）をattach済みであること。**検索keyは本文markerから
      導出する**（引数との二重入力を持たず、本文と検索の不一致による重複投稿を
      構造的に防ぐ）。正規markerまたはkeyが無い本文は投稿前に拒否する
    - search_sinceは投稿開始前の時刻から時計skew分を引いたcursorを渡す
    - 再投稿は同一keyの同一本文で行う。事後に重複が発覚した場合のcanonical選択は
      C-06の責務（C-05は削除しない）
    """
    body = normalize_newlines(body)
    idempotency_key = _require_marker_key(body)
    expected_hash = body_hash_of(body)
    try:
        posted = post_issue_comment(context, repo, number, body)
        route = PostRoute.POSTED
    except (GhTimeoutError, GhApiError) as exc:
        if isinstance(exc, GhApiError) and exc.category is not ErrorCategory.TRANSIENT:
            raise
        found = _search_with_backoff(
            context,
            repo,
            number,
            idempotency_key=idempotency_key,
            expected_hash=expected_hash,
            since=search_since,
            attempts=search_attempts,
            backoff_seconds=search_backoff_seconds,
            max_pages=search_max_pages,
            policy=policy,
        )
        if found is None:
            posted = post_issue_comment(context, repo, number, body)
            route = PostRoute.REPOSTED_AFTER_TIMEOUT
        else:
            posted = found
            route = PostRoute.FOUND_AFTER_TIMEOUT
    fetched, matched = verify_comment(context, repo, posted.comment_id, expected_hash, policy=policy)
    if not matched:
        return PostHashMismatch(route=route, comment=fetched, expected_hash=expected_hash)
    return PostVerified(route=route, comment=fetched, body_hash=expected_hash)


def _search_with_backoff(
    context: GhContext,
    repo: RepoRef,
    number: int,
    *,
    idempotency_key: str,
    expected_hash: str,
    since: str | None,
    attempts: int,
    backoff_seconds: float,
    max_pages: int,
    policy: RetryPolicy,
) -> UnverifiedComment | None:
    for attempt in range(attempts):
        result = fetch_comments_since(context, repo, number, since, policy=policy, max_pages=max_pages)
        found = find_comment_by_marker(result.comments, idempotency_key, expected_hash)
        if found is not None:
            return found
        if attempt < attempts - 1:
            policy.sleep(backoff_seconds)
    return None
