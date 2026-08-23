# SPDX-License-Identifier: Apache-2.0
"""C-07 local state testの共有helper。

state rootの用意と、最小のcheckpoint / lock payloadを組み立てる。時刻は注入引数
（製品codeが時刻sourceを持たないため、testでも固定値を渡す）。
"""

from __future__ import annotations

from pathlib import Path

from claude_code_codex_review_loop.state import StatePaths, prepare_state_root

RUN = "run-1"
REPOSITORY = "owner/repo"
NUMBER = 12
HEAD = "a" * 40
ACQUIRED_AT = "2026-08-23T10:00:00Z"


def state_paths(tmp_path: Path, *, name: str = "state") -> StatePaths:
    """tmp_path配下へstate rootを用意する（作成者限定で作られる）。"""
    return prepare_state_root(tmp_path.resolve() / name)


def checkpoint_payload(**overrides: object) -> dict[str, object]:
    """schema検証を通る最小のcheckpoint payload。"""
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": RUN,
        "repository": REPOSITORY,
        "number": NUMBER,
    }
    payload.update(overrides)
    return payload


def lock_payload(**overrides: object) -> dict[str, object]:
    """schema検証を通る最小のlock payload（破損caseの素材にも使う）。"""
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": RUN,
        "repository": REPOSITORY,
        "number": NUMBER,
        "pid": 424242,
        "host": "test-host",
        "acquired_at": ACQUIRED_AT,
    }
    payload.update(overrides)
    return payload
