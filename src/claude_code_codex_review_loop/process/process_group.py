# SPDX-License-Identifier: Apache-2.0
"""C-03のPOSIX backend（process group）。

子processを新規session（start_new_session=True）で起動し、child = session /
process group leader（pgid == pid）としてtree全体をprocess groupで捕捉する。
停止はSIGTERM -> grace period -> SIGKILLの段階で行う（ADR-0005）。

既知limitation（OS非対称。ADR-0005に記載）: 孫processが自らsetsid()等で別の
process groupへ移った場合、killpgの射程外になる。stdlibのみでは原理的に防げない
（WindowsはJob Objectのbreakaway拒否で封じられる）。
"""

from __future__ import annotations

import sys

if sys.platform == "win32":  # pragma: no cover - POSIX専用module(Windows側CIはreport対象からomitする)
    raise ImportError("process_groupはPOSIX専用moduleである")

import os
import signal
import subprocess
import time
from typing import IO

from .spawn import ProcessGroupRef, SpawnError, SpawnSpec, StopError, StopMethod, StopResult, TreeRef, _open_output

_POLL_INTERVAL_SECONDS = 0.05
# 強制停止の完了確認に使う内部上限。呼び出し側のtimeout / grace（C-12で既定値を解決する）とは別物
_FORCE_CONFIRM_SECONDS = 5.0


def _signal_group(pgid: int, signum: int) -> bool:
    """groupへsignalを送る。groupが既に消滅していればFalse（冪等）。"""
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        return False
    except OSError as exc:
        raise StopError("signal_group", f"killpgが失敗した: {type(exc).__name__}", exc.errno) from exc
    return True


def _group_exists(pgid: int) -> bool:
    return _signal_group(pgid, 0)


class PosixTreeHandle:
    """process groupで捕捉した1つのtreeへの操作。TreeHandle protocolを実装する。"""

    def __init__(self, popen: subprocess.Popen[bytes], ref: ProcessGroupRef, files: list[IO[bytes]]) -> None:
        self._popen = popen
        self._ref = ref
        self._files = files
        self._closed = False

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def ref(self) -> ProcessGroupRef:
        return self._ref

    def poll(self) -> int | None:
        return self._popen.poll()

    def wait(self, timeout_seconds: float) -> int | None:
        try:
            return self._popen.wait(timeout_seconds)
        except subprocess.TimeoutExpired:
            return None

    def alive_in_tree(self) -> bool:
        # 直接childがzombieの間はgroupが存在し続けるため、先にreapしてから観測する
        self._popen.poll()
        return _group_exists(self._ref.pgid)

    def request_graceful_stop(self) -> bool:
        return _signal_group(self._ref.pgid, signal.SIGTERM)

    def force_stop(self) -> None:
        _signal_group(self._ref.pgid, signal.SIGKILL)
        self._popen.poll()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _signal_group(self._ref.pgid, signal.SIGKILL)
        # SIGKILL後の直接childを確実にreapする（zombieを残さない）
        try:
            self._popen.wait(_FORCE_CONFIRM_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL後にwaitが失敗する状況は再現不能
            pass
        for stream in self._files:
            stream.close()


def spawn_tree(spec: SpawnSpec) -> PosixTreeHandle:
    """新規session / process groupで起動する。pgidはsetsidの定義によりchildのpidと等しい。"""
    files: list[IO[bytes]] = []
    try:
        stdout = _open_output(spec.stdout_path)
        if stdout is not None:
            files.append(stdout)
        stderr = _open_output(spec.stderr_path)
        if stderr is not None:
            files.append(stderr)
        popen = subprocess.Popen(
            list(spec.argv),
            cwd=str(spec.cwd),
            env=dict(spec.env),
            stdin=subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.DEVNULL,
            stderr=stderr if stderr is not None else subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        for stream in files:
            stream.close()
        raise SpawnError("popen", f"processを起動できない: {type(exc).__name__}", exc.errno) from exc
    return PosixTreeHandle(popen, ProcessGroupRef(pid=popen.pid, pgid=popen.pid), files)


def _drain_group(pgid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if not _group_exists(pgid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))


def stop_by_ref(ref: TreeRef, grace_seconds: float) -> StopResult:
    """元のhandleを持たない（別processを含む）呼び出し側からの再停止。冪等。

    注意: 呼び出しprocess自身が直接childの親である場合はhandle経由で停止する
    （未reapのzombieがgroupを生存として見せ続けるため）。leader pidの再利用は
    getpgid照合で緩和するが、group全滅後のpgid再利用は検出できない（既知limitation）。
    """
    if not isinstance(ref, ProcessGroupRef):
        raise StopError("ref_mismatch", f"{type(ref).__name__}は現在のOSでは扱えない")
    try:
        if os.getpgid(ref.pid) != ref.pgid:
            return StopResult(StopMethod.ALREADY_EXITED, graceful_requested=False)  # pid再利用を検知
    except ProcessLookupError:
        # leaderが消滅していてもgroupは孫だけで存続し得るため、groupの存在で判定を続ける
        pass
    if not _group_exists(ref.pgid):
        return StopResult(StopMethod.ALREADY_EXITED, graceful_requested=False)
    requested = _signal_group(ref.pgid, signal.SIGTERM)
    if requested and _drain_group(ref.pgid, grace_seconds):
        return StopResult(StopMethod.GRACEFUL, graceful_requested=True)
    _signal_group(ref.pgid, signal.SIGKILL)
    if not _drain_group(ref.pgid, _FORCE_CONFIRM_SECONDS):
        raise StopError("force_stop", "強制停止後もtreeが残存している")
    return StopResult(StopMethod.FORCED, graceful_requested=requested)
