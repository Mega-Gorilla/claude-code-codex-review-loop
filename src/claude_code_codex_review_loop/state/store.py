# SPDX-License-Identifier: Apache-2.0
"""checkpointの読み書き（C-07。ADR-0011）。

checkpointは**GitHubがcanonicalな会話に対するcache**であり、単独の判断根拠にしない。
本moduleの責務はI/Oと構造化した結果の提供までで、GitHubとの照合はresume（PR-3）が行う。

- 保存は「先にschema検証 -> atomic replace」。不正なcheckpointを永続化しない
- 更新は`identity.fs_permissions.replace_private_text`（一時file + `os.replace`）で、
  中断してもtruncateされた中間状態を残さない。世代は保存しない
- 読込結果は真偽値ではなく**構造化直和**にする。missing / unreadable /
  permission-violation / schema-invalid / migration-unavailableを区別し、
  壊れたcheckpointを「無いもの」として黙って上書きしない（silent repair禁止）
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..identity.fs_permissions import (
    FsPermissionError,
    replace_private_text,
    verify_private_file,
    write_private_text,
)
from ..schema.envelope import CHECKPOINT
from ..schema.migrate import load_with_migration
from ..schema.registry import validate_object
from ..schema.validate import PublicError


class CheckpointStoreError(Exception):
    """checkpointの保存失敗（呼び出し側の誤りまたはfs側の失敗）。"""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


@dataclass(frozen=True)
class CheckpointLoaded:
    """検証済みcheckpoint（現行versionへmigration済み）。"""

    payload: dict[str, object]
    version: int


@dataclass(frozen=True)
class CheckpointMissing:
    """checkpointが存在しない（fresh resume。violationではない）。"""

    path: Path


@dataclass(frozen=True)
class CheckpointUnreadable:
    """存在するが読めない（I/O error）。fresh resumeへ迂回しない。"""

    path: Path
    detail: str


@dataclass(frozen=True)
class CheckpointPermissionViolation:
    """権限・file実体が作成者限定の契約から外れている（AC-C06-05）。"""

    path: Path
    detail: str


@dataclass(frozen=True)
class CheckpointSchemaInvalid:
    """schema検証に失敗した（stageとerrorをそのまま提示する）。"""

    path: Path
    stage: str | None
    errors: tuple[PublicError, ...]


@dataclass(frozen=True)
class CheckpointMigrationUnavailable:
    """既知だがmigrationできないversion（silentに無視しない）。"""

    path: Path
    errors: tuple[PublicError, ...]


CheckpointLoadResult = (
    CheckpointLoaded
    | CheckpointMissing
    | CheckpointUnreadable
    | CheckpointPermissionViolation
    | CheckpointSchemaInvalid
    | CheckpointMigrationUnavailable
)


def save_checkpoint(path: Path, payload: dict[str, object]) -> None:
    """checkpointを検証してから原子的に保存する（既存が無ければ排他作成）。

    schema検証を先に行い、**不正なcheckpointをfileへ落とさない**。既存checkpointは
    置換前に権限を検証し、作成者限定でないfileを上書きしない（fail closed）。
    """
    result = validate_object(CHECKPOINT, dict(payload))
    if not result.ok:
        codes = ",".join(sorted(error.code for error in result.errors))
        raise CheckpointStoreError("validate", f"checkpointがschema検証を通らない（codes={codes}）")
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        if path.exists():
            verify_private_file(path)
            replace_private_text(path, text)
        else:
            write_private_text(path, text)
    except FsPermissionError as error:
        raise CheckpointStoreError("write", f"checkpointを書けない: {error.detail}") from error


def load_checkpoint(path: Path) -> CheckpointLoadResult:
    """checkpointを読み、結果を構造化直和で返す（例外で失敗を伝えない）。"""
    if not path.exists():
        return CheckpointMissing(path=path)
    try:
        verify_private_file(path)
    except FsPermissionError as error:
        return CheckpointPermissionViolation(path=path, detail=error.detail)
    try:
        raw = path.read_bytes()
    except OSError as error:  # pragma: no cover - 権限検証直後の読取失敗は実質起きない
        return CheckpointUnreadable(path=path, detail=str(error.errno))
    result = load_with_migration(CHECKPOINT, raw)
    if result.ok:
        payload = result.payload
        version = result.version
        if payload is None or version is None:  # pragma: no cover - 成功時は必ず両方が入る
            return CheckpointUnreadable(path=path, detail="検証結果にpayloadが無い")
        return CheckpointLoaded(payload=dict(payload), version=version)
    if result.stage == "migration":
        return CheckpointMigrationUnavailable(path=path, errors=result.errors)
    return CheckpointSchemaInvalid(path=path, stage=result.stage, errors=result.errors)
