# SPDX-License-Identifier: Apache-2.0
"""C-06 testの共有helper。

- chain specに準拠した正規chain（prev連結済み）の本文列とUnverifiedComment列を生成する
  （fake ghへのseedは`seed_dict`でdict形式へ写す。c05_support.seed_stateへ渡す）
- credential隔離testのfixture script（env dump child / fake claude CLI）をtmp_pathへ
  生成する（tracked fileを増やさない。c03_supportと同じ方式）
"""

from __future__ import annotations

from pathlib import Path

from c02_support.helpers import record_payload

from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.identity import compose_record_marker_payload
from claude_code_codex_review_loop.schema.projection import build_record_projection, derive_record_binding
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


def record_projection(
    kind: RecordKind = RecordKind.REVIEW_RESULT, *, head: str = HEAD, body: str = "record"
) -> dict[str, str | int]:
    """kindのrepresentative payloadから作るprojection（C-02の製品関数を使う）。

    bodyはmarker付加前の公開本文で、`pay`の入力になる（ADR-0010）。
    """
    return build_record_projection(kind, record_payload(kind, head_sha=head), head_sha=head, body=body)


def marker_payload(
    *,
    kind: RecordKind = RecordKind.REVIEW_RESULT,
    run_id: str = RUN,
    head: str = HEAD,
    seq: int,
    prev: str | None = None,
    body: str = "record",
) -> dict[str, str | int]:
    """正規marker payload（projection付き。keyは製品側の導出関数で決まる）。"""
    projection = record_projection(kind, head=head, body=body)
    key = derive_record_binding(
        run_id=run_id, seq=seq, kind=kind, head_sha=head, payload_hash=str(projection["pay"])
    )
    return compose_record_marker_payload(
        key=key,
        kind=kind,
        run_id=run_id,
        head_sha=head,
        seq=seq,
        prev_body_hash=prev,
        projection=projection,
    )


def chain_bodies(
    count: int,
    *,
    run_id: str = RUN,
    head: str = HEAD,
    kind: RecordKind = RecordKind.REVIEW_RESULT,
) -> list[str]:
    """prev連結済みの正規chain本文列（seq=1..count）を生成する。

    projectionとbindingは製品側の導出関数（C-02）で作る。fixtureへ直書きせず、
    producer側の規約が変わればhelper経由で全testへ波及させる。
    """
    bodies: list[str] = []
    prev: str | None = None
    for seq in range(1, count + 1):
        text = f"record {seq}"
        payload = marker_payload(kind=kind, run_id=run_id, head=head, seq=seq, prev=prev, body=text)
        body = attach_marker(text, payload)
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


# 子processが自分のenvをJSONで書き出すscript（argv: 出力path）
_ENV_DUMP_SCRIPT = """\
import json
import os
import sys

with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(dict(os.environ), handle, ensure_ascii=False)
"""

# fake claude CLI。argv: mode ...（modeは環境変数CC_REVIEW_FAKE_CLAUDE_MODEで指定）
_FAKE_CLAUDE_SCRIPT = """\
import json
import os
import sys
import time

mode = os.environ.get("CC_REVIEW_FAKE_CLAUDE_MODE", "ok")
if mode == "hang":
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        time.sleep(0.05)
    sys.exit(9)
if mode == "fail":
    sys.stderr.write("auto-mode unavailable")
    sys.exit(1)
if mode == "nonjson":
    sys.stdout.write("not json")
    sys.exit(0)
if mode == "jsonarray":
    sys.stdout.write("[1, 2]")
    sys.exit(0)
sys.stdout.write(json.dumps({"mode": "auto", "subcommand": sys.argv[1:]}))
sys.exit(0)
"""


def write_env_dump_script(directory: Path) -> Path:
    """子processのenvをJSONへ書き出すscriptを生成する。"""
    script = directory / "env_dump.py"
    script.write_text(_ENV_DUMP_SCRIPT, encoding="utf-8")
    return script


def write_fake_claude(directory: Path) -> Path:
    """Auto mode probe用のfake claude CLIを生成する（P-011: 実CLIへ依存しない）。"""
    script = directory / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE_SCRIPT, encoding="utf-8")
    return script
