# SPDX-License-Identifier: Apache-2.0
"""Windows backendのDACL検証（AC-C06-05）。POSIXではmodule guardによりskipする。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

acl_windows = pytest.importorskip(
    "claude_code_codex_review_loop.identity.acl_windows", exc_type=ImportError
)

from claude_code_codex_review_loop.identity import FsPermissionError  # noqa: E402

_ACCESS_ALLOWED_ACE_TYPE = 0
_OBJECT_INHERIT_ACE = 0x1
_CONTAINER_INHERIT_ACE = 0x2
_INHERITED_ACE = 0x10
_FILE_ALL_ACCESS = 0x1F01FF
# 「Everyone」well-known SID（表示名はlocaleで変わるためSIDで指定する）
_EVERYONE_SID = "*S-1-1-0"


def _grant_everyone(path: Path, permission: str) -> None:
    """test側でのみicaclsを使い、第三者ACEを付加した状態を作る（argv list。P-014）。"""
    completed = subprocess.run(
        ["icacls", str(path), permission, f"{_EVERYONE_SID}:(R)"],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


class TestOwnerOnlyDacl:
    def test_dir_has_single_inheritable_ace_for_current_user(self, tmp_path: Path) -> None:
        target = tmp_path / "artifacts"
        acl_windows.create_private_dir(target)
        aces = acl_windows.read_dacl(target)
        assert len(aces) == 1
        ace = aces[0]
        assert ace.ace_type == _ACCESS_ALLOWED_ACE_TYPE
        assert ace.is_current_user
        assert ace.mask == _FILE_ALL_ACCESS
        assert ace.ace_flags & (_OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE) == (
            _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE
        )

    def test_file_inherits_the_single_ace(self, tmp_path: Path) -> None:
        root = tmp_path / "artifacts"
        acl_windows.create_private_dir(root)
        note = root / "notes.txt"
        acl_windows.write_private_text(note, "x")
        aces = acl_windows.read_dacl(note)
        assert len(aces) == 1
        assert aces[0].is_current_user and aces[0].ace_flags & _INHERITED_ACE

    def test_nested_dir_stays_owner_only(self, tmp_path: Path) -> None:
        root = tmp_path / "artifacts"
        acl_windows.create_private_dir(root)
        nested = root / "runs"
        acl_windows.create_private_dir(nested)
        acl_windows.verify_private_dir(nested)


class TestVerifyRejectsForeignAce:
    def test_extra_allow_ace_is_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "artifacts"
        acl_windows.create_private_dir(target)
        _grant_everyone(target, "/grant")
        assert len(acl_windows.read_dacl(target)) == 2
        with pytest.raises(FsPermissionError) as excinfo:
            acl_windows.verify_private_dir(target)
        assert excinfo.value.stage == "verify"

    def test_deny_ace_is_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "artifacts"
        acl_windows.create_private_dir(target)
        _grant_everyone(target, "/deny")
        with pytest.raises(FsPermissionError):
            acl_windows.verify_private_dir(target)

    def test_missing_path_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(FsPermissionError) as excinfo:
            acl_windows.read_dacl(tmp_path / "missing")
        assert excinfo.value.stage == "verify" and excinfo.value.os_error is not None


class _AlwaysFails:
    """Win32 APIの失敗をkernel levelで注入するstub（実際のraise行を通す）。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, *args: object) -> int:
        return 0


@pytest.mark.parametrize(
    ("api", "stage"),
    [
        ("OpenProcessToken", "token"),
        ("CopySid", "token"),
        ("InitializeAcl", "acl"),
        ("AddAccessAllowedAceEx", "acl"),
        ("InitializeSecurityDescriptor", "acl"),
        ("SetSecurityDescriptorDacl", "acl"),
        ("SetSecurityDescriptorControl", "acl"),
    ],
)
def test_creation_api_failures_are_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: str, stage: str
) -> None:
    """作成経路の各Win32 API失敗が、構造化errorとして表面化する（silentに続行しない）。"""
    monkeypatch.setattr(acl_windows._advapi32, api, _AlwaysFails(api), raising=True)
    with pytest.raises(FsPermissionError) as excinfo:
        acl_windows.create_private_dir(tmp_path / "artifacts")
    assert excinfo.value.stage == stage


@pytest.mark.parametrize("api", ["GetAclInformation", "GetAce"])
def test_read_dacl_api_failures_are_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: str
) -> None:
    target = tmp_path / "artifacts"
    acl_windows.create_private_dir(target)
    monkeypatch.setattr(acl_windows._advapi32, api, _AlwaysFails(api), raising=True)
    with pytest.raises(FsPermissionError) as excinfo:
        acl_windows.read_dacl(target)
    assert excinfo.value.stage == "verify"


def test_null_dacl_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """NULL DACL（全員アクセス可）はerrorにする。"""
    target = tmp_path / "artifacts"
    acl_windows.create_private_dir(target)

    def _null_dacl(*args: object) -> int:
        return 0  # ERROR_SUCCESSのまま、dacl出力を未設定（NULL）にする

    monkeypatch.setattr(acl_windows._advapi32, "GetNamedSecurityInfoW", _null_dacl, raising=True)
    with pytest.raises(FsPermissionError) as excinfo:
        acl_windows.read_dacl(target)
    assert "DACL" in excinfo.value.detail


def test_empty_dacl_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ACEが1つも無いDACL（作成者すらアクセスできない）もerrorにする。"""
    target = tmp_path / "artifacts"
    acl_windows.create_private_dir(target)
    monkeypatch.setattr(acl_windows, "read_dacl", lambda path: (), raising=True)
    with pytest.raises(FsPermissionError) as excinfo:
        acl_windows.verify_private_dir(target)
    assert excinfo.value.stage == "verify"


def test_token_information_failure_is_structured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """size照会は成功し本取得で失敗するcase（bufferを確保した後の失敗経路）。"""
    real = acl_windows._advapi32.GetTokenInformation

    class _FailsAfterSizeQuery:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, *args: object) -> int:
            self.calls += 1
            if self.calls == 1:
                return real(*args)
            return 0

    monkeypatch.setattr(acl_windows._advapi32, "GetTokenInformation", _FailsAfterSizeQuery(), raising=True)
    with pytest.raises(FsPermissionError) as excinfo:
        acl_windows.create_private_dir(tmp_path / "artifacts")
    assert excinfo.value.stage == "token"
