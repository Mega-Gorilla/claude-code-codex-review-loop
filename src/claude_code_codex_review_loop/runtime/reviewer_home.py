# SPDX-License-Identifier: Apache-2.0
"""C-09 reviewer用の固定`CODEX_HOME`の取得・検証・再生成（ADR-0031 決定2 / 7）。

reviewerの認証はOS credential storeに置き、そのkeyは`CODEX_HOME`のcanonical pathから導出される。
そのためhomeのpathは固定し、内容だけをrunごとに再生成する。再生成は削除を伴うため、誤設定で
ユーザーのdataを消さないよう、**削除より前に**次を検証し、検証できないentryは削除せず停止する。

1. sibling lock（`<home>.lock`）をexclusive createで取得する（取得できなければ`home_locked`）
2. parentがprivate dirで、homeがその直下にあり、path上にsymlink / junction / reparse pointが無い
3. homeがcoderの`CODEX_HOME`・実repository・protected root・run rootのいずれとも重ならない
4. 既存entryはproduct ownership markerが検証できるときだけ削除する

本moduleはhomeの中身（config・provisioning成果物）を作らない。作るのは呼出側で、作成後に
`write_home_marker`でmarkerを置く。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from ..identity.fs_permissions import (
    FsPermissionError,
    create_private_dir,
    reject_reparse_points,
    remove_tree,
    verify_private_dir,
    write_private_text,
)

MARKER_NAME: Final = ".cc-review-reviewer-home.json"
MARKER_PRODUCT: Final = "claude-code-codex-review-loop"
MARKER_SCHEMA_VERSION: Final = 1
LOCK_SUFFIX: Final = ".lock"
_MAX_MARKER_BYTES: Final = 4_096


class HomeError(Exception):
    """固定stageだけを公開する固定homeの失敗。path・内容を含めない。"""

    def __init__(self, stage: str) -> None:
        super().__init__(f"reviewer_home_error: {stage}")
        self.stage = stage


@dataclass(frozen=True)
class FixedHomeLease:
    """lockを保持している間だけ有効なhomeの占有。呼出側はreviewerの終了後に`release`する。"""

    parent: Path
    name: str
    home: Path
    lock: Path

    def release(self) -> None:
        """lockを外す。既に無ければ何もしない（停止理由を置き換えない）。"""
        try:
            (self.lock / "owner.json").unlink(missing_ok=True)
            self.lock.rmdir()
        except OSError:
            pass


def acquire_fixed_home(*, parent: Path, name: str, disjoint_from: tuple[Path, ...]) -> FixedHomeLease:
    """lockを取り、検証し、前回のhomeを（markerが検証できる場合だけ）取り除く。homeの中身は作らない。"""
    _validate_name(name)
    root = _canonical_private_dir(parent, "parent")
    home = root / name
    lock = root / f"{name}{LOCK_SUFFIX}"
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise HomeError("home_locked") from error
    except OSError as error:
        raise HomeError("lock") from error
    lease = FixedHomeLease(parent=root, name=name, home=home, lock=lock)
    try:
        write_private_text(
            lock / "owner.json",
            json.dumps({"lease": uuid.uuid4().hex, "acquired_at": datetime.now(UTC).isoformat()}),
        )
        _validate_placement(home, root, disjoint_from)
        _remove_previous_home(home)
    except BaseException:
        lease.release()
        raise
    return lease


def write_home_marker(home: Path) -> Path:
    """呼出側がhomeを作った直後に、product ownership markerを私有fileとして置く。"""
    marker = home / MARKER_NAME
    try:
        write_private_text(
            marker,
            json.dumps(
                {
                    "product": MARKER_PRODUCT,
                    "schema_version": MARKER_SCHEMA_VERSION,
                    "created_at": datetime.now(UTC).isoformat(),
                }
            ),
        )
    except (FsPermissionError, OSError) as error:
        raise HomeError("marker") from error
    return marker


def _validate_name(name: str) -> None:
    forbidden = ("/", "\\", ":")
    if (
        not name
        or name in {".", ".."}
        or name.endswith(LOCK_SUFFIX)
        or Path(name).name != name
        or any(character in name for character in forbidden)
    ):
        raise HomeError("name")


def _canonical_private_dir(path: Path, stage: str) -> Path:
    try:
        candidate = Path(path)
        if not candidate.is_absolute() or candidate != candidate.resolve() or not candidate.is_dir():
            raise HomeError(stage)
        verify_private_dir(candidate)
    except (FsPermissionError, OSError) as error:
        raise HomeError(stage) from error
    return candidate


def _validate_placement(home: Path, root: Path, disjoint_from: tuple[Path, ...]) -> None:
    try:
        reject_reparse_points(home, stop_at=root)
    except (FsPermissionError, OSError) as error:
        raise HomeError("reparse_point") from error
    for other in disjoint_from:
        try:
            candidate = Path(other).resolve()
        except OSError as error:
            raise HomeError("home_overlap") from error
        if home == candidate or home.is_relative_to(candidate) or candidate.is_relative_to(home):
            raise HomeError("home_overlap")


def _remove_previous_home(home: Path) -> None:
    """前回のhomeを、markerが検証できる場合だけ取り除く。それ以外のentryは触らない。

    symlink / junctionは`_validate_placement`のreparse point検査で既に拒否されている。
    """
    if not home.exists():
        return
    if not home.is_dir():
        raise HomeError("home_entry")
    if not _marker_is_valid(home / MARKER_NAME):
        raise HomeError("home_marker")
    try:
        remove_tree(home)
    except FsPermissionError as error:
        raise HomeError("remove") from error


def _marker_is_valid(marker: Path) -> bool:
    try:
        if marker.is_symlink() or not marker.is_file():
            return False
        raw = marker.read_bytes()
        if len(raw) > _MAX_MARKER_BYTES:
            return False
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(parsed, dict)
        and parsed.get("product") == MARKER_PRODUCT
        and parsed.get("schema_version") == MARKER_SCHEMA_VERSION
    )


def create_home_dir(home: Path) -> None:
    """検証済みのhome pathへprivate dirを作る（呼出側が`prepare_codex_canary_home`を使わない経路用）。"""
    try:
        create_private_dir(home)
    except (FsPermissionError, OSError) as error:
        raise HomeError("create") from error
