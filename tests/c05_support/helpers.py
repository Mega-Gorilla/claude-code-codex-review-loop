# SPDX-License-Identifier: Apache-2.0
"""C-05受入testの共有helper。

fake ghをprocess境界に置き（P-011）、live GitHubへ接続せず全経路を検証する。
fake gh scriptはtmp_pathへ生成する（tracked fileを増やさない）。gh本体の挙動
（`--include`の出力形式、exit code 0/1/2/4、`-F body=@file`のfile読み）を正確に
模倣する。

- 起動は`gh_command=(sys.executable, fake_gh.py)`のargv prefix注入（両OS同一）
- state（投稿済みcomment等）はJSON file。pathはenv `CC_REVIEW_FAKE_GH_STATE`で渡す
- 挙動は per-call のstep列 env `CC_REVIEW_FAKE_GH_SCENARIO`（comma区切り。末尾stepが
  以後繰り返し）で注入する
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from c03_support.helpers import child_env

from claude_code_codex_review_loop.transport import GhContext, RetryPolicy

_FAKE_GH_SCRIPT = '''\
import json
import os
import sys
import time

STATE_PATH = os.environ["CC_REVIEW_FAKE_GH_STATE"]
SCENARIO = os.environ.get("CC_REVIEW_FAKE_GH_SCENARIO", "ok").split(",")
PAGE_SIZE = int(os.environ.get("CC_REVIEW_FAKE_GH_PAGE_SIZE", "100"))


def load_state():
    with open(STATE_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False)


def respond(status, reason, headers, body_bytes, exit_code=None):
    head = f"HTTP/2.0 {status} {reason}".rstrip() + "\\r\\n"
    for name, value in headers:
        head += f"{name}: {value}\\r\\n"
    head += "\\r\\n"
    sys.stdout.buffer.write(head.encode("utf-8") + body_bytes)
    sys.stdout.buffer.flush()
    if exit_code is None:
        exit_code = 0 if 200 <= status < 300 else 1
    sys.exit(exit_code)


def respond_json(status, reason, payload, headers=()):
    respond(status, reason, list(headers), json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def hang():
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        time.sleep(0.05)
    sys.exit(9)


def read_body_field(args):
    for arg in args:
        if arg.startswith("body=@"):
            with open(arg[len("body=@"):], "r", encoding="utf-8") as handle:
                return handle.read()
    return None


def next_comment(state, body, extra):
    state["counter"] = state.get("counter", 0) + 1
    number = state["counter"]
    comment = {
        "id": 1000 + number,
        "html_url": f"https://example.invalid/c/{1000 + number}",
        "body": body,
        "created_at": f"2026-08-21T10:00:{number:02d}Z",
        "updated_at": f"2026-08-21T10:00:{number:02d}Z",
        "user": {"login": "controller-bot"},
    }
    comment.update(extra)
    return comment


def main():
    args = sys.argv[1:]
    state = load_state()
    call_index = state.get("calls", 0)
    state["calls"] = call_index + 1
    save_state(state)
    step = SCENARIO[min(call_index, len(SCENARIO) - 1)]

    if step == "timeout":
        hang()
    if step == "e4":
        sys.exit(4)
    if step == "e2":
        sys.exit(2)
    if step == "noinclude":
        sys.stdout.write("garbled output without status line")
        sys.exit(1)
    if step == "ok_noinclude":
        sys.stdout.write("garbled output with exit 0")
        sys.exit(0)
    if step == "list_object":
        respond_json(200, "OK", {"unexpected": "object"})
    if step == "s500":
        respond_json(500, "Internal Server Error", {"message": "boom"}, [("Retry-After", "1")])
    if step == "s500_noheader":
        respond_json(500, "Internal Server Error", {"message": "boom"})
    if step == "r429":
        respond_json(429, "Too Many Requests", {"message": "slow down"}, [("Retry-After", "2")])
    if step == "rl403":
        respond_json(
            403, "Forbidden", {"message": "rate limited"},
            [("X-RateLimit-Remaining", "0"), ("X-RateLimit-Reset", "2000")],
        )
    if step == "f403":
        respond_json(403, "Forbidden", {"message": "no"})
    if step == "a401":
        respond_json(401, "Unauthorized", {"message": "auth"})
    if step == "nf404":
        respond_json(404, "Not Found", {"message": "missing"})
    if step == "u422":
        respond_json(422, "Unprocessable Entity", {"message": "invalid"})
    if step == "nonjson":
        respond(200, "OK", [], b"this is not json")
    if step == "nonutf8":
        respond(200, "OK", [], b"\\xff\\xfe\\xfa")
    if step == "oversize":
        respond(200, "OK", [], b"[" + b"1," * 200000 + b"1]")
    if step == "graphql_error":
        error_type = os.environ.get("CC_REVIEW_FAKE_GH_GRAPHQL_TYPE")
        entry = {"message": "err"}
        if error_type:
            entry["type"] = error_type
        respond(200, "OK", [], json.dumps({"errors": [entry]}).encode("utf-8"), exit_code=1)

    # step は ok / persist_then_hang / mutate_get のいずれか（endpoint処理へ進む）
    if args[:2] != ["api", "--include"]:
        sys.exit(64)
    rest = args[2:]
    method = "GET"
    if "-X" in rest:
        method = rest[rest.index("-X") + 1]
    path = next(arg for arg in rest if not arg.startswith("-") and arg not in (method,))

    if path == "graphql":
        cursor = None
        for arg in rest:
            if arg.startswith("cursor="):
                cursor = arg[len("cursor="):]
        threads = state.get("threads", [])
        start = int(cursor) if cursor else 0
        page = threads[start : start + PAGE_SIZE]
        has_next = start + PAGE_SIZE < len(threads)
        nodes = []
        for thread in page:
            nodes.append(
                {
                    "id": thread["id"],
                    "isResolved": thread["isResolved"],
                    "comments": {
                        "pageInfo": {"hasNextPage": thread.get("innerHasNext", False)},
                        "nodes": thread["comments"],
                    },
                }
            )
        payload = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": has_next, "endCursor": str(start + PAGE_SIZE)},
                            "nodes": nodes,
                        }
                    }
                }
            }
        }
        respond_json(200, "OK", payload)

    if method == "POST" and "/issues/" in path and path.endswith("/comments"):
        body = read_body_field(rest)
        issue_number = int(path.split("/issues/")[1].split("/")[0])
        comment = next_comment(state, body, {"issue": issue_number})
        state.setdefault("comments", []).append(comment)
        save_state(state)
        if step == "persist_then_hang":
            hang()
        respond_json(201, "Created", comment)

    if method == "POST" and "/pulls/" in path and path.endswith("/replies"):
        body = read_body_field(rest)
        target_id = int(path.split("/comments/")[1].split("/")[0])
        comment = next_comment(state, body, {"in_reply_to_id": target_id, "pull_request_review_id": 555})
        state.setdefault("pull_comments", []).append(comment)
        for thread in state.get("threads", []):
            if any(c["databaseId"] == target_id for c in thread["comments"]):
                thread["comments"].append(
                    {
                        "databaseId": comment["id"],
                        "url": comment["html_url"],
                        "body": body,
                        "path": None,
                        "author": {"login": "controller-bot"},
                    }
                )
        save_state(state)
        if step == "persist_then_hang":
            hang()
        respond_json(201, "Created", comment)

    if method == "GET" and "/issues/comments/" in path:
        comment_id = int(path.rsplit("/", 1)[1])
        for comment in state.get("comments", []):
            if comment["id"] == comment_id:
                found = dict(comment)
                if step == "mutate_get":
                    found["body"] = found["body"] + "[tampered]"
                respond_json(200, "OK", found)
        respond_json(404, "Not Found", {"message": "missing"})

    if method == "GET" and "/pulls/comments/" in path:
        comment_id = int(path.rsplit("/", 1)[1])
        for comment in state.get("pull_comments", []):
            if comment["id"] == comment_id:
                found = dict(comment)
                if step == "mutate_get":
                    found["body"] = found["body"] + "[tampered]"
                respond_json(200, "OK", found)
        respond_json(404, "Not Found", {"message": "missing"})

    if method == "GET" and "/issues/" in path and "/comments" in path:
        issue_number = int(path.split("/issues/")[1].split("/")[0])
        query = path.split("?", 1)[1] if "?" in path else ""
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        page = int(params.get("page", "1"))
        since = params.get("since")
        matched = [
            c for c in state.get("comments", [])
            if c.get("issue") == issue_number and (since is None or c["updated_at"] >= since)
        ]
        start = (page - 1) * PAGE_SIZE
        chunk = matched[start : start + PAGE_SIZE]
        headers = []
        if start + PAGE_SIZE < len(matched):
            headers.append(("Link", '<https://example.invalid/next>; rel="next"'))
        respond(200, "OK", headers, json.dumps(chunk, ensure_ascii=False).encode("utf-8"))

    sys.exit(64)


main()
'''


def write_fake_gh(directory: Path) -> Path:
    """fake gh scriptをtmp配下へ生成してpathを返す。"""
    script = directory / "fake_gh.py"
    script.write_text(_FAKE_GH_SCRIPT, encoding="utf-8")
    return script


def seed_state(
    directory: Path,
    *,
    comments: list[dict[str, object]] | None = None,
    pull_comments: list[dict[str, object]] | None = None,
    threads: list[dict[str, object]] | None = None,
) -> Path:
    """fake ghのstate fileを初期化する。"""
    state_path = directory / "fake-gh-state.json"
    state = {
        "comments": comments or [],
        "pull_comments": pull_comments or [],
        "threads": threads or [],
        "counter": 0,
        "calls": 0,
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return state_path


def reset_call_counter(directory: Path) -> None:
    """scenarioのstep indexを先頭へ戻す（事前準備の呼び出しを消費分から除く）。"""
    state_path = directory / "fake-gh-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["calls"] = 0
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def read_state(directory: Path) -> dict[str, object]:
    state_path = directory / "fake-gh-state.json"
    result = json.loads(state_path.read_text(encoding="utf-8"))
    assert isinstance(result, dict)
    return result


def make_context(
    directory: Path,
    *,
    scenario: str = "ok",
    timeout_seconds: float = 30.0,
    page_size: int = 100,
) -> GhContext:
    """fake ghを指すGhContextを作る（state未作成ならseedする）。"""
    fake = directory / "fake_gh.py"
    if not fake.exists():
        write_fake_gh(directory)
    state_path = directory / "fake-gh-state.json"
    if not state_path.exists():
        seed_state(directory)
    env = dict(child_env())
    env["CC_REVIEW_FAKE_GH_STATE"] = str(state_path)
    env["CC_REVIEW_FAKE_GH_SCENARIO"] = scenario
    env["CC_REVIEW_FAKE_GH_PAGE_SIZE"] = str(page_size)
    return GhContext(
        gh_command=(sys.executable, str(fake)),
        env=env,
        workdir=directory,
        timeout_seconds=timeout_seconds,
        grace_seconds=1.0,
    )


@dataclass
class SleepRecorder:
    """retry testの待機を記録する注入sleep。"""

    calls: list[float] = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_policy(
    *,
    max_attempts: int = 3,
    backoff_seconds: float = 0.5,
    max_wait_seconds: float = 60.0,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        max_wait_seconds=max_wait_seconds,
        sleep=sleep if sleep is not None else SleepRecorder(),
        now=now if now is not None else time.monotonic,
    )
