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
@pytest.mark.parametrize(
    "error",
    [PermissionError(1, "operation not permitted"), OSError(12, "cannot allocate memory")],
    ids=["access_denied", "resource_shortage"],
)
def test_ambiguous_kill_failure_is_treated_as_alive(
    monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    """不在を確定できないerrorはすべて生存扱い（迷ったら回収しない）。"""
    from claude_code_codex_review_loop.process import process_group

    def _deny(pid: int, signum: int) -> None:
        raise error

    monkeypatch.setattr(process_group.os, "kill", _deny)
    assert is_process_alive(os.getpid()) is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows backendの分岐")
@pytest.mark.parametrize(
    "error_name",
    ["_ERROR_ACCESS_DENIED", "_ERROR_NOT_ENOUGH_MEMORY"],
    ids=["access_denied", "resource_shortage"],
)
def test_ambiguous_open_failure_is_treated_as_alive(
    monkeypatch: pytest.MonkeyPatch, error_name: str
) -> None:
    """OpenProcessの失敗がprocessの不在を意味しない場合は生存扱いにする。

    権限不足（5）も資源不足（8）もprocessが在るまま起き得るため、これらで回収へ進むと
    生きているrunのlockを奪ってしまう。
    """
    from claude_code_codex_review_loop.process import job_object

    monkeypatch.setattr(job_object._kernel32, "OpenProcess", lambda access, inherit, pid: 0)
    monkeypatch.setattr(job_object, "_last_error", lambda: getattr(job_object, error_name))
    assert is_process_alive(os.getpid()) is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows backendの分岐")
def test_invalid_parameter_confirms_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    """存在しないpidに対するOpenProcessの`ERROR_INVALID_PARAMETER`だけを不在と解釈する。"""
    from claude_code_codex_review_loop.process import job_object

    monkeypatch.setattr(job_object._kernel32, "OpenProcess", lambda access, inherit, pid: 0)
    monkeypatch.setattr(job_object, "_last_error", lambda: job_object._ERROR_INVALID_PARAMETER)
    assert is_process_alive(os.getpid()) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows backendの分岐")
def test_wait_failure_is_treated_as_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """`WAIT_FAILED`や未知の戻り値を「不在」に倒さない（回収しない側へ倒す）。"""
    from claude_code_codex_review_loop.process import job_object

    monkeypatch.setattr(
        job_object._kernel32, "WaitForSingleObject", lambda handle, timeout: 0xFFFFFFFF
    )
    assert is_process_alive(os.getpid()) is True
