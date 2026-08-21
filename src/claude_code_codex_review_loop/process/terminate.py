# SPDX-License-Identifier: Apache-2.0
"""C-03の停止・timeout logic（OS非依存のfacade）。

TreeHandleの操作だけでgraceful -> grace period -> forceの段階停止を実装する。
二段階のCtrl+C（1回目 = stop_treeによるgraceful cancellation、2回目 = force_stopの
即時実行）へのsignal wiringはC-08の責務であり、本moduleは停止primitivesだけを提供する。
停止は冪等で、対象treeが存在しなければ即時にALREADY_EXITEDを返す（C-01のcancellation
契約: 実行中processが無ければ完了は即時返る）。
"""

from __future__ import annotations

import time

from .spawn import (
    Completed,
    SpawnSpec,
    StopError,
    StopMethod,
    StopResult,
    TimedOut,
    TreeHandle,
    TreeRef,
    _backend,
    spawn_tree,
)

_POLL_INTERVAL_SECONDS = 0.05
# 強制停止の完了確認に使う内部上限。呼び出し側のtimeout / grace（C-12で既定値を解決する）とは別物
_FORCE_CONFIRM_SECONDS = 5.0


def _tree_exited_within(handle: TreeHandle, seconds: float) -> bool:
    """treeの全滅をdeadline付きで観測する。sleepは標準libraryで行う。"""
    deadline = time.monotonic() + seconds
    while True:
        handle.poll()
        if not handle.alive_in_tree():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))


def stop_tree(handle: TreeHandle, grace_seconds: float) -> StopResult:
    """graceful要求 -> grace待機 -> 強制停止の段階でtree全体を停止する。冪等。

    - grace待機中のKeyboardInterrupt（2回目のCtrl+C）は本関数を中断して伝播する。
      その後のforce_stop呼び出し（C-08が行う）は安全である（冪等）
    - 強制停止後もtreeが残存する場合はStopErrorを送出する。停止commandの再発行
      （C-01契約）で本関数を再度呼んでよい
    """
    handle.poll()
    if not handle.alive_in_tree():
        return StopResult(StopMethod.ALREADY_EXITED, graceful_requested=False)
    requested = handle.request_graceful_stop()
    if not requested and not handle.alive_in_tree():
        return StopResult(StopMethod.ALREADY_EXITED, graceful_requested=False)
    if requested and _tree_exited_within(handle, grace_seconds):
        return StopResult(StopMethod.GRACEFUL, graceful_requested=True)
    handle.force_stop()
    if not _tree_exited_within(handle, _FORCE_CONFIRM_SECONDS):
        raise StopError("force_stop", "強制停止後もtreeが残存している")
    return StopResult(StopMethod.FORCED, graceful_requested=requested)


def run_tree(spec: SpawnSpec, timeout_seconds: float, grace_seconds: float) -> Completed | TimedOut:
    """spawnして完了またはtimeoutまで待つ。どちらの経路でもtreeを残さない（AC-C03-01）。

    直接childが正常終了しても孫が残っている場合があるため、返る前に必ずclose()
    （安全網の強制停止と資源解放）を実行する。
    """
    handle = spawn_tree(spec)
    try:
        exit_code = handle.wait(timeout_seconds)
        if exit_code is None:
            return TimedOut(stop_result=stop_tree(handle, grace_seconds))
        return Completed(exit_code=exit_code)
    finally:
        handle.close()


def stop_tree_by_ref(ref: TreeRef, grace_seconds: float) -> StopResult:
    """元のTreeHandleを持たない（別processを含む）呼び出し側からの再停止。冪等。

    現在のOSと一致しないref種別はStopError("ref_mismatch")で拒否する。
    """
    return _backend.stop_by_ref(ref, grace_seconds)
