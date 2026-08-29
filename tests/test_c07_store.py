# SPDX-License-Identifier: Apache-2.0
"""checkpoint storeの受入test（ADR-0011）。

- 保存は「schema検証 -> atomic replace」で、不正なcheckpointをfileへ落とさない
- 置換は中断してもtruncateされた中間状態を残さない（旧内容か新内容のどちらか）
- 読込結果は構造化直和で、壊れたcheckpointを「無いもの」として扱わない
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from c07_support.helpers import RUN, checkpoint_payload, state_paths

from claude_code_codex_review_loop.identity.fs_permissions import verify_private_file
from claude_code_codex_review_loop.state import (
    CheckpointLoaded,
    CheckpointMigrationUnavailable,
    CheckpointMissing,
    CheckpointPermissionViolation,
    CheckpointSchemaInvalid,
    CheckpointStoreError,
    CheckpointUnreadable,
    checkpoint_guard,
    checkpoint_path,
    load_checkpoint,
    save_checkpoint,
)
from claude_code_codex_review_loop.state import store as store_module


def _path(tmp_path: Path) -> Path:
    return checkpoint_path(state_paths(tmp_path), RUN)


class TestSaveAndLoad:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        payload = checkpoint_payload(state={"state": "RUNNING_REVIEW", "awaiting": "CODEX_CODE_REVIEW"})
        save_checkpoint(path, payload)
        result = load_checkpoint(path)
        assert isinstance(result, CheckpointLoaded)
        assert result.payload == payload and result.version == 2

    def test_saved_file_is_private(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        save_checkpoint(path, checkpoint_payload())
        verify_private_file(path)

    def test_update_replaces_content(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        save_checkpoint(path, checkpoint_payload(number=12))
        save_checkpoint(path, checkpoint_payload(number=13))
        result = load_checkpoint(path)
        assert isinstance(result, CheckpointLoaded) and result.payload["number"] == 13
        verify_private_file(path)

    def test_update_leaves_no_temporary_file(self, tmp_path: Path) -> None:
        """一時fileを残さない（次回の排他作成を妨げない）。"""
        path = _path(tmp_path)
        save_checkpoint(path, checkpoint_payload())
        save_checkpoint(path, checkpoint_payload(number=13))
        assert [entry.name for entry in path.parent.iterdir()] == [path.name]

    def test_failed_replace_keeps_previous_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """置換に失敗しても、旧内容がそのまま読める（truncateされた中間状態を作らない）。"""
        path = _path(tmp_path)
        save_checkpoint(path, checkpoint_payload(number=12))

        def _fail(source: object, target: object) -> None:
            raise OSError(13, "replace failed")

        monkeypatch.setattr(store_module.replace_private_text.__globals__["os"], "replace", _fail)
        with pytest.raises(CheckpointStoreError, match="書けない"):
            save_checkpoint(path, checkpoint_payload(number=13))
        result = load_checkpoint(path)
        assert isinstance(result, CheckpointLoaded) and result.payload["number"] == 12
        assert [entry.name for entry in path.parent.iterdir()] == [path.name]


class TestSaveRejections:
    def test_invalid_payload_is_not_persisted(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        with pytest.raises(CheckpointStoreError, match="schema検証"):
            save_checkpoint(path, {"schema_version": 1, "run_id": RUN})
        assert not path.exists()

    def test_invalid_update_keeps_previous_content(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        save_checkpoint(path, checkpoint_payload(number=12))
        with pytest.raises(CheckpointStoreError):
            save_checkpoint(path, checkpoint_payload(number="twelve"))
        result = load_checkpoint(path)
        assert isinstance(result, CheckpointLoaded) and result.payload["number"] == 12


class TestLoadResults:
    def test_missing(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        assert isinstance(load_checkpoint(path), CheckpointMissing)

    def test_schema_invalid(self, tmp_path: Path) -> None:
        """壊れたcheckpointは「無いもの」ではなく、stageとerrorを持つ結果になる。"""
        path = _path(tmp_path)
        save_checkpoint(path, checkpoint_payload())
        path.write_text("{ not json", encoding="utf-8")
        result = load_checkpoint(path)
        assert isinstance(result, CheckpointSchemaInvalid)
        assert result.stage == "json" and result.errors

    def test_unknown_version(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        save_checkpoint(path, checkpoint_payload())
        path.write_text(json.dumps(checkpoint_payload(schema_version=99)), encoding="utf-8")
        result = load_checkpoint(path)
        assert isinstance(result, CheckpointSchemaInvalid)
        assert result.stage == "version"

    def test_migration_unavailable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """migrationが登録されていない旧versionはsilentに無視しない。"""
        path = _path(tmp_path)
        save_checkpoint(path, checkpoint_payload())
        definition = store_module.CHECKPOINT
        spec = definition.versions[1]
        # 現行の次のversionを宣言しつつmigrationを登録しない定義へ差し替える
        # （chainが現行へ到達しない）
        future = dataclasses.replace(
            definition, versions={**definition.versions, definition.current_version + 1: spec}
        )
        monkeypatch.setattr(store_module, "CHECKPOINT", future)
        result = load_checkpoint(path)
        assert isinstance(result, CheckpointMigrationUnavailable)
        assert [error.code for error in result.errors] == ["migration_unavailable"]

    def test_permission_violation(self, tmp_path: Path) -> None:
        """作成者限定でないcheckpointは読み込まない（AC-C06-05の維持）。"""
        path = _path(tmp_path)
        save_checkpoint(path, checkpoint_payload())
        outside = tmp_path.resolve() / "shared.json"
        outside.write_text("{}", encoding="utf-8")
        path.unlink()
        try:
            path.hardlink_to(outside)
        except (OSError, NotImplementedError):  # pragma: no cover - hard link不可環境
            pytest.skip("hard linkを作成できない環境")
        result = load_checkpoint(path)
        assert isinstance(result, CheckpointPermissionViolation | CheckpointUnreadable)


class TestCheckpointGuard:
    """read-modify-writeの直列化（lost update防止）。

    `load_checkpoint`と`save_checkpoint`はそれぞれ原子的だが、その2つを跨いだ更新は
    原子的でない。2つの書き手が同じ旧payloadを読むと、後に保存した側が相手の変更を消す。
    """

    def test_the_guard_is_created_and_released(self, tmp_path: Path) -> None:
        path = _path(tmp_path)
        guard = path.with_name(f"{path.name}{store_module.CHECKPOINT_GUARD_SUFFIX}")
        with checkpoint_guard(path):
            assert guard.is_dir()
        assert not guard.exists()

    def test_a_held_guard_is_reported_after_the_attempts_run_out(self, tmp_path: Path) -> None:
        """取れないまま尽きたら**推測せず失敗させる**（silent repair禁止）。"""
        path = _path(tmp_path)
        path.with_name(f"{path.name}{store_module.CHECKPOINT_GUARD_SUFFIX}").mkdir(parents=True)
        with pytest.raises(CheckpointStoreError) as caught:
            with checkpoint_guard(path, attempts=2, wait_seconds=0.0):
                pytest.fail("guardを取れてはならない")
        assert caught.value.stage == "guard"
        assert "更新中" in caught.value.detail

    def test_the_guard_is_released_when_the_body_fails(self, tmp_path: Path) -> None:
        """解放し損ねると以後の更新が止まる。"""
        path = _path(tmp_path)
        guard = path.with_name(f"{path.name}{store_module.CHECKPOINT_GUARD_SUFFIX}")
        with pytest.raises(RuntimeError):
            with checkpoint_guard(path):
                raise RuntimeError("本体の失敗")
        assert not guard.exists()
