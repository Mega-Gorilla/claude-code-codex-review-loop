# SPDX-License-Identifier: Apache-2.0
"""C-06のWindows backend（明示DACLによるfile権限。AC-C06-05）。

現userの単一許可ACE（`(OI)(CI)` / `FILE_ALL_ACCESS`）だけを持つsecurity descriptorを
組み、`CreateDirectoryW`へ渡して**作成とACL設定を原子的に**行う（mkdir後にicaclsで
付け替える方式が持つ、親DACL継承のままのrace windowを作らない）。検証はDACLの
読み戻しで行い、`icacls`の要約出力の解析には依存しない（出力はlocalizeされるため。
P-003の趣旨と同じく構造化値で判定する）。判断の正本はADR-0009。

- private directory内で作成したfileは`(OI)`継承で同じ単一ACEを持つ
- 検証は宣言したcanonical DACLとの完全一致を要求する: DACLが存在し、ACEが**ちょうど1つ**、
  typeがACCESS_ALLOWED、SIDが現user、maskが`FILE_ALL_ACCESS`、directoryは`(OI)(CI)`かつ
  継承遮断（`SE_DACL_PROTECTED`）、fileは親からの継承ACE。DACL不在（NULL DACL）は
  全員アクセス可を意味するためerrorとする
"""

from __future__ import annotations

import sys

if sys.platform != "win32":  # pragma: no cover - Windows専用module(POSIX側CIはreport対象からomitする)
    raise ImportError("acl_windowsはWindows専用moduleである")

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from .fs_permissions import FsPermissionError, write_all

_TOKEN_QUERY = 0x0008
_TOKEN_USER_CLASS = 1
_ACL_REVISION = 2
_ACL_SIZE_INFORMATION_CLASS = 2
_OBJECT_INHERIT_ACE = 0x1
_CONTAINER_INHERIT_ACE = 0x2
_FILE_ALL_ACCESS = 0x1F01FF
_SECURITY_DESCRIPTOR_REVISION = 1
_SE_DACL_PROTECTED = 0x1000
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_ACCESS_ALLOWED_ACE_TYPE = 0
_INHERITED_ACE = 0x10
# directoryのACEは自分自身へ適用され、かつ子（file / directory）へ継承される形だけを許す。
# INHERIT_ONLY（0x8）はACEを対象自身へ適用せず、NO_PROPAGATE_INHERIT（0x4）は孫への
# 継承を止めるため、いずれもprivate storageの保証を崩す。完全一致で拒否する
_DIRECTORY_ACE_FLAGS = _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE
# private directory内のfileは、親の(OI) ACEを継承した形（INHERITEDのみ）になる
_FILE_ACE_FLAGS = _INHERITED_ACE
_ERROR_SUCCESS = 0
# ACL header(8) + ACE header/mask(8) + SID(最大68)に余裕を持たせた固定長
_ACL_BYTES = 256
# ACCESS_ALLOWED_ACE内のSID開始offset（AceHeader 4 + Mask 4）
_ACE_SID_OFFSET = 8


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _SecurityDescriptor(ctypes.Structure):
    """absolute形式のSECURITY_DESCRIPTOR（pointer fieldにより整列も満たす）。"""

    _fields_ = [
        ("Revision", ctypes.c_ubyte),
        ("Sbz1", ctypes.c_ubyte),
        ("Control", wintypes.WORD),
        ("Owner", wintypes.LPVOID),
        ("Group", wintypes.LPVOID),
        ("Sacl", wintypes.LPVOID),
        ("Dacl", wintypes.LPVOID),
    ]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("User", _SidAndAttributes)]


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
    ]


class _AccessAllowedAce(ctypes.Structure):
    _fields_ = [("Header", _AceHeader), ("Mask", wintypes.DWORD)]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

# testが失敗経路を決定的に注入できるよう、last errorの取得は間接参照にする
_last_error = ctypes.get_last_error

_kernel32.GetCurrentProcess.argtypes = ()
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.CreateDirectoryW.argtypes = (wintypes.LPCWSTR, wintypes.LPVOID)
_kernel32.CreateDirectoryW.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
_kernel32.LocalFree.restype = wintypes.HLOCAL

_advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
_advapi32.OpenProcessToken.restype = wintypes.BOOL
_advapi32.GetTokenInformation.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
)
_advapi32.GetTokenInformation.restype = wintypes.BOOL
_advapi32.GetLengthSid.argtypes = (wintypes.LPVOID,)
_advapi32.GetLengthSid.restype = wintypes.DWORD
_advapi32.CopySid.argtypes = (wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)
_advapi32.CopySid.restype = wintypes.BOOL
_advapi32.EqualSid.argtypes = (wintypes.LPVOID, wintypes.LPVOID)
_advapi32.EqualSid.restype = wintypes.BOOL
_advapi32.InitializeAcl.argtypes = (wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD)
_advapi32.InitializeAcl.restype = wintypes.BOOL
_advapi32.AddAccessAllowedAceEx.argtypes = (
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
)
_advapi32.AddAccessAllowedAceEx.restype = wintypes.BOOL
_advapi32.InitializeSecurityDescriptor.argtypes = (wintypes.LPVOID, wintypes.DWORD)
_advapi32.InitializeSecurityDescriptor.restype = wintypes.BOOL
_advapi32.SetSecurityDescriptorDacl.argtypes = (
    wintypes.LPVOID,
    wintypes.BOOL,
    wintypes.LPVOID,
    wintypes.BOOL,
)
_advapi32.SetSecurityDescriptorDacl.restype = wintypes.BOOL
_advapi32.SetSecurityDescriptorControl.argtypes = (wintypes.LPVOID, wintypes.WORD, wintypes.WORD)
_advapi32.SetSecurityDescriptorControl.restype = wintypes.BOOL
_advapi32.GetNamedSecurityInfoW.argtypes = (
    wintypes.LPCWSTR,
    ctypes.c_int,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.LPVOID,
    ctypes.POINTER(wintypes.LPVOID),
    wintypes.LPVOID,
    ctypes.POINTER(wintypes.LPVOID),
)
_advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
_advapi32.GetAclInformation.argtypes = (wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.c_int)
_advapi32.GetAclInformation.restype = wintypes.BOOL
_advapi32.GetAce.argtypes = (wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID))
_advapi32.GetAce.restype = wintypes.BOOL
_advapi32.GetSecurityDescriptorControl.argtypes = (
    wintypes.LPVOID,
    ctypes.POINTER(wintypes.WORD),
    ctypes.POINTER(wintypes.DWORD),
)
_advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL


@dataclass(frozen=True)
class AceSummary:
    """1 ACEの構造化要約（localizeされた文字列出力に依存しない検証の材料）。"""

    ace_type: int
    ace_flags: int
    mask: int
    is_current_user: bool


@dataclass(frozen=True)
class DaclSummary:
    """対象のDACL全体の構造化要約。protectedは親からの継承が遮断されているか。"""

    protected: bool
    aces: tuple[AceSummary, ...]


def _current_user_sid() -> ctypes.Array[ctypes.c_uint32]:
    """現process tokenのuser SIDを、独立したDWORD整列bufferへ複製して返す。"""
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise FsPermissionError("token", "OpenProcessTokenが失敗した", _last_error())
    try:
        size = wintypes.DWORD(0)
        _advapi32.GetTokenInformation(token, _TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
        if size.value == 0:  # pragma: no cover - size取得の失敗は実質起きない
            raise FsPermissionError("token", "TOKEN_USERのsizeを取得できない", _last_error())
        buffer = (ctypes.c_uint64 * ((size.value + 7) // 8))()
        if not _advapi32.GetTokenInformation(token, _TOKEN_USER_CLASS, buffer, size.value, ctypes.byref(size)):
            raise FsPermissionError("token", "GetTokenInformationが失敗した", _last_error())
        user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        length = _advapi32.GetLengthSid(user.User.Sid)
        sid: ctypes.Array[ctypes.c_uint32] = (ctypes.c_uint32 * ((length + 3) // 4))()
        if not _advapi32.CopySid(length, sid, user.User.Sid):
            raise FsPermissionError("token", "CopySidが失敗した", _last_error())
        return sid
    finally:
        _kernel32.CloseHandle(token)


def _owner_only_security_attributes() -> tuple[_SecurityAttributes, tuple[object, ...]]:
    """現userの単一ACEだけを持つSECURITY_ATTRIBUTESと、その生存を保つbufferを返す。"""
    sid = _current_user_sid()
    acl = (ctypes.c_uint32 * (_ACL_BYTES // 4))()
    if not _advapi32.InitializeAcl(acl, _ACL_BYTES, _ACL_REVISION):
        raise FsPermissionError("acl", "InitializeAclが失敗した", _last_error())
    inherit = _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE
    if not _advapi32.AddAccessAllowedAceEx(acl, _ACL_REVISION, inherit, _FILE_ALL_ACCESS, sid):
        raise FsPermissionError("acl", "AddAccessAllowedAceExが失敗した", _last_error())
    descriptor = _SecurityDescriptor()
    if not _advapi32.InitializeSecurityDescriptor(ctypes.byref(descriptor), _SECURITY_DESCRIPTOR_REVISION):
        raise FsPermissionError("acl", "InitializeSecurityDescriptorが失敗した", _last_error())
    if not _advapi32.SetSecurityDescriptorDacl(ctypes.byref(descriptor), True, acl, False):
        raise FsPermissionError("acl", "SetSecurityDescriptorDaclが失敗した", _last_error())
    # 親からの継承ACEを混入させない（作成時点で遮断する）
    if not _advapi32.SetSecurityDescriptorControl(
        ctypes.byref(descriptor), _SE_DACL_PROTECTED, _SE_DACL_PROTECTED
    ):
        raise FsPermissionError("acl", "SetSecurityDescriptorControlが失敗した", _last_error())
    attributes = _SecurityAttributes()
    attributes.nLength = ctypes.sizeof(_SecurityAttributes)
    attributes.lpSecurityDescriptor = ctypes.cast(ctypes.byref(descriptor), wintypes.LPVOID)
    attributes.bInheritHandle = False
    return attributes, (sid, acl, descriptor)


def read_dacl(path: Path) -> DaclSummary:
    """対象のDACLとcontrol flagを構造化して読み出す（DACL不在はerror）。"""
    descriptor = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    status = _advapi32.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if status != _ERROR_SUCCESS:
        raise FsPermissionError("verify", f"GetNamedSecurityInfoWが失敗した: {path}", status)
    try:
        if not dacl:
            raise FsPermissionError("verify", f"DACLが存在せず全員がアクセスできる: {path}")
        info = _AclSizeInformation()
        if not _advapi32.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), _ACL_SIZE_INFORMATION_CLASS):
            raise FsPermissionError("verify", "GetAclInformationが失敗した", _last_error())
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not _advapi32.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise FsPermissionError("verify", "GetSecurityDescriptorControlが失敗した", _last_error())
        current = _current_user_sid()
        summaries: list[AceSummary] = []
        for index in range(info.AceCount):
            entry = wintypes.LPVOID()
            if not _advapi32.GetAce(dacl, index, ctypes.byref(entry)):
                raise FsPermissionError("verify", "GetAceが失敗した", _last_error())
            ace = ctypes.cast(entry, ctypes.POINTER(_AccessAllowedAce)).contents
            address = ctypes.cast(entry, ctypes.c_void_p).value
            assert address is not None  # GetAce成功時のentryは非NULL
            sid_pointer = wintypes.LPVOID(address + _ACE_SID_OFFSET)
            summaries.append(
                AceSummary(
                    ace_type=ace.Header.AceType,
                    ace_flags=ace.Header.AceFlags,
                    mask=ace.Mask,
                    is_current_user=bool(_advapi32.EqualSid(sid_pointer, current)),
                )
            )
        return DaclSummary(protected=bool(control.value & _SE_DACL_PROTECTED), aces=tuple(summaries))
    finally:
        _kernel32.LocalFree(descriptor)


def _verify(path: Path, expect_dir: bool) -> None:
    """宣言したcanonical DACLとの完全一致を検証する（生成結果を信用せず読み戻す）。"""
    if path.is_dir() is not expect_dir:
        raise FsPermissionError("verify", f"対象の種別が期待と異なる: {path}")
    summary = read_dacl(path)
    if len(summary.aces) != 1:
        raise FsPermissionError("verify", f"DACLが現userの単一ACEでない（{len(summary.aces)}件）: {path}")
    ace = summary.aces[0]
    if ace.ace_type != _ACCESS_ALLOWED_ACE_TYPE or not ace.is_current_user:
        raise FsPermissionError("verify", f"作成者以外のACEを含む: {path}")
    if ace.mask != _FILE_ALL_ACCESS:
        raise FsPermissionError("verify", f"ACEのaccess maskが期待と異なる: {path}")
    if expect_dir:
        if ace.ace_flags != _DIRECTORY_ACE_FLAGS:
            raise FsPermissionError("verify", f"ACEの継承flagが期待と異なる: {path}")
        if not summary.protected:
            raise FsPermissionError("verify", f"DACLが親からの継承を遮断していない: {path}")
    elif ace.ace_flags != _FILE_ACE_FLAGS:
        # private directory内で作成したfileは親の(OI) ACEを継承する。それ以外のflag構成は
        # 権限の出所が想定と異なる（継承元が別、または対象自身へ適用されない）
        raise FsPermissionError("verify", f"ACEの継承flagが期待と異なる: {path}")


def create_private_dir(path: Path) -> None:
    """現userの単一ACEを持つdirectoryを排他的・原子的に作成する（既存pathはerror）。"""
    attributes, _keep_alive = _owner_only_security_attributes()
    if not _kernel32.CreateDirectoryW(str(path), ctypes.byref(attributes)):
        raise FsPermissionError("create_dir", f"directoryを作成できない: {path}", _last_error())
    _verify(path, expect_dir=True)


def write_private_text(path: Path, text: str) -> None:
    """private directory内へfileを排他的に作成する（親の継承ACEで作成者限定になる）。"""
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_BINARY)
    except OSError as error:
        raise FsPermissionError("create_file", f"fileを作成できない: {path}", error.errno) from error
    try:
        write_all(descriptor, text.encode("utf-8"), path)
        # 作成も置換も耐久性を持たせる（checkpointのatomic replaceの前提）
        os.fsync(descriptor)
    except BaseException:
        # 書き切れなかったfileを残さない（短い内容がreplaceされるのを防ぐ）
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    _verify(path, expect_dir=False)


def sync_directory(path: Path) -> None:
    """Windowsではdirectory handleへのfsyncができないためno-op（契約を揃えるための空実装）。

    `os.replace`自体がNTFS上でatomicであり、file側は`write_private_text`のfsyncで
    確定済みである。POSIX backendとinterfaceを合わせるためだけに存在する。
    """


def verify_private_dir(path: Path) -> None:
    """既存directoryのDACLが現userの単一許可ACEのみであることを検証する。"""
    _verify(path, expect_dir=True)


def verify_private_file(path: Path) -> None:
    """既存fileのDACLが現userの許可ACEのみであることを検証する。"""
    _verify(path, expect_dir=False)
