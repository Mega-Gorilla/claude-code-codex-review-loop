# SPDX-License-Identifier: Apache-2.0
"""C-09 固定`CODEX_HOME`の取得・検証・再生成（ADR-0031 決定2 / 7）のhermetic test。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claude_code_codex_review_loop.identity import create_private_dir
from claude_code_codex_review_loop.identity.fs_permissions import (
    FsPermissionError,
    reject_reparse_points,
    remove_tree,
    verify_private_file,
)
from claude_code_codex_review_loop.runtime import reviewer_home as module
from claude_code_codex_review_loop.runtime.reviewer_home import (
    MARKER_NAME,
    MARKER_PRODUCT,
    HomeError,
    acquire_fixed_home,
    create_home_dir,
    write_home_marker,
)


def _parent(tmp_path: Path) -> Path:
    parent = (tmp_path / "homes").resolve()
    create_private_dir(parent)
    return parent


def _valid_marker() -> str:
    return json.dumps({"product": MARKER_PRODUCT, "schema_version": 1, "created_at": "2026-09-15T00:00:00+00:00"})


def _junction(link: Path, target: Path) -> None:
    """Windowsではjunction、他ではsymlinkを作る。作れない環境はskip。"""
    try:
        if sys.platform == "win32":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        else:
            link.symlink_to(target, target_is_directory=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("junction / symlinkを作成できない環境")


class TestAcquireFixedHome:
    def test_acquires_the_lock_and_leaves_the_home_for_the_caller(self, tmp_path: Path) -> None:
        parent = _parent(tmp_path)
        lease = acquire_fixed_home(parent=parent, name="reviewer-home", disjoint_from=())
        assert lease.home == parent / "reviewer-home" and lease.lock == parent / "reviewer-home.lock"
        assert lease.lock.is_dir() and (lease.lock / "owner.json").is_file()
        assert not lease.home.exists()  # 中身は呼出側が作る
        lease.release()
        assert not lease.lock.exists()

    def test_release_is_idempotent(self, tmp_path: Path) -> None:
        lease = acquire_fixed_home(parent=_parent(tmp_path), name="reviewer-home", disjoint_from=())
        lease.release()
        lease.release()
        assert not lease.lock.exists()

    def test_existing_lock_fails_closed_without_taking_it(self, tmp_path: Path) -> None:
        parent = _parent(tmp_path)
        (parent / "reviewer-home.lock").mkdir()
        with pytest.raises(HomeError) as stopped:
            acquire_fixed_home(parent=parent, name="reviewer-home", disjoint_from=())
        assert stopped.value.stage == "home_locked" and (parent / "reviewer-home.lock").is_dir()

    def test_previous_home_with_a_valid_marker_is_removed(self, tmp_path: Path) -> None:
        parent = _parent(tmp_path)
        home = parent / "reviewer-home"
        create_home_dir(home)
        (home / MARKER_NAME).write_text(_valid_marker(), encoding="utf-8")
        (home / "tmp").mkdir()
        (home / "tmp" / "leftover").write_text("x", encoding="utf-8")
        lease = acquire_fixed_home(parent=parent, name="reviewer-home", disjoint_from=())
        assert not home.exists()
        lease.release()

    @pytest.mark.parametrize(
        ("marker", "stage"),
        (
            (None, "home_marker"),
            ("{not json", "home_marker"),
            (json.dumps({"product": "other", "schema_version": 1}), "home_marker"),
            (json.dumps({"product": MARKER_PRODUCT, "schema_version": 2}), "home_marker"),
            (json.dumps([]), "home_marker"),
            ("x" * 5000, "home_marker"),
        ),
        ids=("absent", "not_json", "other_product", "other_schema", "not_object", "oversized"),
    )
    def test_entry_without_a_verifiable_marker_is_left_untouched(
        self, tmp_path: Path, marker: str | None, stage: str
    ) -> None:
        parent = _parent(tmp_path)
        home = parent / "reviewer-home"
        home.mkdir()
        (home / "auth.json").write_text("{}", encoding="utf-8")
        if marker is not None:
            (home / MARKER_NAME).write_text(marker, encoding="utf-8")
        with pytest.raises(HomeError) as stopped:
            acquire_fixed_home(parent=parent, name="reviewer-home", disjoint_from=())
        assert stopped.value.stage == stage
        assert (home / "auth.json").read_text(encoding="utf-8") == "{}"
        assert not (parent / "reviewer-home.lock").exists()  # 停止時はlockを外す

    def test_file_or_symlink_at_the_home_path_is_rejected(self, tmp_path: Path) -> None:
        parent = _parent(tmp_path)
        (parent / "reviewer-home").write_text("not a dir", encoding="utf-8")
        with pytest.raises(HomeError) as stopped:
            acquire_fixed_home(parent=parent, name="reviewer-home", disjoint_from=())
        assert stopped.value.stage in {"home_entry", "reparse_point"}
        assert (parent / "reviewer-home").read_text(encoding="utf-8") == "not a dir"

    def test_junction_at_the_home_path_is_rejected_before_deletion(self, tmp_path: Path) -> None:
        parent = _parent(tmp_path)
        target = (tmp_path / "victim").resolve()
        target.mkdir()
        (target / "precious.txt").write_text("keep", encoding="utf-8")
        (target / MARKER_NAME).write_text(_valid_marker(), encoding="utf-8")
        _junction(parent / "reviewer-home", target)
        with pytest.raises(HomeError) as stopped:
            acquire_fixed_home(parent=parent, name="reviewer-home", disjoint_from=())
        assert stopped.value.stage in {"reparse_point", "home_entry"}
        assert (target / "precious.txt").read_text(encoding="utf-8") == "keep"

    @pytest.mark.parametrize("kind", ("equal", "inside", "contains"))
    def test_overlap_with_a_guarded_root_is_rejected(self, tmp_path: Path, kind: str) -> None:
        parent = _parent(tmp_path)
        home = parent / "reviewer-home"
        others = {"equal": home, "inside": home / "sub", "contains": parent}
        with pytest.raises(HomeError) as stopped:
            acquire_fixed_home(parent=parent, name="reviewer-home", disjoint_from=(others[kind],))
        assert stopped.value.stage == "home_overlap" and not (parent / "reviewer-home.lock").exists()

    @pytest.mark.parametrize("name", ("", ".", "..", "a/b", "a\\b", "c:d", "x.lock"))
    def test_invalid_name_is_rejected(self, tmp_path: Path, name: str) -> None:
        with pytest.raises(HomeError) as stopped:
            acquire_fixed_home(parent=_parent(tmp_path), name=name, disjoint_from=())
        assert stopped.value.stage == "name"

    @pytest.mark.parametrize("kind", ("relative", "missing", "not_private"))
    def test_invalid_parent_is_rejected(self, tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        parent = _parent(tmp_path)
        if kind == "not_private":
            monkeypatch.setattr(
                module, "verify_private_dir", lambda path: (_ for _ in ()).throw(FsPermissionError("verify", "t"))
            )
        candidates = {"relative": Path("relative"), "missing": (tmp_path / "missing").resolve(), "not_private": parent}
        with pytest.raises(HomeError) as stopped:
            acquire_fixed_home(parent=candidates[kind], name="reviewer-home", disjoint_from=())
        assert stopped.value.stage == "parent"

    def test_lock_creation_failure_is_classified(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        parent = _parent(tmp_path)
        original = Path.mkdir

        def failing(self: Path, *args: object, **kwargs: object) -> None:
            if self.name.endswith(".lock"):
                raise PermissionError("test")
            original(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "mkdir", failing)
        with pytest.raises(HomeError) as stopped:
            acquire_fixed_home(parent=parent, name="reviewer-home", disjoint_from=())
        assert stopped.value.stage == "lock"

    def test_removal_failure_is_classified_and_releases_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parent = _parent(tmp_path)
        home = parent / "reviewer-home"
        create_home_dir(home)
        (home / MARKER_NAME).write_text(_valid_marker(), encoding="utf-8")
        monkeypatch.setattr(
            module, "remove_tree", lambda root: (_ for _ in ()).throw(FsPermissionError("remove", "t"))
        )
        with pytest.raises(HomeError) as stopped:
            acquire_fixed_home(parent=parent, name="reviewer-home", disjoint_from=())
        assert stopped.value.stage == "remove" and not (parent / "reviewer-home.lock").exists()

    def test_unresolvable_guarded_root_is_classified(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        parent = _parent(tmp_path)
        original = Path.resolve

        def failing(self: Path, strict: bool = False) -> Path:
            if self.name == "broken":
                raise OSError("test")
            return original(self, strict)

        monkeypatch.setattr(Path, "resolve", failing)
        with pytest.raises(HomeError) as stopped:
            acquire_fixed_home(parent=parent, name="reviewer-home", disjoint_from=(tmp_path / "broken",))
        assert stopped.value.stage == "home_overlap"


class TestHomeMarker:
    def test_marker_is_a_private_file_that_validates(self, tmp_path: Path) -> None:
        parent = _parent(tmp_path)
        home = parent / "reviewer-home"
        create_home_dir(home)
        marker = write_home_marker(home)
        verify_private_file(marker)
        parsed = json.loads(marker.read_text(encoding="utf-8"))
        assert parsed["product"] == MARKER_PRODUCT and parsed["schema_version"] == 1
        # 次回の取得で削除対象として認められる
        lease = acquire_fixed_home(parent=parent, name="reviewer-home", disjoint_from=())
        assert not home.exists()
        lease.release()

    def test_marker_write_failure_is_classified(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _parent(tmp_path) / "reviewer-home"
        create_home_dir(home)
        monkeypatch.setattr(
            module, "write_private_text", lambda *args: (_ for _ in ()).throw(FsPermissionError("write", "t"))
        )
        with pytest.raises(HomeError) as stopped:
            write_home_marker(home)
        assert stopped.value.stage == "marker"

    def test_create_home_dir_failure_is_classified(self, tmp_path: Path) -> None:
        home = _parent(tmp_path) / "reviewer-home"
        home.mkdir()
        with pytest.raises(HomeError) as stopped:
            create_home_dir(home)
        assert stopped.value.stage == "create"


class TestFsHelpers:
    def test_reject_reparse_points_accepts_plain_and_missing_paths(self, tmp_path: Path) -> None:
        base = tmp_path.resolve()
        (base / "a").mkdir()
        reject_reparse_points(base / "a" / "missing", stop_at=base)
        reject_reparse_points(base, stop_at=base)

    def test_reject_reparse_points_requires_containment(self, tmp_path: Path) -> None:
        base = tmp_path.resolve()
        with pytest.raises(FsPermissionError):
            reject_reparse_points(base.parent, stop_at=base)

    def test_reject_reparse_points_detects_a_junction_or_symlink(self, tmp_path: Path) -> None:
        base = tmp_path.resolve()
        target = base / "target"
        target.mkdir()
        _junction(base / "link", target)
        with pytest.raises(FsPermissionError):
            reject_reparse_points(base / "link" / "child", stop_at=base)

    def test_remove_tree_clears_read_only_entries(self, tmp_path: Path) -> None:
        root = tmp_path / "tree"
        (root / "sub").mkdir(parents=True)
        victim = root / "sub" / "ro.txt"
        victim.write_text("x", encoding="utf-8")
        os.chmod(victim, 0o444)
        remove_tree(root)
        assert not root.exists()

    def test_remove_tree_does_not_follow_symlinked_entries(self, tmp_path: Path) -> None:
        root = tmp_path / "tree"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        try:
            (root / "link").symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlinkを作成できない環境")
        remove_tree(root)
        assert not root.exists() and (outside / "keep.txt").read_text(encoding="utf-8") == "keep"

    def test_remove_tree_failure_is_classified(self, tmp_path: Path) -> None:
        with pytest.raises(FsPermissionError) as stopped:
            remove_tree(tmp_path / "missing")
        assert stopped.value.stage == "remove"
