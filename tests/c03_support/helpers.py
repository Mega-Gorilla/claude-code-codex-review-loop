# SPDX-License-Identifier: Apache-2.0
"""C-03受入testの共有helper。

- 子/孫processのfixture script生成（tmp_pathへ書き、tracked fileを増やさない）
- 子scriptは短周期sleep loopで書く（Windowsは長い単発sleep中にSIGBREAK handlerを
  実行しないため、これを外すとgraceful testが偽陰性になる）
- 全scriptに自己終了の安全網（lifetime上限）を持たせ、test失敗時もprocessを残さない
- 生存確認はstdlibのみ（POSIXはos.kill(pid, 0)、Windowsはctypes）
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# 子scriptの自己終了上限（安全網）。testのassert対象ではない
CHILD_LIFETIME_SECONDS = 120.0
# testでの観測待ちの上限。pytest-timeoutが無いため、全waitはこの水準で必ず打ち切る
WAIT_LIMIT_SECONDS = 30.0

# 子process script。argv: mode pidfile spawn_grandchild lifetime
# mode: cooperative（graceful要求で終了） / ignore（graceful要求を無視） /
#       exit_after_spawn（孫を残して即終了） / exit_ignore（graceful無視の孫を残して即終了） /
#       quick_exit（孫なしで即終了）
_CHILD_SCRIPT = """\
import os
import signal
import subprocess
import sys
import time

mode = sys.argv[1]
pidfile = sys.argv[2]
spawn_grandchild = sys.argv[3] == "1"
lifetime = float(sys.argv[4])

stop_signal = signal.SIGBREAK if hasattr(signal, "SIGBREAK") else signal.SIGTERM
state = {"stop": False}


def _handler(signum, frame):
    state["stop"] = True


if mode == "ignore":
    signal.signal(stop_signal, signal.SIG_IGN)
else:
    signal.signal(stop_signal, _handler)

grandchild_pid = ""
if spawn_grandchild:
    ignore_flag = "1" if mode in ("ignore", "exit_ignore") else "0"
    grandchild = subprocess.Popen(
        [sys.executable, "-c", GRANDCHILD_CODE, ignore_flag, str(lifetime)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    grandchild_pid = str(grandchild.pid)

with open(pidfile, "w", encoding="utf-8") as handle:
    handle.write(f"{os.getpid()},{grandchild_pid}")

if mode in ("exit_after_spawn", "exit_ignore", "quick_exit"):
    sys.exit(0)

deadline = time.monotonic() + lifetime
while time.monotonic() < deadline:
    if state["stop"]:
        sys.exit(0)
    time.sleep(0.05)
sys.exit(9)
"""

# 孫process code（-cで渡す）。argv: ignore_flag lifetime
_GRANDCHILD_CODE = """\
import signal
import sys
import time

if sys.argv[1] == "1":
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

deadline = time.monotonic() + float(sys.argv[2])
while time.monotonic() < deadline:
    time.sleep(0.05)
"""


def write_child_script(directory: Path) -> Path:
    """孫を起動できる子scriptをtmp配下へ生成し、pathを返す。"""
    script = directory / "c03_child.py"
    body = _CHILD_SCRIPT.replace("GRANDCHILD_CODE", repr(_GRANDCHILD_CODE))
    script.write_text(body, encoding="utf-8")
    return script


def child_argv(script: Path, mode: str, pidfile: Path, *, grandchild: bool) -> tuple[str, ...]:
    return (
        sys.executable,
        str(script),
        mode,
        str(pidfile),
        "1" if grandchild else "0",
        str(CHILD_LIFETIME_SECONDS),
    )


def child_env() -> dict[str, str]:
    """explicit env契約のもとで子のPythonが起動できる最小環境変数。"""
    env: dict[str, str] = {}
    for name in ("SYSTEMROOT", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def wait_until(condition, timeout_seconds: float = WAIT_LIMIT_SECONDS, interval: float = 0.05) -> bool:
    """conditionが真になるまでdeadline付きで待つ。"""
    deadline = time.monotonic() + timeout_seconds
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def read_pids(pidfile: Path) -> tuple[int, int | None]:
    """子scriptが書いたpidfileから(child_pid, grandchild_pid)を読む。"""
    assert wait_until(pidfile.exists), "pidfileが作成されない"
    text = pidfile.read_text(encoding="utf-8")
    child_text, _, grandchild_text = text.partition(",")
    return int(child_text), int(grandchild_text) if grandchild_text else None


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _k32.CloseHandle.restype = wintypes.BOOL

    def process_alive(pid: int) -> bool:
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return int(code.value) == _STILL_ACTIVE
        finally:
            _k32.CloseHandle(handle)

else:

    def process_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def tree_gone(*pids: int | None) -> bool:
    """指定した全pidが消滅しているか（Noneは無視）。"""
    return all(not process_alive(pid) for pid in pids if pid is not None)
