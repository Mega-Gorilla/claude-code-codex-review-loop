# SPDX-License-Identifier: Apache-2.0
"""OS別file権限の共通契約の受入test（AC-C06-05。両OSで実行する）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_code_codex_review_loop.identity import (
    FsPermissionError,
    create_private_dir,
    verify_private_dir,
    verify_private_file,
    write_private_text,
)


class TestExclusiveCreation:
    """事前に存在するpathの権限を信用しない（fail closed）。"""

    def test_dir_creation_is_exclusive(self, tmp_path: Path) -> None:
        target = tmp_path / "artifacts"
        create_private_dir(target)
        with pytest.raises(FsPermissionError) as excinfo:
            create_private_dir(target)
        assert excinfo.value.stage == "create_dir"

    def test_dir_creation_requires_existing_parent(self, tmp_path: Path) -> None:
        with pytest.raises(FsPermissionError) as excinfo:
            create_private_dir(tmp_path / "missing" / "artifacts")
        assert excinfo.value.stage == "create_dir"

    def test_file_creation_is_exclusive(self, tmp_path: Path) -> None:
        root = tmp_path / "artifacts"
        create_private_dir(root)
        target = root / "notes.txt"
        write_private_text(target, "本文\n")
        assert target.read_bytes() == "本文\n".encode()
        with pytest.raises(FsPermissionError) as excinfo:
            write_private_text(target, "上書き")
        assert excinfo.value.stage == "create_file"

    def test_file_creation_requires_existing_parent(self, tmp_path: Path) -> None:
        with pytest.raises(FsPermissionError) as excinfo:
            write_private_text(tmp_path / "missing" / "notes.txt", "x")
        assert excinfo.value.stage == "create_file"


class TestVerify:
    def test_verify_accepts_created_objects(self, tmp_path: Path) -> None:
        root = tmp_path / "artifacts"
        create_private_dir(root)
        target = root / "notes.txt"
        write_private_text(target, "x")
        verify_private_dir(root)
        verify_private_file(target)

    def test_verify_rejects_wrong_kind(self, tmp_path: Path) -> None:
        root = tmp_path / "artifacts"
        create_private_dir(root)
        target = root / "notes.txt"
        write_private_text(target, "x")
        with pytest.raises(FsPermissionError) as excinfo:
            verify_private_dir(target)
        assert excinfo.value.stage == "verify"
        with pytest.raises(FsPermissionError):
            verify_private_file(root)

    def test_verify_reports_missing_path(self, tmp_path: Path) -> None:
        with pytest.raises(FsPermissionError) as excinfo:
            verify_private_dir(tmp_path / "missing")
        assert excinfo.value.stage == "verify"


class TestSharedFileEntity:
    """hard linkはpath正規化で検出できないため、link数で実体の共有を拒否する。"""

    def _link(self, source: Path, target: Path) -> None:
        try:
            os.link(source, target)
        except (OSError, NotImplementedError, AttributeError):  # pragma: no cover - hard link不可の環境
            pytest.skip("hard linkを作成できない環境")

    def test_verify_rejects_hard_linked_file(self, tmp_path: Path) -> None:
        outside_dir = tmp_path / "outside"
        create_private_dir(outside_dir)
        outside_file = outside_dir / "real-config"
        write_private_text(outside_file, "[credential]\n")

        root = tmp_path / "artifacts"
        create_private_dir(root)
        linked = root / "gitconfig"
        self._link(outside_file, linked)

        # 権限・所有者・pathは正当に見えるが、file実体はroot外と共有されている
        assert linked.read_text(encoding="utf-8") == "[credential]\n"
        with pytest.raises(FsPermissionError) as excinfo:
            verify_private_file(linked)
        assert excinfo.value.stage == "verify"

    def test_freshly_written_file_has_single_link(self, tmp_path: Path) -> None:
        root = tmp_path / "artifacts"
        create_private_dir(root)
        target = root / "notes.txt"
        write_private_text(target, "x")
        assert os.stat(target).st_nlink == 1
        verify_private_file(target)
