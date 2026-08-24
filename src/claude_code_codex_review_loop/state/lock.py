# SPDX-License-Identifier: Apache-2.0
"""PR lockの取得・解放・検査（C-07。ADR-0011）。

同一PRへ複数のrunが同時に進むことを防ぐための**永続表現と照合**を提供する。
拒否そのもののworkflow動作（AC-C10-03）はC-10の責務で、本moduleは
「取得できたか / 誰が持っているか / 回収してよいか」を構造化して返すまでを行う。

- 内容はC-02の`RUN_LOCK` schemaで検証する。壊れたlockは`LockCorrupt`として扱い、
  「無いもの」として黙って上書きしない（silent repair禁止）
- **stale lockの回収は3条件がすべて揃った場合のみ**: 記録されたpidが生存していない、
  hostが一致する、再開しようとするrunのIDが一致する。pid生存の判定は曖昧な場合に
  「生存」へ倒れる（`process.is_process_alive`）ため、迷ったら回収しない
- **取得は新規・回収を問わず排他guard（`os.mkdir`）の下で直列化**し、guardの下で
  読んでから同じguardの下で確定する。読取と書込を別processが挟めないため、同じ状態を
  見た2 processが両方成功することがない（AC-C10-03の前提）
- lock fileの確定は一時fileへ書き切ってからの`os.replace`で行う。中断しても
  「lockが無い」か「完全なlock」のどちらかしか見えず、残り得るのは一意名の一時fileだけ
  である（読み手から見えず、次の取得を妨げない）
- 書き込むowner payloadは保存前に`RUN_LOCK`で検証する。consumerが受理できないlockを
  producerが作れてしまうと、silent repair禁止の下でそのpathを回復できなくなる
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..identity.fs_permissions import FsPermissionError, replace_private_text, verify_private_file
from ..process import is_process_alive
from ..schema.lock import RUN_LOCK
from ..schema.registry import validate, validate_object
from ..schema.validate import PublicError

ACQUIRE_GUARD_SUFFIX: Final = ".guard"


class LockGuardError(Exception):
    """取得guardを解放できない（残ると以後の取得が止まるため、silentに続行しない）。"""


class LockInputError(Exception):
    """呼び出し側が渡したowner情報が不正（consumerが受理できないlockを作らせない）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


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


def _validated_text(owner: LockOwner) -> str:
    """保存するlock payloadを検証してからJSONへ直列化する。

    schemaの整数fieldに下限は無いため、回収判定に使う値（pid / number）の意味制約は
    ここで併せて検証する。
    """
    if owner.pid < 1:
        raise LockInputError("pidは1以上でなければならない")
    if owner.number < 1:
        raise LockInputError("PR / Issue番号は1以上でなければならない")
    payload = _owner_payload(owner)
    result = validate_object(RUN_LOCK, dict(payload))
    if not result.ok:
        codes = ",".join(sorted(error.code for error in result.errors))
        raise LockInputError(f"lock payloadがschema検証を通らない（codes={codes}）")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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

    新規取得も回収も**排他guardの下で**行う（読取と書込の間に別processが入れない）。
    `acquired_at`は呼び出し側が渡す（時刻sourceを注入可能にし、testを決定論的にする）。
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
    text = _validated_text(owner)
    guard = path.with_name(f"{path.name}{ACQUIRE_GUARD_SUFFIX}")
    try:
        os.mkdir(guard)
    except FileExistsError:
        return LockUnavailable(
            path=path,
            detail=f"別processがlockを操作中（中断した場合は{guard.name}を削除する）",
        )
    except OSError as error:  # pragma: no cover - private dir配下のmkdir失敗は実質起きない
        return LockUnavailable(path=path, detail=f"取得guardを作成できない（errno={error.errno}）")
    try:
        return _acquire_under_guard(path, owner, text)
    finally:
        _remove_guard(guard)


def _remove_guard(guard: Path) -> None:
    """取得guardを解放する（残ると以後の取得が止まるため、失敗も無視しない）。"""
    try:
        guard.rmdir()
    except OSError as error:  # pragma: no cover - 直前に自分で作成したdirectoryの削除失敗は実質起きない
        raise LockGuardError(f"取得guardを解放できない: {guard}（errno={error.errno}）") from error


def _acquire_under_guard(path: Path, owner: LockOwner, text: str) -> LockResult:
    """guard保持中の取得本体（読取から確定までを1 processに限定する）。

    guardを取る前に読んだ状態を根拠にしない。guardの下で読み直してから判断し、同じ
    guardの下で確定するため、同じstale lockを見た2 processが両方成功することがない。
    生きているlockには触れず、確定は一時fileの`os.replace`で行う（作成途中の空fileも、
    公開後に残る二重linkも作らない）。
    """
    current = _read_owner(path)
    if isinstance(current, LockCorrupt | LockUnavailable):
        return current
    if current is not None:
        reason = _reclaim_reason(current, run_id=owner.run_id, host=owner.host)
        if reason is not None:
            return LockHeld(path=path, owner=current, reason=reason)
    try:
        replace_private_text(path, text)
    except FsPermissionError as error:
        return LockUnavailable(path=path, detail=error.detail)
    return LockAcquired(path=path, owner=owner)


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
