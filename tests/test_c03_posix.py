# SPDX-License-Identifier: Apache-2.0
"""C-03 POSIX backend（process group）の受入test。Windowsでは自動skipされる。

zombie reap・killpgの冪等性・pid再利用検知・注入による失敗経路を検証する。
"""

from __future__ import annotations

import os
import signal
import threading
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

from claude_code_codex_review_loop.process import ProcessGroupRef, SpawnSpec, StopError, StopMethod, spawn_tree

process_group = pytest.importorskip(
    "claude_code_codex_review_loop.process.process_group", reason="POSIX専用backendの検証", exc_type=ImportError
)


def test_zombie_child_is_reaped_by_alive_check(tmp_path: Path) -> None:
    """未reapのzombieがtree生存として観測され続けない（alive_in_treeがreapを兼ねる）。"""
    script = write_child_script(tmp_path)
    pidfile = tmp_path / "zombie.txt"
    spec = SpawnSpec(argv=child_argv(script, "quick_exit", pidfile, grandchild=False), cwd=tmp_path, env=child_env())
    handle = spawn_tree(spec)
    try:
        # handle.wait()を呼ばずに、alive_in_tree()だけでFalseへ収束することを確認する
        assert wait_until(lambda: not handle.alive_in_tree())
        assert handle.poll() == 0
    finally:
        handle.close()


def test_signal_group_is_idempotent_after_group_is_gone(tmp_path: Path) -> None:
    script = write_child_script(tmp_path)
    pidfile = tmp_path / "gone.txt"
    spec = SpawnSpec(argv=child_argv(script, "quick_exit", pidfile, grandchild=False), cwd=tmp_path, env=child_env())
    handle = spawn_tree(spec)
    assert handle.wait(WAIT_LIMIT_SECONDS) == 0
    handle.close()
    assert process_group._signal_group(handle.ref.pgid, signal.SIGTERM) is False


def test_signal_group_unexpected_error_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    """EPERM等のESRCH以外の失敗は構造化errorになる（monkeypatchで注入）。"""

    def _eperm(pgid: int, sig: int) -> None:
        raise PermissionError(1, "注入したEPERM")

    monkeypatch.setattr(process_group.os, "killpg", _eperm)
    with pytest.raises(StopError) as excinfo:
        process_group._signal_group(999_999, signal.SIGTERM)
    assert excinfo.value.stage == "signal_group"
    assert excinfo.value.os_error == 1


def test_stop_by_ref_detects_pid_reuse() -> None:
    """leaderのpgid照合が一致しないrefはALREADY_EXITEDとして扱う（誤killしない）。"""
    own_pgid = os.getpgid(os.getpid())
    ref = ProcessGroupRef(pid=os.getpid(), pgid=own_pgid + 1)
    result = process_group.stop_by_ref(ref, grace_seconds=0.5)
    assert result.method is StopMethod.ALREADY_EXITED


def test_stop_by_ref_with_live_leader_uses_group_match(tmp_path: Path) -> None:
    """leaderが生存しているtreeへのby-ref停止（pgid照合の一致分岐）。

    by-refの呼び出し側は直接childをreapできないため、handle.poll()を回す
    reaper threadを併走させてzombie滞留を避ける（docstringの注意の再現）。
    """
    script = write_child_script(tmp_path)
    pidfile = tmp_path / "live-leader.txt"
    spec = SpawnSpec(argv=child_argv(script, "ignore", pidfile, grandchild=True), cwd=tmp_path, env=child_env())
    handle = spawn_tree(spec)
    stop_reaper = threading.Event()

    def _reaper() -> None:
        while not stop_reaper.is_set():
            handle.poll()
            stop_reaper.wait(0.02)

    reaper = threading.Thread(target=_reaper)
    reaper.start()
    try:
        child_pid, grandchild_pid = read_pids(pidfile)
        result = process_group.stop_by_ref(handle.ref, grace_seconds=0.3)
        assert result.method is StopMethod.FORCED  # ignore treeはgrace超過で強制停止
        assert wait_until(lambda: tree_gone(child_pid, grandchild_pid))
    finally:
        stop_reaper.set()
        reaper.join(timeout=5.0)
        handle.close()
