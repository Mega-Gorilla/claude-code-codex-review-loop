# SPDX-License-Identifier: Apache-2.0
"""OS別file権限の共通契約の受入test（AC-C06-05。両OSで実行する）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_code_codex_review_loop.identity import (
    FsPermissionError,
    create_private_dir,
    fs_permissions,
    verify_private_dir,
    verify_private_file,
    write_private_text,
)
from claude_code_codex_review_loop.identity.fs_permissions import replace_private_text


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


class TestReplacePrivateText:
    """atomic replace（checkpointの更新経路。ADR-0011）。"""

    def test_replaces_content_and_keeps_permissions(self, tmp_path: Path) -> None:
        directory = tmp_path / "private"
        create_private_dir(directory)
        target = directory / "checkpoint.json"
        write_private_text(target, "old")
        replace_private_text(target, "new")
        assert target.read_text(encoding="utf-8") == "new"
        verify_private_file(target)

    def test_leaves_no_temporary_file(self, tmp_path: Path) -> None:
        directory = tmp_path / "private"
        create_private_dir(directory)
        target = directory / "checkpoint.json"
        write_private_text(target, "old")
        replace_private_text(target, "new")
        assert [entry.name for entry in directory.iterdir()] == [target.name]

    def test_replace_failure_removes_temporary_and_keeps_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = tmp_path / "private"
        create_private_dir(directory)
        target = directory / "checkpoint.json"
        write_private_text(target, "old")

        def _fail(source: object, destination: object) -> None:
            raise OSError(13, "replace failed")

        monkeypatch.setattr(fs_permissions.os, "replace", _fail)
        with pytest.raises(FsPermissionError, match="置換できない"):
            replace_private_text(target, "new")
        assert target.read_text(encoding="utf-8") == "old"
        assert [entry.name for entry in directory.iterdir()] == [target.name]

    def test_creates_target_when_absent(self, tmp_path: Path) -> None:
        """置換先が無い場合も、作成者限定のfileとして落ち着く。"""
        directory = tmp_path / "private"
        create_private_dir(directory)
        target = directory / "checkpoint.json"
        replace_private_text(target, "new")
        assert target.read_text(encoding="utf-8") == "new"
        verify_private_file(target)

    def test_confirmed_file_is_not_shared_with_a_temporary(self, tmp_path: Path) -> None:
        """確定後のfileはlink数1（公開後に一時fileが残ってlink数2になる経路を持たない）。"""
        directory = tmp_path / "private"
        create_private_dir(directory)
        target = directory / "checkpoint.json"
        replace_private_text(target, "first")
        replace_private_text(target, "second")
        assert os.stat(target).st_nlink == 1
        assert [entry.name for entry in directory.iterdir()] == [target.name]


class TestPartialWrite:
    """`os.write`のpartial writeを成功扱いにしない（ADR-0011 決定1）。"""

    def test_short_write_is_completed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """1回のwriteが短くても、全byteを書き切ってから確定する。"""
        directory = tmp_path / "private"
        create_private_dir(directory)
        target = directory / "checkpoint.json"
        real_write = os.write

        def _short_write(descriptor: int, data: bytes) -> int:
            return real_write(descriptor, data[:1])

        monkeypatch.setattr(fs_permissions.os, "write", _short_write)
        text = "x" * 100
        write_private_text(target, text)
        assert target.read_text(encoding="utf-8") == text

    def test_failed_write_leaves_no_partial_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """途中で書けなくなった場合、短い内容のfileを残さない。"""
        directory = tmp_path / "private"
        create_private_dir(directory)
        target = directory / "checkpoint.json"

        def _stalled_write(descriptor: int, data: bytes) -> int:
            return 0

        monkeypatch.setattr(fs_permissions.os, "write", _stalled_write)
        with pytest.raises(FsPermissionError, match="書き進められない"):
            write_private_text(target, "x" * 100)
        assert not target.exists()

    def test_write_error_is_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        directory = tmp_path / "private"
        create_private_dir(directory)
        target = directory / "checkpoint.json"

        def _failing_write(descriptor: int, data: bytes) -> int:
            raise OSError(28, "no space left")

        monkeypatch.setattr(fs_permissions.os, "write", _failing_write)
        with pytest.raises(FsPermissionError, match="書き込めない"):
            write_private_text(target, "x")
        assert not target.exists()
