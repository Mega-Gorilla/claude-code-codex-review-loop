# SPDX-License-Identifier: Apache-2.0
"""PR lockの取得・解放・検査（C-07。ADR-0011）。

同一PRへ複数のrunが同時に進むことを防ぐための**永続表現と照合**を提供する。
拒否そのもののworkflow動作（AC-C10-03）はC-10の責務で、本moduleは
「取得できたか / 誰が持っているか / 回収してよいか」を構造化して返すまでを行う。

- lock fileは`O_CREAT | O_EXCL`で作成する（存在すれば取得失敗）
- 内容はC-02の`RUN_LOCK` schemaで検証する。壊れたlockは`LockCorrupt`として扱い、
  「無いもの」として黙って上書きしない（silent repair禁止）
- **stale lockの回収は3条件がすべて揃った場合のみ**: 記録されたpidが生存していない、
  hostが一致する、再開しようとするrunのIDが一致する。pid生存の判定は曖昧な場合に
  「生存」へ倒れる（`process.is_process_alive`）ため、迷ったら回収しない
- 回収は`os.replace`で行い、書込後に自runのrun ID / pidを読み戻して確認する
  （同時に回収を試みた別processに奪われていないことの確認）
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path

from ..identity.fs_permissions import (
    FsPermissionError,
    replace_private_text,
    verify_private_file,
    write_private_text,
)
from ..process import is_process_alive
from ..schema.lock import RUN_LOCK
from ..schema.registry import validate
from ..schema.validate import PublicError


@dataclass(frozen=True)
class LockOwner:
    """lock fileが記録している保持者（検証済み）。"""

    run_id: str
    repository: str
    number: int
    pid: int
    host: str
    acquired_at: str
    head_sha: str | None = None


@dataclass(frozen=True)
class LockAcquired:
    """lockを取得した（自runが保持者）。"""

    path: Path
    owner: LockOwner


@dataclass(frozen=True)
class LockHeld:
    """他runが保持している（回収条件を満たさない）。理由を添えて停止する。"""

    path: Path
    owner: LockOwner
    reason: str


@dataclass(frozen=True)
class LockCorrupt:
    """lock fileを解釈できない（黙って上書きしない）。"""

    path: Path
    stage: str | None
    errors: tuple[PublicError, ...]


@dataclass(frozen=True)
class LockUnavailable:
    """権限違反・I/O失敗でlockを扱えない。"""

    path: Path
    detail: str


LockResult = LockAcquired | LockHeld | LockCorrupt | LockUnavailable
LockInspection = LockOwner | LockCorrupt | LockUnavailable | None


def current_host() -> str:
    """回収条件のhost一致判定に使う識別子。"""
    return socket.gethostname()


def _owner_payload(owner: LockOwner) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": owner.run_id,
        "repository": owner.repository,
        "number": owner.number,
        "pid": owner.pid,
        "host": owner.host,
        "acquired_at": owner.acquired_at,
    }
    if owner.head_sha is not None:
        payload["head_sha"] = owner.head_sha
    return payload


def _read_owner(path: Path) -> LockInspection:
    """既存lockを読んで検証する（存在しなければNone）。"""
    if not path.exists():
        return None
    try:
        verify_private_file(path)
        raw = path.read_bytes()
    except FsPermissionError as error:
        return LockUnavailable(path=path, detail=error.detail)
    except OSError as error:  # pragma: no cover - 権限検証直後の読取失敗は実質起きない
        return LockUnavailable(path=path, detail=str(error.errno))
    result = validate(RUN_LOCK, raw)
    if not result.ok or result.payload is None:
        return LockCorrupt(path=path, stage=result.stage, errors=result.errors)
    payload = result.payload
    head = payload.get("head_sha")
    return LockOwner(
        run_id=str(payload["run_id"]),
        repository=str(payload["repository"]),
        number=int(str(payload["number"])),
        pid=int(str(payload["pid"])),
        host=str(payload["host"]),
        acquired_at=str(payload["acquired_at"]),
        head_sha=str(head) if isinstance(head, str) else None,
    )


def inspect_pr_lock(path: Path) -> LockInspection:
    """lockの現在の保持者を返す（取得も回収も行わない）。"""
    return _read_owner(path)


def _reclaim_reason(owner: LockOwner, *, run_id: str, host: str) -> str | None:
    """回収を拒む理由（回収してよい場合はNone）。3条件はすべて必須。"""
    if owner.host != host:
        return "別hostのrunが保持している（host跨ぎの回収は行わない）"
    if owner.run_id != run_id:
        return "別runが保持している（同一runのresumeでのみ回収する）"
    if is_process_alive(owner.pid):
        return "保持しているprocessが生存している"
    return None


def acquire_pr_lock(
    path: Path,
    *,
    run_id: str,
    repository: str,
    number: int,
    acquired_at: str,
    head_sha: str | None = None,
    host: str | None = None,
    pid: int | None = None,
) -> LockResult:
    """PR lockを取得する。既存lockは検証し、回収3条件を満たす場合だけ引き継ぐ。

    `acquired_at`は呼び出し側が渡す（時刻source を注入可能にし、testを決定論的にする）。
    """
    owner = LockOwner(
        run_id=run_id,
        repository=repository,
        number=number,
        pid=os.getpid() if pid is None else pid,
        host=current_host() if host is None else host,
        acquired_at=acquired_at,
        head_sha=head_sha,
    )
    text = json.dumps(_owner_payload(owner), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    existing = _read_owner(path)
    if existing is None:
        try:
            write_private_text(path, text)
        except FsPermissionError as error:
            # 排他作成の失敗は「直前に別processが取得した」可能性を含む（推測しない）
            return LockUnavailable(path=path, detail=error.detail)
        return LockAcquired(path=path, owner=owner)
    if isinstance(existing, LockCorrupt | LockUnavailable):
        return existing
    reason = _reclaim_reason(existing, run_id=run_id, host=owner.host)
    if reason is not None:
        return LockHeld(path=path, owner=existing, reason=reason)
    try:
        replace_private_text(path, text)
    except FsPermissionError as error:  # pragma: no cover - 権限検証済みpathの置換失敗は実質起きない
        return LockUnavailable(path=path, detail=error.detail)
    # 回収の競合検出: 書込後に読み戻し、自runのrun ID / pidであることを確認する
    confirmed = _read_owner(path)
    if not isinstance(confirmed, LockOwner):
        return LockUnavailable(path=path, detail="回収後のlockを読み戻せない")
    if (confirmed.run_id, confirmed.pid, confirmed.host) != (owner.run_id, owner.pid, owner.host):
        return LockHeld(path=path, owner=confirmed, reason="回収の直後に別processがlockを取得した")
    return LockAcquired(path=path, owner=confirmed)


def release_pr_lock(path: Path, *, run_id: str, pid: int | None = None) -> bool:
    """自runが保持しているlockだけを解放する（他runのlockは削除しない）。

    解放できた場合はTrue、保持者が別run・別pid・不在・解釈不能な場合はFalseを返す。
    """
    owner = _read_owner(path)
    if not isinstance(owner, LockOwner):
        return False
    if owner.run_id != run_id or owner.pid != (os.getpid() if pid is None else pid):
        return False
    try:
        path.unlink()
    except OSError:  # pragma: no cover - 直前に検証したprivate fileの削除失敗は実質起きない
        return False
    return True
