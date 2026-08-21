# SPDX-License-Identifier: Apache-2.0
"""C-03のtree停止・timeout・二段階escalationの受入test（AC-C03-01 / AC-C03-02）。

親・子・孫の3階層treeを実際に起動し、timeout後と停止後に孫が残らないこと、
graceful -> forceの二段階が機能することを両OSで検証する。OS実挙動に依存して
分岐が揺れる箇所（Windows consoleの有無等）は、fake handleによる決定的なtestで
facadeの全分岐を固定する。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from c03_support.helpers import (
    WAIT_LIMIT_SECONDS,
    child_argv,
    child_env,
    read_pids,
    tree_gone,
    wait_until,
    write_child_script,
)

from claude_code_codex_review_loop.process import (
    Completed,
    SpawnSpec,
    StopError,
    StopMethod,
    TimedOut,
    TreeHandle,
    run_tree,
    spawn_tree,
    stop_tree,
)
from claude_code_codex_review_loop.process import terminate as terminate_module


def _tree_spec(tmp_path: Path, mode: str, *, grandchild: bool = True) -> tuple[SpawnSpec, Path]:
    script = write_child_script(tmp_path)
    pidfile = tmp_path / f"pids-{mode}.txt"
    spec = SpawnSpec(
        argv=child_argv(script, mode, pidfile, grandchild=grandchild),
        cwd=tmp_path,
        env=child_env(),
    )
    return spec, pidfile


def test_timeout_stops_entire_tree(tmp_path: Path) -> None:
    """AC-C03-01: timeout後に子も孫も残らない。graceful要求を無視するtreeで強制停止へ到達する。"""
    spec, pidfile = _tree_spec(tmp_path, "ignore")
    result = run_tree(spec, timeout_seconds=2.0, grace_seconds=0.5)
    assert isinstance(result, TimedOut)
    assert result.stop_result.method is StopMethod.FORCED
    child_pid, grandchild_pid = read_pids(pidfile)
    assert grandchild_pid is not None
    assert wait_until(lambda: tree_gone(child_pid, grandchild_pid))


def test_graceful_stop_of_cooperative_tree(tmp_path: Path) -> None:
    """AC-C03-02前半: graceful要求に応答するtreeはgrace期間内に停止する。

    WindowsのCTRL_BREAK配送はconsole構成に依存するため、本testの決定的な
    graceful検証はPOSIXで行い、Windowsはtest_c03_windows.pyのconsole-owner
    patternで検証する（ここでは全滅のみを断言する）。
    """
    spec, pidfile = _tree_spec(tmp_path, "cooperative")
    handle = spawn_tree(spec)
    try:
        child_pid, grandchild_pid = read_pids(pidfile)
        result = stop_tree(handle, grace_seconds=WAIT_LIMIT_SECONDS)
        assert result.method is not StopMethod.ALREADY_EXITED
        if sys.platform != "win32":
            assert result.method is StopMethod.GRACEFUL
            assert result.graceful_requested is True
            assert handle.poll() == 0  # cooperative childはhandlerからexit 0する
        assert wait_until(lambda: tree_gone(child_pid, grandchild_pid))
    finally:
        handle.close()


def test_ignoring_tree_is_forced_after_grace(tmp_path: Path) -> None:
    """graceful要求を無視するtreeはgrace超過後に強制停止される。再停止は冪等。"""
    spec, pidfile = _tree_spec(tmp_path, "ignore")
    handle = spawn_tree(spec)
    try:
        child_pid, grandchild_pid = read_pids(pidfile)
        result = stop_tree(handle, grace_seconds=0.3)
        assert result.method is StopMethod.FORCED
        assert wait_until(lambda: tree_gone(child_pid, grandchild_pid))
        again = stop_tree(handle, grace_seconds=0.3)
        assert again.method is StopMethod.ALREADY_EXITED
    finally:
        handle.close()


def test_second_stage_force_interrupts_grace_wait(tmp_path: Path) -> None:
    """AC-C03-02後半: grace待機中のforce_stop（2回目のCtrl+C相当）で即時に全滅する。"""
    spec, pidfile = _tree_spec(tmp_path, "ignore")
    handle = spawn_tree(spec)
    results: list[object] = []
    try:
        child_pid, grandchild_pid = read_pids(pidfile)

        def _first_stage() -> None:
            results.append(stop_tree(handle, grace_seconds=WAIT_LIMIT_SECONDS))

        worker = threading.Thread(target=_first_stage)
        started = time.monotonic()
        worker.start()
        time.sleep(0.3)  # graceful要求が発行されgrace待機に入るまでの余裕
        handle.force_stop()
        worker.join(timeout=10.0)
        assert not worker.is_alive()
        assert time.monotonic() - started < WAIT_LIMIT_SECONDS  # grace満了を待たずに完了した
        assert len(results) == 1
        assert wait_until(lambda: tree_gone(child_pid, grandchild_pid))
    finally:
        handle.close()


def test_stop_after_natural_exit_is_idempotent(tmp_path: Path) -> None:
    """C-01契約: 実行中processが無ければ停止完了は即時返る。二重forceとcloseも安全。"""
    spec, pidfile = _tree_spec(tmp_path, "quick_exit", grandchild=False)
    handle = spawn_tree(spec)
    try:
        assert handle.wait(WAIT_LIMIT_SECONDS) == 0
        child_pid, _grandchild = read_pids(pidfile)
        assert handle.pid == child_pid
        result = stop_tree(handle, grace_seconds=0.2)
        assert result.method is StopMethod.ALREADY_EXITED
        handle.force_stop()
        handle.force_stop()
    finally:
        handle.close()
        handle.close()  # closeも冪等


def test_completed_run_cleans_leftover_grandchild(tmp_path: Path) -> None:
    """AC-C03-01: 直接childが正常終了しても、残った孫はrun_treeが返る前に掃除される。"""
    spec, pidfile = _tree_spec(tmp_path, "exit_after_spawn")
    result = run_tree(spec, timeout_seconds=WAIT_LIMIT_SECONDS, grace_seconds=1.0)
    assert result == Completed(exit_code=0)
    child_pid, grandchild_pid = read_pids(pidfile)
    assert grandchild_pid is not None
    assert wait_until(lambda: tree_gone(child_pid, grandchild_pid))


class _FakeHandle:
    """facadeの分岐をOS実挙動から独立して固定するためのfake。TreeHandle protocol互換。"""

    def __init__(self, alive_sequence: list[bool], requested: bool) -> None:
        self._alive = list(alive_sequence)
        self._requested = requested
        self.force_calls = 0
        self.closed = False

    @property
    def pid(self) -> int:
        return 12345

    @property
    def ref(self) -> object:
        raise NotImplementedError

    def poll(self) -> int | None:
        return None

    def wait(self, timeout_seconds: float) -> int | None:
        return None

    def alive_in_tree(self) -> bool:
        if len(self._alive) > 1:
            return self._alive.pop(0)
        return self._alive[0]

    def request_graceful_stop(self) -> bool:
        return self._requested

    def force_stop(self) -> None:
        self.force_calls += 1

    def close(self) -> None:
        self.closed = True


def _as_handle(fake: _FakeHandle) -> TreeHandle:
    return fake


class TestFacadeBranches:
    def test_request_refused_and_tree_gone_is_already_exited(self) -> None:
        """graceful要求が受理されず、その時点でtreeも消滅していれば即時成功。"""
        fake = _FakeHandle(alive_sequence=[True, False], requested=False)
        result = stop_tree(_as_handle(fake), grace_seconds=5.0)
        assert result.method is StopMethod.ALREADY_EXITED
        assert fake.force_calls == 0

    def test_request_refused_and_alive_goes_straight_to_force(self) -> None:
        """graceful要求が受理されない場合（console不在等）、grace待機を飛ばして強制停止する。"""
        fake = _FakeHandle(alive_sequence=[True, True, False], requested=False)
        result = stop_tree(_as_handle(fake), grace_seconds=30.0)
        assert result.method is StopMethod.FORCED
        assert result.graceful_requested is False
        assert fake.force_calls == 1

    def test_graceful_exit_within_grace(self) -> None:
        fake = _FakeHandle(alive_sequence=[True, False], requested=True)
        result = stop_tree(_as_handle(fake), grace_seconds=5.0)
        assert result.method is StopMethod.GRACEFUL
        assert result.graceful_requested is True
        assert fake.force_calls == 0

    def test_force_failure_raises_stop_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """強制停止後もtreeが残存する場合は構造化errorになり、停止commandを再発行できる。"""
        monkeypatch.setattr(terminate_module, "_FORCE_CONFIRM_SECONDS", 0.2)
        fake = _FakeHandle(alive_sequence=[True], requested=False)
        with pytest.raises(StopError) as excinfo:
            stop_tree(_as_handle(fake), grace_seconds=0.1)
        assert excinfo.value.stage == "force_stop"
        assert fake.force_calls == 1
