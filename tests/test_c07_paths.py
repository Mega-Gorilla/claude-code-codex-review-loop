# SPDX-License-Identifier: Apache-2.0
"""state root配置の受入test（ADR-0011）。

配置の決定論性、作成者限定であること、state root配下から出ないこと、
lockがrun directoryに依存しない（= worktreeやrun IDで分裂しない）ことを検証する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from c07_support.helpers import NUMBER, REPOSITORY, RUN, state_paths

from claude_code_codex_review_loop.identity.fs_permissions import verify_private_dir
from claude_code_codex_review_loop.schema.projection import ProjectionError
from claude_code_codex_review_loop.state import (
    CHECKPOINT_FILE_NAME,
    LOCK_SUFFIX,
    StatePathError,
    StatePaths,
    checkpoint_path,
    lock_path,
    prepare_state_root,
    repository_digest,
    run_directory,
)


class TestStateRoot:
    def test_subtrees_are_created_private(self, tmp_path: Path) -> None:
        paths = state_paths(tmp_path)
        for directory in (paths.root, paths.runs_dir, paths.locks_dir):
            assert directory.is_dir()
            verify_private_dir(directory)

    def test_existing_root_is_reused(self, tmp_path: Path) -> None:
        """2回目以降のrunは同じrootを使う（排他作成だけでは足りない）。"""
        first = state_paths(tmp_path)
        second = state_paths(tmp_path)
        assert first == second

    def test_relative_root_is_rejected(self, tmp_path: Path) -> None:
        """相対pathはcwd依存でstate rootが動くため受理しない。"""
        with pytest.raises(StatePathError, match="絶対path"):
            prepare_state_root(Path("state"))

    def test_non_canonical_root_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(StatePathError, match="絶対path"):
            prepare_state_root(tmp_path.resolve() / "sub" / ".." / "state")

    def test_root_with_loose_permissions_is_rejected(self, tmp_path: Path) -> None:
        """緩い権限で先に作られたdirectoryをそのまま使わない（fail closed）。"""
        root = tmp_path.resolve() / "loose"
        root.mkdir()
        with pytest.raises(Exception, match="作成者限定|DACL|mode"):
            prepare_state_root(root)


class TestRunPaths:
    def test_checkpoint_path_is_deterministic(self, tmp_path: Path) -> None:
        paths = state_paths(tmp_path)
        assert checkpoint_path(paths, RUN) == checkpoint_path(paths, RUN)
        assert checkpoint_path(paths, RUN).name == CHECKPOINT_FILE_NAME
        assert checkpoint_path(paths, RUN).parent == paths.runs_dir / RUN

    def test_run_directory_is_private(self, tmp_path: Path) -> None:
        paths = state_paths(tmp_path)
        verify_private_dir(run_directory(paths, RUN))

    @pytest.mark.parametrize(
        "run_id",
        ["", "run/1", "run\\1", "..", "run:1", "a" * 65],
        ids=["empty", "slash", "backslash", "parent", "colon", "too_long"],
    )
    def test_invalid_run_id_is_rejected(self, tmp_path: Path, run_id: str) -> None:
        """run IDはbinding（PR-1）とpathで同一の文字集合を共有する。"""
        paths = state_paths(tmp_path)
        with pytest.raises(ProjectionError):
            run_directory(paths, run_id)


class TestLockPaths:
    def test_lock_path_does_not_depend_on_run(self, tmp_path: Path) -> None:
        """同一PRのlockは、run IDやcwdが違っても同じpathを指す（AC-C10-03の前提）。"""
        paths = state_paths(tmp_path)
        first = lock_path(paths, REPOSITORY, NUMBER)
        second = lock_path(paths, REPOSITORY, NUMBER)
        assert first == second
        assert first.name == f"{NUMBER}{LOCK_SUFFIX}"
        assert paths.runs_dir not in first.parents

    def test_different_repository_or_number_differ(self, tmp_path: Path) -> None:
        paths = state_paths(tmp_path)
        base = lock_path(paths, REPOSITORY, NUMBER)
        assert lock_path(paths, "owner/other", NUMBER) != base
        assert lock_path(paths, REPOSITORY, NUMBER + 1) != base

    def test_digest_is_path_safe_and_stable(self) -> None:
        digest = repository_digest(REPOSITORY)
        assert digest == repository_digest(REPOSITORY)
        assert "/" not in digest and len(digest) == 32

    @pytest.mark.parametrize(
        "repository",
        ["owner", "owner/name/extra", "owner/../name", "", "own er/name"],
        ids=["no_slash", "extra_segment", "parent", "empty", "space"],
    )
    def test_invalid_repository_is_rejected(self, repository: str) -> None:
        with pytest.raises(StatePathError, match="owner/name"):
            repository_digest(repository)

    @pytest.mark.parametrize("number", [0, -1, True], ids=["zero", "negative", "bool"])
    def test_invalid_number_is_rejected(self, tmp_path: Path, number: object) -> None:
        paths = state_paths(tmp_path)
        with pytest.raises(StatePathError, match="1以上"):
            lock_path(paths, REPOSITORY, number)  # type: ignore[arg-type]


class TestContainment:
    def test_subtree_outside_root_is_rejected(self, tmp_path: Path) -> None:
        """手動構築されたStatePathsでも、state root外のsubtreeは使わない。"""
        paths = state_paths(tmp_path)
        outside = tmp_path.resolve() / "outside"
        forged = StatePaths(root=paths.root, runs_dir=outside, locks_dir=paths.locks_dir)
        with pytest.raises(StatePathError, match="state root配下"):
            run_directory(forged, RUN)

    def test_symlinked_run_directory_is_rejected(self, tmp_path: Path) -> None:
        """run directoryがstate root外へのlinkへ差し替えられた場合に検出する。"""
        paths = state_paths(tmp_path)
        outside = tmp_path.resolve() / "outside"
        outside.mkdir()
        target = paths.runs_dir / RUN
        try:
            target.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - symlink不可環境
            pytest.skip("symlinkを作成できない環境")
        with pytest.raises(Exception, match="state root配下|作成者限定|DACL|mode"):
            checkpoint_path(paths, RUN)
