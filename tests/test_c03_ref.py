# SPDX-License-Identifier: Apache-2.0
"""C-03のref再停止（stop_tree_by_ref）の受入test。

元のTreeHandleを持たない呼び出し側（別processを含む）からの再停止と冪等性を検証する。
POSIXの注意（docstringどおり）: 呼び出しprocessが直接childの親のままだと未reapの
zombieがgroupを生存として見せるため、by-refのtestはleaderを先にreapしたtreeで行う。
"""

from __future__ import annotations

import os
import subprocess
import sys
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
    JobObjectRef,
    ProcessGroupRef,
    SpawnSpec,
    StopError,
    StopMethod,
    TreeHandle,
    spawn_tree,
    stop_tree_by_ref,
)


def _leaderless_tree(tmp_path: Path, mode: str = "exit_after_spawn") -> tuple[TreeHandle, int | None]:
    """leaderが終了・reap済みで、孫だけが生き残ったtreeを作る。(handle, grandchild_pid)を返す。"""
    script = write_child_script(tmp_path)
    pidfile = tmp_path / f"ref-pids-{mode}.txt"
    spec = SpawnSpec(argv=child_argv(script, mode, pidfile, grandchild=True), cwd=tmp_path, env=child_env())
    handle = spawn_tree(spec)
    assert handle.wait(WAIT_LIMIT_SECONDS) == 0  # leaderをreapする
    _child_pid, grandchild_pid = read_pids(pidfile)
    return handle, grandchild_pid


def test_stop_by_ref_kills_surviving_grandchild(tmp_path: Path) -> None:
    handle, grandchild_pid = _leaderless_tree(tmp_path)
    try:
        assert grandchild_pid is not None
        result = stop_tree_by_ref(handle.ref, grace_seconds=5.0)
        assert result.method is not StopMethod.ALREADY_EXITED
        assert wait_until(lambda: tree_gone(grandchild_pid))
        again = stop_tree_by_ref(handle.ref, grace_seconds=1.0)
        assert again.method is StopMethod.ALREADY_EXITED
    finally:
        handle.close()


def test_stop_by_ref_forces_ignoring_grandchild(tmp_path: Path) -> None:
    """graceful要求を無視する孫は、grace超過後にby-ref経路でも強制停止される。

    WindowsはSIG_IGNでもCTRL_BREAKでprocessが終了し得る（CRT既定handlerの挙動）ため、
    FORCEDの断言はSIGTERMのSIG_IGNが確実なPOSIXのみで行う。Windowsの決定的なFORCED
    分岐はtest_c03_windows.pyの注入testで固定する。
    """
    handle, grandchild_pid = _leaderless_tree(tmp_path, mode="exit_ignore")
    try:
        assert grandchild_pid is not None
        result = stop_tree_by_ref(handle.ref, grace_seconds=0.3)
        assert result.method is not StopMethod.ALREADY_EXITED
        if sys.platform != "win32":
            assert result.method is StopMethod.FORCED
        assert wait_until(lambda: tree_gone(grandchild_pid))
    finally:
        handle.close()


def test_stop_by_ref_of_fully_exited_tree_is_immediate(tmp_path: Path) -> None:
    script = write_child_script(tmp_path)
    pidfile = tmp_path / "ref-quick.txt"
    spec = SpawnSpec(
        argv=child_argv(script, "quick_exit", pidfile, grandchild=False), cwd=tmp_path, env=child_env()
    )
    handle = spawn_tree(spec)
    ref = handle.ref
    assert handle.wait(WAIT_LIMIT_SECONDS) == 0
    handle.close()
    result = stop_tree_by_ref(ref, grace_seconds=1.0)
    assert result.method is StopMethod.ALREADY_EXITED
    assert result.graceful_requested is False


def test_mismatched_ref_type_is_rejected() -> None:
    if sys.platform == "win32":
        foreign: JobObjectRef | ProcessGroupRef = ProcessGroupRef(pid=1, pgid=1)
    else:
        foreign = JobObjectRef(pid=1, job_name="Local\\cc-review-none")
    with pytest.raises(StopError) as excinfo:
        stop_tree_by_ref(foreign, grace_seconds=1.0)
    assert excinfo.value.stage == "ref_mismatch"


def test_stop_by_ref_from_another_process(tmp_path: Path) -> None:
    """別processが（envを継承した通常のPythonとして）refだけからtreeを停止できる。"""
    handle, grandchild_pid = _leaderless_tree(tmp_path)
    try:
        assert grandchild_pid is not None
        ref = handle.ref
        if sys.platform == "win32":
            second_field = ref.job_name
        else:
            second_field = str(ref.pgid)
        code = (
            "import sys\n"
            "from claude_code_codex_review_loop.process import JobObjectRef, ProcessGroupRef, stop_tree_by_ref\n"
            "if sys.platform == 'win32':\n"
            "    ref = JobObjectRef(pid=int(sys.argv[1]), job_name=sys.argv[2])\n"
            "else:\n"
            "    ref = ProcessGroupRef(pid=int(sys.argv[1]), pgid=int(sys.argv[2]))\n"
            "result = stop_tree_by_ref(ref, 5.0)\n"
            "sys.exit(0 if result.method.value in ('GRACEFUL', 'FORCED') else 8)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code, str(ref.pid), second_field],
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=WAIT_LIMIT_SECONDS,
        )
        assert completed.returncode == 0, completed.stderr
        assert wait_until(lambda: tree_gone(grandchild_pid))
    finally:
        handle.close()
