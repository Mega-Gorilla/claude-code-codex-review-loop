# SPDX-License-Identifier: Apache-2.0
"""C-06 testの共有helper。

chain specに準拠した正規chain（prev連結済み）の本文列とUnverifiedComment列を生成する。
fake ghへのseedは`seed_dict`でdict形式へ写す（tests/c05_support/helpers.pyのseed_stateへ渡す）。
"""

from __future__ import annotations

from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.identity import compose_record_marker_payload
from claude_code_codex_review_loop.transport.conversation import UnverifiedComment, body_hash_of
from claude_code_codex_review_loop.transport.marker import attach_marker, extract_marker

PRODUCER = "controller-bot"
HEAD = "a" * 40
RUN = "run-1"
REPOSITORY = "Mega-Gorilla/claude-code-codex-review-loop"
NUMBER = 42


def make_comment(
    comment_id: int | str,
    body: str,
    *,
    author: str | None = PRODUCER,
    created_at: str = "2026-08-21T10:00:00Z",
    updated_at: str | None = None,
    repository: str = REPOSITORY,
    number: int = NUMBER,
    url: str | None = None,
) -> UnverifiedComment:
    """UnverifiedCommentを直接構築する（pure coreのtestはfake gh不要）。

    urlはGitHubのhtml_url形式を既定にする（観測元照合の入力になるため）。
    """
    return UnverifiedComment(
        comment_id=str(comment_id),
        url=url if url is not None else f"https://github.com/{repository}/issues/{number}#issuecomment-{comment_id}",
        author_login=author,
        created_at=created_at,
        updated_at=updated_at if updated_at is not None else created_at,
        body=body,
        reply_to=None,
        review_id=None,
        body_hash=body_hash_of(body),
        marker=extract_marker(body),
    )


def chain_bodies(
    count: int,
    *,
    run_id: str = RUN,
    head: str = HEAD,
    kind: RecordKind = RecordKind.REVIEW_RESULT,
) -> list[str]:
    """prev連結済みの正規chain本文列（seq=1..count）を生成する。"""
    bodies: list[str] = []
    prev: str | None = None
    for seq in range(1, count + 1):
        payload = compose_record_marker_payload(
            key=f"turn-{seq}", kind=kind, run_id=run_id, head_sha=head, seq=seq, prev_body_hash=prev
        )
        body = attach_marker(f"record {seq}", payload)
        bodies.append(body)
        prev = body_hash_of(body)
    return bodies


def chain_comments(
    count: int,
    *,
    run_id: str = RUN,
    head: str = HEAD,
    author: str | None = PRODUCER,
    start_id: int = 1001,
) -> tuple[UnverifiedComment, ...]:
    """正規chainのUnverifiedComment列（comment IDはstart_idからの連番）を生成する。"""
    return tuple(
        make_comment(start_id + index, body, author=author, created_at=f"2026-08-21T10:00:{index:02d}Z")
        for index, body in enumerate(chain_bodies(count, run_id=run_id, head=head))
    )


def seed_dict(comment: UnverifiedComment, *, issue: int = 7) -> dict[str, object]:
    """fake ghのstateへseedできるdict形式へ写す（c05_support.seed_state用）。"""
    return {
        "id": int(comment.comment_id),
        "issue": issue,
        "html_url": comment.url,
        "body": comment.body,
        "created_at": comment.created_at,
        "updated_at": comment.updated_at,
        "user": None if comment.author_login is None else {"login": comment.author_login},
    }
