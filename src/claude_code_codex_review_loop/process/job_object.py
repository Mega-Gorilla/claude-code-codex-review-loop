# SPDX-License-Identifier: Apache-2.0
"""C-03のWindows backend（Job Object）。

参考実装はPOSIX専用のため、本moduleは新規実装である（ADR-0005）。子processを
CREATE_SUSPENDEDで起動し、孫を起動する前にnamed Job Objectへ所属させてから
resumeすることで、tree全体を1つのjobとして捕捉する（後付け所属のrace windowを
作らない）。breakawayは許可flagを設定しないことで拒否される。

- KILL_ON_JOB_CLOSE: jobへの最後のhandleが閉じるとtreeは自動で全滅する。起動元
  processの急死に対する安全網を兼ねる
- graceful停止はCTRL_BREAK_EVENT（best-effort）。送信成功は配送保証ではなく、
  応答の確認はActiveProcessesの観測でのみ行う
- 強制停止はTerminateJobObject。tree全滅の確認はQueryInformationJobObjectの
  ActiveProcesses == 0で行う
"""

from __future__ import annotations

import sys

if sys.platform != "win32":  # pragma: no cover - Windows専用module(POSIX側CIはreport対象からomitする)
    raise ImportError("job_objectはWindows専用moduleである")

import ctypes
import os
import signal
import subprocess
import time
import uuid
from ctypes import wintypes
from typing import IO

from .spawn import JobObjectRef, SpawnError, SpawnSpec, StopError, StopMethod, StopResult, TreeRef, _open_output

_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_QUERY = 0x0004
_JOB_OBJECT_TERMINATE = 0x0008
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# handleを待機可能にする（QUERY_LIMITED_INFORMATIONだけではWaitForSingleObjectがWAIT_FAILEDになる）
_SYNCHRONIZE = 0x00100000
_THREAD_SUSPEND_RESUME = 0x0002
_TH32CS_SNAPTHREAD = 0x00000004
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
_ERROR_FILE_NOT_FOUND = 2
_ERROR_ACCESS_DENIED = 5
_WAIT_TIMEOUT = 0x00000102
_ERROR_BAD_LENGTH = 24
_ERROR_ALREADY_EXISTS = 183
_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
_RESUME_FAILED = wintypes.DWORD(-1).value
_FORCED_EXIT_CODE = 1
_SNAPSHOT_RETRY_LIMIT = 5
_POLL_INTERVAL_SECONDS = 0.05
# 強制停止の完了確認に使う内部上限。呼び出し側のtimeout / grace（C-12で既定値を解決する）とは別物
_FORCE_CONFIRM_SECONDS = 5.0


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    # AffinityはULONG_PTR。pointer幅のためc_size_tで表現する
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobObjectBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", wintypes.LARGE_INTEGER),
        ("TotalKernelTime", wintypes.LARGE_INTEGER),
        ("ThisPeriodTotalUserTime", wintypes.LARGE_INTEGER),
        ("ThisPeriodTotalKernelTime", wintypes.LARGE_INTEGER),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# testが失敗経路を決定的に注入できるよう、last errorの取得は間接参照にする
_last_error = ctypes.get_last_error

# restype既定のc_intは32bitで、64bitのHANDLEを切り詰める。全APIへ型を明示する
_kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.OpenJobObjectW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
_kernel32.OpenJobObjectW.restype = wintypes.HANDLE
_kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.QueryInformationJobObject.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPVOID,
)
_kernel32.QueryInformationJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
_kernel32.TerminateJobObject.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))
_kernel32.Thread32First.restype = wintypes.BOOL
_kernel32.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))
_kernel32.Thread32Next.restype = wintypes.BOOL
_kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.OpenThread.restype = wintypes.HANDLE
_kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
_kernel32.ResumeThread.restype = wintypes.DWORD


def _close_handle(handle: int) -> None:
    _kernel32.CloseHandle(handle)


def is_process_alive(pid: int) -> bool:
    """pidのprocessが生存しているか（handleのsignal状態で判定する）。

    exit codeの`STILL_ACTIVE`（259）は正当な終了codeとしても現れ得るため、
    `WaitForSingleObject`のtimeoutで判定する（handleがsignal済み = 終了）。
    OpenProcessが権限不足で失敗した場合は「存在するが触れない」であり**生存扱い**に
    する（stale lockの回収可否では、迷ったら回収しない側が安全）。pidの再利用も
    生存と誤判定し得るが、同じく回収しない側へ倒れる（ADR-0011）。
    """
    handle = _kernel32.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return _last_error() == _ERROR_ACCESS_DENIED
    try:
        return bool(_kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT)
    finally:
        _close_handle(handle)


def _create_named_job(name: str) -> int:
    handle = _kernel32.CreateJobObjectW(None, name)
    if not handle:
        raise SpawnError("create_job", "CreateJobObjectWが失敗した", _last_error())
    if _last_error() == _ERROR_ALREADY_EXISTS:
        # uuid名の衝突は事実上起きないため、既存objectの再利用（squatting）として拒否する
        _close_handle(int(handle))
        raise SpawnError("create_job", "同名のJob Objectが既に存在する", _ERROR_ALREADY_EXISTS)
    return int(handle)


def _set_kill_on_close(job: int) -> None:
    info = _JobObjectExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    )
    if not ok:
        raise SpawnError("configure_job", "SetInformationJobObjectが失敗した", _last_error())


def _open_process_for_assign(pid: int) -> int:
    handle = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not handle:
        raise SpawnError("open_process", "OpenProcessが失敗した", _last_error())
    return int(handle)


def _assign_to_job(job: int, process_handle: int) -> None:
    if not _kernel32.AssignProcessToJobObject(job, process_handle):
        raise SpawnError("assign_job", "AssignProcessToJobObjectが失敗した", _last_error())


def _create_thread_snapshot() -> int | None:
    """thread snapshotを取得する。ERROR_BAD_LENGTH（文書化されたretry条件）はNoneを返す。"""
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snapshot or int(snapshot) == _INVALID_HANDLE_VALUE:
        error = _last_error()
        if error == _ERROR_BAD_LENGTH:
            return None
        raise SpawnError("resume", "CreateToolhelp32Snapshotが失敗した", error)
    return int(snapshot)


def _find_single_thread(pid: int) -> int:
    """suspended直後のprocess（thread数1）の主thread IDをToolhelp32 snapshotで特定する。"""
    for _ in range(_SNAPSHOT_RETRY_LIMIT):
        snapshot = _create_thread_snapshot()
        if snapshot is None:
            continue
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            has_entry = bool(_kernel32.Thread32First(snapshot, ctypes.byref(entry)))
            while has_entry:
                if int(entry.th32OwnerProcessID) == pid:
                    return int(entry.th32ThreadID)
                has_entry = bool(_kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
        finally:
            _close_handle(int(snapshot))
        raise SpawnError("resume", "起動したprocessのthreadが見つからない")
    raise SpawnError("resume", "snapshotのretry上限に到達した", _ERROR_BAD_LENGTH)


def _resume_main_thread(pid: int) -> None:
    thread_id = _find_single_thread(pid)
    thread = _kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, thread_id)
    if not thread:
        raise SpawnError("resume", "OpenThreadが失敗した", _last_error())
    try:
        if int(_kernel32.ResumeThread(thread)) == _RESUME_FAILED:
            raise SpawnError("resume", "ResumeThreadが失敗した", _last_error())
    finally:
        _close_handle(int(thread))


def _query_active_processes(job: int) -> int:
    info = _JobObjectBasicAccountingInformation()
    ok = _kernel32.QueryInformationJobObject(
        job, _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info), None
    )
    if not ok:
        raise StopError("query_job", "QueryInformationJobObjectが失敗した", _last_error())
    return int(info.ActiveProcesses)


def _terminate_job(job: int) -> None:
    if not _kernel32.TerminateJobObject(job, _FORCED_EXIT_CODE):
        raise StopError("terminate_job", "TerminateJobObjectが失敗した", _last_error())


def _open_job_by_name(name: str) -> int | None:
    handle = _kernel32.OpenJobObjectW(_JOB_OBJECT_QUERY | _JOB_OBJECT_TERMINATE, False, name)
    if not handle:
        error = _last_error()
        if error == _ERROR_FILE_NOT_FOUND:
            return None  # KILL_ON_JOB_CLOSEにより、name不在は「tree停止済み」を意味する
        raise StopError("open_job", "OpenJobObjectWが失敗した", error)
    return int(handle)


def _send_ctrl_break(pid: int) -> bool:
    """CTRL_BREAK_EVENTをprocess groupへ送る。成功はqueue受理であり、配送保証ではない。"""
    try:
        os.kill(pid, signal.CTRL_BREAK_EVENT)
    except OSError:
        return False  # console不在等。呼び出し側は即時force停止へ進む
    return True


class WindowsTreeHandle:
    """Job Objectで捕捉した1つのtreeへの操作。TreeHandle protocolを実装する。"""

    def __init__(self, popen: subprocess.Popen[bytes], job: int, ref: JobObjectRef, files: list[IO[bytes]]) -> None:
        self._popen = popen
        self._job: int | None = job  # Noneはclose済み（job handle解放済み）を意味する
        self._ref = ref
        self._files = files

    @property
    def pid(self) -> int:
        return self._popen.pid

    @property
    def ref(self) -> JobObjectRef:
        return self._ref

    def poll(self) -> int | None:
        return self._popen.poll()

    def wait(self, timeout_seconds: float) -> int | None:
        try:
            return self._popen.wait(timeout_seconds)
        except subprocess.TimeoutExpired:
            return None

    def alive_in_tree(self) -> bool:
        if self._job is None:
            return False
        return _query_active_processes(self._job) > 0

    def request_graceful_stop(self) -> bool:
        return _send_ctrl_break(self._popen.pid)

    def force_stop(self) -> None:
        if self._job is not None:
            _terminate_job(self._job)
        self._popen.poll()

    def close(self) -> None:
        job = self._job
        if job is None:
            return  # close済み（冪等）
        try:
            # 明示のterminateを安全網として実行する
            _terminate_job(job)
        finally:
            # terminateが失敗（StopError）してもhandleとstreamは必ず解放する。
            # handleのcloseでKILL_ON_JOB_CLOSEが作動するため、失敗時もtreeはOSの
            # 安全網で全滅し、_job = Noneが実態と一致する
            self._job = None
            _close_handle(job)
            self._popen.poll()
            for stream in self._files:
                stream.close()


def spawn_tree(spec: SpawnSpec) -> WindowsTreeHandle:
    """CREATE_SUSPENDEDで起動し、孫を起動する前にJob Objectへ所属させてからresumeする。"""
    job_name = f"Local\\cc-review-{uuid.uuid4().hex}"
    job = _create_named_job(job_name)
    files: list[IO[bytes]] = []
    popen: subprocess.Popen[bytes] | None = None
    try:
        _set_kill_on_close(job)
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
            creationflags=_CREATE_SUSPENDED | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        process_handle = _open_process_for_assign(popen.pid)
        try:
            _assign_to_job(job, process_handle)
        finally:
            _close_handle(process_handle)
        _resume_main_thread(popen.pid)
    except SpawnError:
        # suspendedのまま放置すると永久に残るため、必ず殺してから資源を解放する
        if popen is not None:
            popen.kill()
            popen.wait()
        _close_handle(job)
        for stream in files:
            stream.close()
        raise
    except OSError as exc:
        _close_handle(job)
        for stream in files:
            stream.close()
        raise SpawnError("popen", f"processを起動できない: {type(exc).__name__}", exc.errno) from exc
    return WindowsTreeHandle(popen, job, JobObjectRef(pid=popen.pid, job_name=job_name), files)


def _drain_job(job: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if _query_active_processes(job) == 0:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))


def stop_by_ref(ref: TreeRef, grace_seconds: float) -> StopResult:
    """元のhandleを持たない（別processを含む）呼び出し側からの再停止。冪等。"""
    if not isinstance(ref, JobObjectRef):
        raise StopError("ref_mismatch", f"{type(ref).__name__}は現在のOSでは扱えない")
    job = _open_job_by_name(ref.job_name)
    if job is None:
        return StopResult(StopMethod.ALREADY_EXITED, graceful_requested=False)
    try:
        if _query_active_processes(job) == 0:
            return StopResult(StopMethod.ALREADY_EXITED, graceful_requested=False)
        requested = _send_ctrl_break(ref.pid)
        if requested and _drain_job(job, grace_seconds):
            return StopResult(StopMethod.GRACEFUL, graceful_requested=True)
        _terminate_job(job)
        if not _drain_job(job, _FORCE_CONFIRM_SECONDS):
            raise StopError("force_stop", "強制停止後もtreeが残存している")
        return StopResult(StopMethod.FORCED, graceful_requested=requested)
    finally:
        _close_handle(job)
