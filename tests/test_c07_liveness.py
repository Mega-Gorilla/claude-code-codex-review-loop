# SPDX-License-Identifier: Apache-2.0
"""pid生存判定の受入test（stale lock回収の前提。ADR-0011）。

判定は非対称で、「不在」と確定できた場合だけFalseになる。曖昧な状況（権限不足・
pid再利用）はTrue（生存扱い）へ倒れ、結果として**回収しない**側へ働く。
OS別backendの実装は自OSのCIで検証する（対向OSはcoverage reportからomitする）。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from claude_code_codex_review_loop.process import is_process_alive


def test_current_process_is_alive() -> None:
    assert is_process_alive(os.getpid()) is True


def test_exited_process_is_not_alive() -> None:
    """終了を確認したchildは不在と判定される（回収の前提条件）。"""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    assert is_process_alive(process.pid) is False


def test_unlikely_pid_is_not_alive() -> None:
    assert is_process_alive(2**31 - 1) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX backendの分岐")
def test_permission_error_is_treated_as_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """存在するが触れないprocessは生存扱い（迷ったら回収しない）。"""
    from claude_code_codex_review_loop.process import process_group

    def _deny(pid: int, signum: int) -> None:
        raise PermissionError(1, "operation not permitted")

    monkeypatch.setattr(process_group.os, "kill", _deny)
    assert is_process_alive(os.getpid()) is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows backendの分岐")
def test_access_denied_is_treated_as_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenProcessが権限不足で失敗した場合は生存扱い（迷ったら回収しない）。"""
    from claude_code_codex_review_loop.process import job_object

    monkeypatch.setattr(job_object._kernel32, "OpenProcess", lambda access, inherit, pid: 0)
    monkeypatch.setattr(job_object, "_last_error", lambda: job_object._ERROR_ACCESS_DENIED)
    assert is_process_alive(os.getpid()) is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows backendの分岐")
def test_other_open_failure_is_not_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    from claude_code_codex_review_loop.process import job_object

    monkeypatch.setattr(job_object._kernel32, "OpenProcess", lambda access, inherit, pid: 0)
    monkeypatch.setattr(job_object, "_last_error", lambda: 87)
    assert is_process_alive(os.getpid()) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows backendの分岐")
def test_wait_failure_is_treated_as_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """`WAIT_FAILED`や未知の戻り値を「不在」に倒さない（回収しない側へ倒す）。"""
    from claude_code_codex_review_loop.process import job_object

    monkeypatch.setattr(
        job_object._kernel32, "WaitForSingleObject", lambda handle, timeout: 0xFFFFFFFF
    )
    assert is_process_alive(os.getpid()) is True
