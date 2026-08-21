# SPDX-License-Identifier: Apache-2.0
"""C-03 Windows backend（Job Object）の受入test。POSIXでは自動skipされる。

- breakaway拒否・close安全網・named jobの実挙動
- console-owner pattern: CREATE_NEW_CONSOLEで起動した中継processが自分のconsole
  配下でtreeを起動しCTRL_BREAKのgraceful停止をE2Eで検証する（pytest自身のconsole
  有無に依存しない）
- 環境依存で通る枝が変わる失敗経路は、monkeypatch注入で両方向を決定的に固定する
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

from claude_code_codex_review_loop.process import Completed, SpawnError, SpawnSpec, StopError, run_tree, spawn_tree

job_object = pytest.importorskip(
    "claude_code_codex_review_loop.process.job_object", reason="Windows専用backendの検証", exc_type=ImportError
)


def _ignore_tree(tmp_path: Path) -> tuple[object, Path]:
    script = write_child_script(tmp_path)
    pidfile = tmp_path / "win-pids.txt"
    spec = SpawnSpec(argv=child_argv(script, "ignore", pidfile, grandchild=True), cwd=tmp_path, env=child_env())
    return spawn_tree(spec), pidfile


def test_breakaway_from_job_is_denied(tmp_path: Path) -> None:
    """job内の子はCREATE_BREAKAWAY_FROM_JOBの孫を起動できない（WinError 5で失敗する）。"""
    code = (
        "import subprocess, sys\n"
        "try:\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "                     creationflags=0x01000000)\n"
        "except OSError as exc:\n"
        "    sys.exit(5 if getattr(exc, 'winerror', None) == 5 else 6)\n"
        "sys.exit(7)\n"
    )
    spec = SpawnSpec(argv=(sys.executable, "-c", code), cwd=tmp_path, env=child_env())
    result = run_tree(spec, timeout_seconds=WAIT_LIMIT_SECONDS, grace_seconds=1.0)
    assert result == Completed(exit_code=5)


def test_close_terminates_tree_and_is_idempotent(tmp_path: Path) -> None:
    handle, pidfile = _ignore_tree(tmp_path)
    child_pid, grandchild_pid = read_pids(pidfile)
    handle.close()
    assert wait_until(lambda: tree_gone(child_pid, grandchild_pid))
    assert handle.alive_in_tree() is False  # close後はresource解放済みとして扱う
    handle.force_stop()  # no-op（冪等）
    handle.close()


def test_open_job_by_unknown_name_returns_none() -> None:
    assert job_object._open_job_by_name("Local\\cc-review-" + "0" * 32) is None


_OWNER_SCRIPT = """\
import sys
from pathlib import Path

from c03_support.helpers import child_argv, child_env, read_pids, write_child_script
from claude_code_codex_review_loop.process import SpawnSpec, StopMethod, spawn_tree, stop_tree

tmp = Path(sys.argv[1])
script = write_child_script(tmp)
pidfile = tmp / "owner-pids.txt"
spec = SpawnSpec(argv=child_argv(script, "cooperative", pidfile, grandchild=True), cwd=tmp, env=child_env())
handle = spawn_tree(spec)
try:
    read_pids(pidfile)
    result = stop_tree(handle, grace_seconds=20.0)
finally:
    handle.close()
ok = result.method is StopMethod.GRACEFUL and result.graceful_requested
sys.exit(0 if ok else 4)
"""


def test_console_owner_ctrl_break_e2e(tmp_path: Path) -> None:
    """AC-C03-02前半のWindows決定的検証。

    CREATE_NEW_CONSOLEのowner processが自分のconsoleでcooperative treeを起動し、
    CTRL_BREAKによるgraceful停止（GRACEFUL + graceful_requested）を確認する。
    ownerはenvを継承して起動するため、owner内のbackend実行はsubprocess coverageで
    親のcoverageへ合流する。
    """
    owner = tmp_path / "owner.py"
    owner.write_text(_OWNER_SCRIPT, encoding="utf-8")
    tests_dir = str(Path(__file__).resolve().parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = tests_dir + os.pathsep + env["PYTHONPATH"] if "PYTHONPATH" in env else tests_dir
    completed = subprocess.run(
        [sys.executable, str(owner), str(tmp_path)],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        env=env,
        capture_output=True,
        text=True,
        timeout=60.0,
    )
    assert completed.returncode == 0, (completed.returncode, completed.stdout, completed.stderr)


class TestInjectedFailures:
    """ctypes wrapperの失敗経路をmonkeypatchで決定的に踏む（CI環境の挙動へ依存しない）。"""

    def test_create_job_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job_object._kernel32, "CreateJobObjectW", lambda *args: None)
        monkeypatch.setattr(job_object, "_last_error", lambda: 87)
        with pytest.raises(SpawnError) as excinfo:
            job_object._create_named_job("Local\\cc-review-injected")
        assert (excinfo.value.stage, excinfo.value.os_error) == ("create_job", 87)

    def test_create_job_name_squatting_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job_object._kernel32, "CreateJobObjectW", lambda *args: 99)
        monkeypatch.setattr(job_object, "_last_error", lambda: 183)
        monkeypatch.setattr(job_object, "_close_handle", lambda handle: None)
        with pytest.raises(SpawnError) as excinfo:
            job_object._create_named_job("Local\\cc-review-squat")
        assert excinfo.value.os_error == 183

    def test_configure_failure_before_popen_cleans_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Popen前の失敗ではjob handleだけを解放する（popen不在分岐）。"""
        monkeypatch.setattr(job_object._kernel32, "SetInformationJobObject", lambda *args: 0)
        monkeypatch.setattr(job_object, "_last_error", lambda: 6)
        spec = SpawnSpec(argv=(sys.executable, "-c", "pass"), cwd=Path.cwd(), env=child_env())
        with pytest.raises(SpawnError) as excinfo:
            spawn_tree(spec)
        assert excinfo.value.stage == "configure_job"

    def test_assign_failure_kills_suspended_child(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """assign失敗時、suspendedの子とredirect fileを残さない（放置すると永久に残る）。"""
        observed: list[int] = []
        original = job_object._open_process_for_assign

        def _recording(pid: int) -> int:
            observed.append(pid)
            return original(pid)

        monkeypatch.setattr(job_object, "_open_process_for_assign", _recording)
        monkeypatch.setattr(job_object._kernel32, "AssignProcessToJobObject", lambda *args: 0)
        monkeypatch.setattr(job_object, "_last_error", lambda: 5)
        out = tmp_path / "assign-fail.txt"
        spec = SpawnSpec(
            argv=(sys.executable, "-c", "import time; time.sleep(60)"),
            cwd=tmp_path,
            env=child_env(),
            stdout_path=out,
        )
        with pytest.raises(SpawnError) as excinfo:
            spawn_tree(spec)
        assert (excinfo.value.stage, excinfo.value.os_error) == ("assign_job", 5)
        assert len(observed) == 1
        assert wait_until(lambda: tree_gone(observed[0]))
        out.unlink()  # fileが閉じられていないとWindowsでPermissionErrorになる

    def test_open_process_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job_object._kernel32, "OpenProcess", lambda *args: None)
        monkeypatch.setattr(job_object, "_last_error", lambda: 5)
        with pytest.raises(SpawnError) as excinfo:
            job_object._open_process_for_assign(4_000_000_000)
        assert excinfo.value.stage == "open_process"

    def test_snapshot_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job_object._kernel32, "CreateToolhelp32Snapshot", lambda *args: 0)
        monkeypatch.setattr(job_object, "_last_error", lambda: 31)
        with pytest.raises(SpawnError) as excinfo:
            job_object._create_thread_snapshot()
        assert excinfo.value.os_error == 31

    def test_snapshot_bad_length_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job_object._kernel32, "CreateToolhelp32Snapshot", lambda *args: 0)
        monkeypatch.setattr(job_object, "_last_error", lambda: job_object._ERROR_BAD_LENGTH)
        assert job_object._create_thread_snapshot() is None

    def test_drain_job_polls_until_drained_or_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """drain loopの両出口（途中で全滅 / deadline超過）を決定的に踏む。"""
        counts = iter([1, 1, 0])
        monkeypatch.setattr(job_object, "_query_active_processes", lambda job: next(counts))
        assert job_object._drain_job(0, seconds=5.0) is True
        monkeypatch.setattr(job_object, "_query_active_processes", lambda job: 1)
        assert job_object._drain_job(0, seconds=0.15) is False

    def test_snapshot_bad_length_is_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"count": 0}
        original = job_object._create_thread_snapshot

        def _flaky() -> int | None:
            calls["count"] += 1
            if calls["count"] == 1:
                return None  # ERROR_BAD_LENGTH相当
            return original()

        monkeypatch.setattr(job_object, "_create_thread_snapshot", _flaky)
        thread_id = job_object._find_single_thread(os.getpid())
        assert thread_id > 0
        assert calls["count"] == 2

    def test_snapshot_retry_exhausted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job_object, "_create_thread_snapshot", lambda: None)
        with pytest.raises(SpawnError) as excinfo:
            job_object._find_single_thread(os.getpid())
        assert excinfo.value.os_error == job_object._ERROR_BAD_LENGTH

    def test_thread_not_found_for_unknown_pid(self) -> None:
        with pytest.raises(SpawnError) as excinfo:
            job_object._find_single_thread(4_000_000_000)
        assert excinfo.value.stage == "resume"

    def test_open_thread_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job_object, "_find_single_thread", lambda pid: 1234)
        monkeypatch.setattr(job_object._kernel32, "OpenThread", lambda *args: None)
        monkeypatch.setattr(job_object, "_last_error", lambda: 5)
        with pytest.raises(SpawnError):
            job_object._resume_main_thread(4_000_000_000)

    def test_resume_thread_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job_object, "_find_single_thread", lambda pid: 1234)
        monkeypatch.setattr(job_object._kernel32, "OpenThread", lambda *args: 77)
        monkeypatch.setattr(job_object._kernel32, "ResumeThread", lambda *args: job_object._RESUME_FAILED)
        monkeypatch.setattr(job_object, "_close_handle", lambda handle: None)
        monkeypatch.setattr(job_object, "_last_error", lambda: 6)
        with pytest.raises(SpawnError):
            job_object._resume_main_thread(4_000_000_000)

    def test_query_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job_object._kernel32, "QueryInformationJobObject", lambda *args: 0)
        monkeypatch.setattr(job_object, "_last_error", lambda: 6)
        with pytest.raises(StopError) as excinfo:
            job_object._query_active_processes(1)
        assert excinfo.value.stage == "query_job"

    def test_terminate_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job_object._kernel32, "TerminateJobObject", lambda *args: 0)
        monkeypatch.setattr(job_object, "_last_error", lambda: 6)
        with pytest.raises(StopError) as excinfo:
            job_object._terminate_job(1)
        assert excinfo.value.stage == "terminate_job"

    def test_open_job_unexpected_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job_object._kernel32, "OpenJobObjectW", lambda *args: None)
        monkeypatch.setattr(job_object, "_last_error", lambda: 5)
        with pytest.raises(StopError) as excinfo:
            job_object._open_job_by_name("Local\\cc-review-any")
        assert excinfo.value.stage == "open_job"

    def test_send_ctrl_break_reports_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(pid: int, sig: int) -> None:
            raise OSError("console不在の注入")

        monkeypatch.setattr(job_object.os, "kill", _raise)
        assert job_object._send_ctrl_break(1234) is False

    def test_send_ctrl_break_reports_request_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(job_object.os, "kill", lambda pid, sig: None)
        assert job_object._send_ctrl_break(1234) is True

    def test_stop_by_ref_graceful_branch(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """by-refのgraceful成立分岐を、配送と観測を注入して決定的に踏む。"""
        script = write_child_script(tmp_path)
        pidfile = tmp_path / "ref-graceful.txt"
        spec = SpawnSpec(
            argv=child_argv(script, "exit_ignore", pidfile, grandchild=True), cwd=tmp_path, env=child_env()
        )
        handle = spawn_tree(spec)
        try:
            assert handle.wait(WAIT_LIMIT_SECONDS) == 0
            read_pids(pidfile)
            monkeypatch.setattr(job_object, "_send_ctrl_break", lambda pid: True)
            monkeypatch.setattr(job_object, "_drain_job", lambda job, seconds: True)
            result = job_object.stop_by_ref(handle.ref, grace_seconds=0.5)
            assert result.method.value == "GRACEFUL"
        finally:
            handle.close()

    def test_close_releases_resources_when_terminate_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """terminate失敗でもhandle / streamを解放し、KILL_ON_JOB_CLOSEの安全網でtreeが全滅する。"""
        script = write_child_script(tmp_path)
        pidfile = tmp_path / "close-fail.txt"
        out = tmp_path / "close-fail-out.txt"
        spec = SpawnSpec(
            argv=child_argv(script, "ignore", pidfile, grandchild=True),
            cwd=tmp_path,
            env=child_env(),
            stdout_path=out,
        )
        handle = spawn_tree(spec)
        child_pid, grandchild_pid = read_pids(pidfile)

        def _fail(job: int) -> None:
            raise StopError("terminate_job", "注入した失敗")

        monkeypatch.setattr(job_object, "_terminate_job", _fail)
        with pytest.raises(StopError):
            handle.close()
        # handleが閉じられ、KILL_ON_JOB_CLOSEでtreeはOS側の安全網により全滅する
        assert wait_until(lambda: tree_gone(child_pid, grandchild_pid))

        def _unlinkable() -> bool:
            # 親側streamが解放済みなら、killされた子のhandle解放（非同期）を待てば削除できる
            try:
                out.unlink()
            except PermissionError:
                return False
            return True

        assert wait_until(_unlinkable)
        handle.close()  # 再closeはno-op（解放済み、冪等）

    def test_stop_by_ref_forced_when_graceful_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """graceful要求が受理されない場合（console不在相当）、by-refでも強制停止でFORCEDになる。"""
        script = write_child_script(tmp_path)
        pidfile = tmp_path / "ref-forced.txt"
        spec = SpawnSpec(
            argv=child_argv(script, "exit_ignore", pidfile, grandchild=True), cwd=tmp_path, env=child_env()
        )
        handle = spawn_tree(spec)
        try:
            assert handle.wait(WAIT_LIMIT_SECONDS) == 0
            _child_pid, grandchild_pid = read_pids(pidfile)
            assert grandchild_pid is not None
            monkeypatch.setattr(job_object, "_send_ctrl_break", lambda pid: False)
            result = job_object.stop_by_ref(handle.ref, grace_seconds=0.3)
            assert result.method.value == "FORCED"
            assert result.graceful_requested is False
            assert wait_until(lambda: tree_gone(grandchild_pid))
        finally:
            handle.close()

    def test_stop_by_ref_force_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """強制停止後も残存する場合の構造化error（停止失敗の表現）。"""
        script = write_child_script(tmp_path)
        pidfile = tmp_path / "ref-forcefail.txt"
        spec = SpawnSpec(
            argv=child_argv(script, "exit_ignore", pidfile, grandchild=True), cwd=tmp_path, env=child_env()
        )
        handle = spawn_tree(spec)
        try:
            assert handle.wait(WAIT_LIMIT_SECONDS) == 0
            read_pids(pidfile)
            monkeypatch.setattr(job_object, "_send_ctrl_break", lambda pid: False)
            monkeypatch.setattr(job_object, "_terminate_job", lambda job: None)
            monkeypatch.setattr(job_object, "_drain_job", lambda job, seconds: False)
            with pytest.raises(StopError) as excinfo:
                job_object.stop_by_ref(handle.ref, grace_seconds=0.2)
            assert excinfo.value.stage == "force_stop"
        finally:
            handle.close()
