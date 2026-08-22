# SPDX-License-Identifier: Apache-2.0
"""POSIX backendのmode検証（AC-C06-05）。Windowsではmodule guardによりskipする。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

mode_posix = pytest.importorskip(
    "claude_code_codex_review_loop.identity.mode_posix", exc_type=ImportError
)

from claude_code_codex_review_loop.identity import FsPermissionError  # noqa: E402


class TestModeBits:
    def test_dir_is_0700_and_owned_by_current_user(self, tmp_path: Path) -> None:
        target = tmp_path / "artifacts"
        mode_posix.create_private_dir(target)
        info = os.stat(target)
        assert stat.S_IMODE(info.st_mode) == 0o700
        assert info.st_uid == os.getuid()

    def test_file_is_0600(self, tmp_path: Path) -> None:
        root = tmp_path / "artifacts"
        mode_posix.create_private_dir(root)
        target = root / "notes.txt"
        mode_posix.write_private_text(target, "x")
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600

    def test_restrictive_umask_still_yields_expected_mode(self, tmp_path: Path) -> None:
        """umaskで権限が削られても、作成者がアクセスできる期待値へ揃える。"""
        previous = os.umask(0o377)
        try:
            target = tmp_path / "artifacts"
            mode_posix.create_private_dir(target)
            assert stat.S_IMODE(os.stat(target).st_mode) == 0o700
            note = target / "notes.txt"
            mode_posix.write_private_text(note, "x")
            assert stat.S_IMODE(os.stat(note).st_mode) == 0o600
        finally:
            os.umask(previous)


class TestVerifyRejectsLoosePermissions:
    def test_group_or_other_access_is_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "artifacts"
        mode_posix.create_private_dir(target)
        os.chmod(target, 0o755)
        with pytest.raises(FsPermissionError) as excinfo:
            mode_posix.verify_private_dir(target)
        assert excinfo.value.stage == "verify"

    def test_foreign_owner_is_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """所有者が現userでないdirectoryは受理しない（uidをkernel levelで注入して検証）。"""
        target = tmp_path / "artifacts"
        mode_posix.create_private_dir(target)
        real_uid = os.getuid()
        monkeypatch.setattr(mode_posix.os, "getuid", lambda: real_uid + 1)
        with pytest.raises(FsPermissionError) as excinfo:
            mode_posix.verify_private_dir(target)
        assert excinfo.value.stage == "verify"

    def test_loose_file_mode_is_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "artifacts"
        mode_posix.create_private_dir(root)
        note = root / "notes.txt"
        mode_posix.write_private_text(note, "x")
        os.chmod(note, 0o644)
        with pytest.raises(FsPermissionError):
            mode_posix.verify_private_file(note)
