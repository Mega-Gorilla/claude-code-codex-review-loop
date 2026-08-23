# SPDX-License-Identifier: Apache-2.0
"""PR lockの受入test（ADR-0011）。

- 取得は排他。既に他runが持っていれば理由つきで停止する
- **回収は3条件（pid非生存・host一致・run一致）がすべて揃った場合のみ**
- 壊れたlockは「無いもの」として上書きせず、構造化errorとして提示する
- 解放は自runのlockに限る

同時run拒否のworkflow動作（AC-C10-03）はC-10の責務であり、ここでは永続表現と照合を
検証する。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from c07_support.helpers import ACQUIRED_AT, HEAD, NUMBER, REPOSITORY, RUN, lock_payload, state_paths

from claude_code_codex_review_loop.identity.fs_permissions import verify_private_file
from claude_code_codex_review_loop.state import (
    LockAcquired,
    LockCorrupt,
    LockHeld,
    LockOwner,
    LockUnavailable,
    acquire_pr_lock,
    current_host,
    inspect_pr_lock,
    lock_path,
    release_pr_lock,
)
from claude_code_codex_review_loop.state import lock as lock_module

DEAD_PID = 424242
HOST = "test-host"


def _path(tmp_path: Path) -> Path:
    return lock_path(state_paths(tmp_path), REPOSITORY, NUMBER)


def _acquire(path: Path, **overrides: object) -> object:
    arguments: dict[str, object] = {
        "run_id": RUN,
        "repository": REPOSITORY,
        "number": NUMBER,
        "acquired_at": ACQUIRED_AT,
        "host": HOST,
        "pid": os.getpid(),
    }
    arguments.update(overrides)
    return acquire_pr_lock(path, **arguments)  # type: ignore[arg-type]


def _seed(path: Path, **overrides: object) -> None:
    """既存lockを直接書く（別processが取得した状態の再現）。"""
    from claude_code_codex_review_loop.identity.fs_permissions import write_private_text

    write_private_text(path, json.dumps(lock_payload(**overrides), ensure_ascii=False))


class TestAcquire:
    def test_acquires_when_absent(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        result = _acquire(path, head_sha=HEAD)
        assert isinstance(result, LockAcquired)
        assert result.owner.run_id == RUN and result.owner.head_sha == HEAD
        verify_private_file(path)

    def test_lock_file_is_schema_valid(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        _acquire(path)
        owner = inspect_pr_lock(path)
        assert isinstance(owner, LockOwner)
        assert (owner.repository, owner.number, owner.host) == (REPOSITORY, NUMBER, HOST)

    def test_second_run_is_rejected_while_holder_is_alive(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        _acquire(path)
        result = _acquire(path, run_id="run-2")
        assert isinstance(result, LockHeld)
        assert result.owner.run_id == RUN and "別run" in result.reason

    def test_same_run_is_rejected_while_process_is_alive(self, tmp_path: Path) -> None:
        """同一runでも、保持processが生きていれば奪わない（二重起動の防止）。"""
        path = _path(tmp_path)
        _acquire(path)
        result = _acquire(path, pid=os.getpid() + 0)
        assert isinstance(result, LockHeld) and "生存" in result.reason

    def test_inspect_does_not_acquire(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        assert inspect_pr_lock(path) is None
        assert not path.exists()


class TestReclaim:
    """回収の3条件の真理表（1つでも欠ければ回収しない）。"""

    def test_reclaims_when_all_three_conditions_hold(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        _seed(path, pid=DEAD_PID, host=HOST, run_id=RUN)
        result = _acquire(path)
        assert isinstance(result, LockAcquired)
        assert result.owner.pid == os.getpid()

    def test_does_not_reclaim_when_process_is_alive(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        _seed(path, pid=os.getpid(), host=HOST, run_id=RUN)
        result = _acquire(path)
        assert isinstance(result, LockHeld) and "生存" in result.reason

    def test_does_not_reclaim_across_hosts(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        _seed(path, pid=DEAD_PID, host="other-host", run_id=RUN)
        result = _acquire(path)
        assert isinstance(result, LockHeld) and "host" in result.reason

    def test_does_not_reclaim_other_run(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        _seed(path, pid=DEAD_PID, host=HOST, run_id="run-other")
        result = _acquire(path)
        assert isinstance(result, LockHeld) and "別run" in result.reason

    def test_ambiguous_liveness_is_treated_as_alive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """生存判定が曖昧な場合（権限不足等）は回収しない側へ倒れる。"""
        path = _path(tmp_path)
        _seed(path, pid=DEAD_PID, host=HOST, run_id=RUN)
        monkeypatch.setattr(lock_module, "is_process_alive", lambda pid: True)
        result = _acquire(path)
        assert isinstance(result, LockHeld) and "生存" in result.reason

    def test_reclaim_detects_losing_the_race(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回収直後に別processが取得していたら、書込後の読み戻しで検出する。"""
        path = _path(tmp_path)
        _seed(path, pid=DEAD_PID, host=HOST, run_id=RUN)
        original = lock_module.replace_private_text

        def _replace_then_steal(target: Path, text: str) -> None:
            original(target, text)
            original(target, json.dumps(lock_payload(pid=DEAD_PID + 1, host=HOST, run_id=RUN)))

        monkeypatch.setattr(lock_module, "replace_private_text", _replace_then_steal)
        result = _acquire(path)
        assert isinstance(result, LockHeld) and "別process" in result.reason


    def test_reclaim_detects_unreadable_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回収後の読み戻しが解釈不能なら、取得成功と見なさない。"""
        path = _path(tmp_path)
        _seed(path, pid=DEAD_PID, host=HOST, run_id=RUN)
        original = lock_module.replace_private_text

        def _replace_then_corrupt(target: Path, text: str) -> None:
            original(target, text)
            original(target, "{ not json")

        monkeypatch.setattr(lock_module, "replace_private_text", _replace_then_corrupt)
        assert isinstance(_acquire(path), LockUnavailable)


class TestCorruptAndUnavailable:
    def test_corrupt_lock_is_not_overwritten(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        from claude_code_codex_review_loop.identity.fs_permissions import write_private_text

        write_private_text(path, "{ not json")
        result = _acquire(path)
        assert isinstance(result, LockCorrupt) and result.stage == "json"
        assert path.read_text(encoding="utf-8") == "{ not json"

    def test_missing_field_is_corrupt(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        from claude_code_codex_review_loop.identity.fs_permissions import write_private_text

        payload = lock_payload()
        del payload["pid"]
        write_private_text(path, json.dumps(payload))
        result = _acquire(path)
        assert isinstance(result, LockCorrupt) and result.stage == "schema"

    def test_permission_violation_is_reported(self, tmp_path: Path) -> None:
        """作成者限定でないlockは扱わない（AC-C06-05の維持）。"""
        path = _path(tmp_path)
        outside = tmp_path.resolve() / "shared.lock"
        outside.write_text(json.dumps(lock_payload()), encoding="utf-8")
        try:
            path.hardlink_to(outside)
        except (OSError, NotImplementedError):  # pragma: no cover - hard link不可環境
            pytest.skip("hard linkを作成できない環境")
        assert isinstance(_acquire(path), LockUnavailable)

    def test_create_failure_is_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """排他作成の失敗（直前に別processが取得した等）を成功と推測しない。"""
        path = _path(tmp_path)

        def _fail(target: Path, text: str) -> None:
            raise lock_module.FsPermissionError("create_file", "作成できない", 17)

        monkeypatch.setattr(lock_module, "write_private_text", _fail)
        assert isinstance(_acquire(path), LockUnavailable)


class TestRelease:
    def test_releases_own_lock(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        _acquire(path)
        assert release_pr_lock(path, run_id=RUN) is True
        assert not path.exists()

    def test_does_not_release_other_run(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        _seed(path, pid=os.getpid(), host=HOST, run_id="run-other")
        assert release_pr_lock(path, run_id=RUN) is False
        assert path.exists()

    def test_does_not_release_other_pid(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        _seed(path, pid=DEAD_PID, host=HOST, run_id=RUN)
        assert release_pr_lock(path, run_id=RUN) is False
        assert path.exists()

    def test_absent_lock_is_not_an_error(self, tmp_path: Path) -> None:
        assert release_pr_lock(_path(tmp_path), run_id=RUN) is False


def test_current_host_is_non_empty() -> None:
    assert current_host()
