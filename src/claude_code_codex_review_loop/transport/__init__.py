# SPDX-License-Identifier: Apache-2.0
"""C-05 GitHub transport（Phase 5）。

未検証のGitHub metadataの取得・投稿・read-after-write確認・thread操作に限定した
I/O層。**このcomponentは認証を行わない** — actorの解決・allowlist照合・record chain
検証・canonical recordの生成はC-06 / identityが担い、C-05の戻り値（Unverified*）を
workflowの判断根拠にしない。gh CLIの呼び出し規約・予約marker形式・冪等post flow・
error分類はADR-0007を正本とする。
"""

from .conversation import (
    MAX_COMMENT_CHARS,
    EnsureOutcome,
    FetchResult,
    PostHashMismatch,
    PostRoute,
    PostVerified,
    UnverifiedComment,
    body_hash_of,
    ensure_comment_posted,
    fetch_comments_since,
    find_comment_by_marker,
    get_issue_comment,
    post_issue_comment,
    verify_comment,
)
from .gh import (
    ApiResponse,
    GhApiError,
    GhContext,
    GhTimeoutError,
    RepoRef,
    RetryPolicy,
    TransportError,
    run_gh,
    run_gh_api,
    run_gh_api_with_retry,
    write_private_file,
)
from .marker import (
    ALLOWED_PAYLOAD_KEYS,
    ESCAPED_TOKEN,
    MARKER_TOKEN,
    ExtractedMarker,
    SanitizedBody,
    attach_marker,
    extract_marker,
    sanitize_agent_body,
)
from .pull_request import UnverifiedPullRequest, get_pull_request, pull_request_from_json
from .render import PreparedBody, normalize_newlines, prepare_public_body
from .threads import (
    ReplyOutcome,
    ReplyRoute,
    ThreadComment,
    UnverifiedThread,
    ensure_thread_reply,
    fetch_review_threads,
    get_pull_comment,
    post_thread_reply,
    reply_with_fallback,
)

__all__ = [
    "ALLOWED_PAYLOAD_KEYS",
    "ESCAPED_TOKEN",
    "MARKER_TOKEN",
    "MAX_COMMENT_CHARS",
    "ApiResponse",
    "EnsureOutcome",
    "ExtractedMarker",
    "FetchResult",
    "GhApiError",
    "GhContext",
    "GhTimeoutError",
    "PostHashMismatch",
    "PostRoute",
    "PostVerified",
    "PreparedBody",
    "ReplyOutcome",
    "ReplyRoute",
    "RepoRef",
    "RetryPolicy",
    "SanitizedBody",
    "ThreadComment",
    "TransportError",
    "UnverifiedComment",
    "UnverifiedPullRequest",
    "UnverifiedThread",
    "attach_marker",
    "body_hash_of",
    "ensure_comment_posted",
    "ensure_thread_reply",
    "extract_marker",
    "fetch_comments_since",
    "fetch_review_threads",
    "find_comment_by_marker",
    "get_issue_comment",
    "get_pull_request",
    "get_pull_comment",
    "normalize_newlines",
    "post_issue_comment",
    "post_thread_reply",
    "prepare_public_body",
    "pull_request_from_json",
    "reply_with_fallback",
    "run_gh",
    "run_gh_api",
    "run_gh_api_with_retry",
    "sanitize_agent_body",
    "verify_comment",
    "write_private_file",
]
