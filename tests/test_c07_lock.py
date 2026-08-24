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
import threading
from pathlib import Path

import pytest
from c07_support.helpers import ACQUIRED_AT, HEAD, NUMBER, REPOSITORY, RUN, lock_payload, state_paths

from claude_code_codex_review_loop.identity import fs_permissions
from claude_code_codex_review_loop.identity.fs_permissions import verify_private_file, write_private_text
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
from claude_code_codex_review_loop.state.lock import LockInputError

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


class TestAcquireIsSerialized:
    """取得（新規・回収の別を問わず）は排他guardの下で直列化される。"""

    def test_second_acquire_of_a_reclaimed_lock_is_held(self, tmp_path: Path) -> None:
        """回収に成功した直後は自processが生存者になるため、次の取得は停止する。"""
        path = _path(tmp_path)
        _seed(path, pid=DEAD_PID, host=HOST, run_id=RUN)
        first = _acquire(path, acquired_at="t1")
        second = _acquire(path, acquired_at="t2")
        assert isinstance(first, LockAcquired)
        assert isinstance(second, LockHeld) and "生存" in second.reason
        owner = inspect_pr_lock(path)
        assert isinstance(owner, LockOwner) and owner.acquired_at == "t1"

    @pytest.mark.parametrize("seeded", [False, True], ids=["absent", "stale"])
    def test_concurrent_contenders_produce_one_acquisition(self, tmp_path: Path, seeded: bool) -> None:
        """barrierで同期した2 contenderを走らせ、LockAcquiredが1件だけであることを確認する。"""
        path = _path(tmp_path)
        if seeded:
            _seed(path, pid=DEAD_PID, host=HOST, run_id=RUN)
        barrier = threading.Barrier(2)
        results: list[object] = []
        collected = threading.Lock()

        def _contend(tag: str) -> None:
            barrier.wait(timeout=5)
            try:
                result: object = _acquire(path, acquired_at=tag, pid=os.getpid())
            except BaseException as error:  # 例外も結果として集める（握り潰さない）
                result = error
            with collected:
                results.append(result)

        threads = [threading.Thread(target=_contend, args=(tag,)) for tag in ("t1", "t2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert [type(result).__name__ for result in results if isinstance(result, BaseException)] == []
        assert len(results) == 2
        assert sum(isinstance(result, LockAcquired) for result in results) == 1
        # 一時fileもguardも残さない（次の取得を妨げない）
        assert [entry.name for entry in path.parent.iterdir()] == [path.name]

    def test_guard_held_by_another_process_stops_acquisition(self, tmp_path: Path) -> None:
        """guardを他processが保持していれば、推測せず停止する（fail closed）。"""
        path = _path(tmp_path)
        guard = path.parent / f"{path.name}{lock_module.ACQUIRE_GUARD_SUFFIX}"
        guard.mkdir()
        result = _acquire(path)
        assert isinstance(result, LockUnavailable) and guard.name in result.detail
        assert not path.exists()
        # 復旧手順（guardの削除）はdetailが示すとおりで、実行すれば取得できる
        guard.rmdir()
        assert isinstance(_acquire(path), LockAcquired)

    def test_guard_is_released_after_each_outcome(self, tmp_path: Path) -> None:
        """取得できなかった場合もguardを残さない。"""
        path = _path(tmp_path)
        _seed(path, pid=os.getpid(), host=HOST, run_id="run-other")
        assert isinstance(_acquire(path), LockHeld)
        assert [entry.name for entry in path.parent.iterdir()] == [path.name]


class TestInterruptedAcquire:
    """中断しても「完全なlock」か「lockが無い」のどちらかへ収束する（回復不能にしない）。"""

    def test_interrupted_write_leaves_no_lock_and_stays_acquirable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """確定（os.replace）の前に落ちても、pathには何も残らず次回取得できる。"""
        path = _path(tmp_path)

        def _interrupt(source: object, destination: object) -> None:
            raise OSError(5, "interrupted")

        monkeypatch.setattr(fs_permissions.os, "replace", _interrupt)
        assert isinstance(_acquire(path), LockUnavailable)
        assert not path.exists()
        monkeypatch.undo()
        assert isinstance(_acquire(path), LockAcquired)

    def test_leftover_temporary_does_not_block_acquisition(self, tmp_path: Path) -> None:
        """中断で残り得るのは一意名の一時fileだけで、取得も検証も妨げない。"""
        path = _path(tmp_path)
        write_private_text(path.parent / f".{path.name}.deadbeef.tmp", "中断した書き込み")
        result = _acquire(path)
        assert isinstance(result, LockAcquired)
        assert isinstance(inspect_pr_lock(path), LockOwner)
        verify_private_file(path)

    def test_acquired_lock_is_not_shared_with_another_path(self, tmp_path: Path) -> None:
        """確定したlockはlink数1（公開後に一時fileが残ってlink数2になる経路を持たない）。"""
        path = _path(tmp_path)
        assert isinstance(_acquire(path), LockAcquired)
        assert path.stat().st_nlink == 1
        assert [entry.name for entry in path.parent.iterdir()] == [path.name]


class TestOwnerValidation:
    """consumerが受理できないlockをproducerに作らせない。"""

    @pytest.mark.parametrize(
        "override",
        [{"acquired_at": ""}, {"pid": 0}, {"pid": -1}, {"number": 0}, {"repository": ""}],
        ids=["empty_acquired_at", "pid_zero", "pid_negative", "number_zero", "empty_repository"],
    )
    def test_invalid_owner_is_rejected_before_writing(self, tmp_path: Path, override: dict[str, object]) -> None:
        path = _path(tmp_path)
        with pytest.raises(LockInputError):
            _acquire(path, **override)
        assert not path.exists()

    def test_written_lock_is_always_readable(self, tmp_path: Path) -> None:
        """取得に成功したlockは、必ずinspectで解釈できる（LockCorruptにならない）。"""
        path = _path(tmp_path)
        assert isinstance(_acquire(path), LockAcquired)
        assert isinstance(inspect_pr_lock(path), LockOwner)


class TestCorruptAndUnavailable:
    def test_corrupt_lock_is_not_overwritten(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        write_private_text(path, "{ not json")
        result = _acquire(path)
        assert isinstance(result, LockCorrupt) and result.stage == "json"
        assert path.read_text(encoding="utf-8") == "{ not json"

    def test_missing_field_is_corrupt(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
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

    def test_write_failure_is_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """lockを確定できなかった場合、取得できたと推測しない。"""
        path = _path(tmp_path)

        def _fail(target: Path, text: str) -> None:
            raise lock_module.FsPermissionError("create_file", "作成できない", 17)

        monkeypatch.setattr(lock_module, "replace_private_text", _fail)
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
