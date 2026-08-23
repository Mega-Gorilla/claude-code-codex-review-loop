# SPDX-License-Identifier: Apache-2.0
"""PR lockの取得・解放・検査（C-07。ADR-0011）。

同一PRへ複数のrunが同時に進むことを防ぐための**永続表現と照合**を提供する。
拒否そのもののworkflow動作（AC-C10-03）はC-10の責務で、本moduleは
「取得できたか / 誰が持っているか / 回収してよいか」を構造化して返すまでを行う。

- lock fileは一時fileへ書き切ってから`os.link`で公開する（存在すれば取得失敗）。
  他processからは「無い」か「完全な内容」のどちらかしか見えない
- 内容はC-02の`RUN_LOCK` schemaで検証する。壊れたlockは`LockCorrupt`として扱い、
  「無いもの」として黙って上書きしない（silent repair禁止）
- **stale lockの回収は3条件がすべて揃った場合のみ**: 記録されたpidが生存していない、
  hostが一致する、再開しようとするrunのIDが一致する。pid生存の判定は曖昧な場合に
  「生存」へ倒れる（`process.is_process_alive`）ため、迷ったら回収しない
- 回収は**排他guard（`os.mkdir`）の下で直列化**し、guardを取ってから読み直して3条件を
  再判定する。単なる置換 + 読み戻しでは同じstale lockを読んだ2 processが順に置換して
  **両方が成功**し得る（AC-C10-03を成立させられない）。lock fileを退避してから確認する
  方式も、**生きているlockを一度動かす**ため差し戻しに失敗すると保持者のlockが消える
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

from ..identity.fs_permissions import (
    FsPermissionError,
    publish_private_text,
    replace_private_text,
    verify_private_file,
)
from ..process import is_process_alive
from ..schema.lock import RUN_LOCK
from ..schema.registry import validate, validate_object
from ..schema.validate import PublicError

RECLAIM_GUARD_SUFFIX: Final = ".reclaim"


class LockGuardError(Exception):
    """回収guardを解放できない（残ると以後の回収が止まるため、silentに続行しない）。"""


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
    text = _validated_text(owner)
    existing = _read_owner(path)
    if existing is None:
        return _create_exclusively(path, owner, text)
    if isinstance(existing, LockCorrupt | LockUnavailable):
        return existing
    reason = _reclaim_reason(existing, run_id=run_id, host=owner.host)
    if reason is not None:
        return LockHeld(path=path, owner=existing, reason=reason)
    return _reclaim(path, owner, text, stale=existing)


def _create_exclusively(path: Path, owner: LockOwner, text: str) -> LockResult:
    """lockを原子的かつ排他的に作る（publishに成功したprocessだけが保持者になる）。

    他processからは「lockが無い」か「完全なlock」のどちらかしか見えない
    （作成途中の空fileを破損と誤認させない）。
    """
    try:
        published = publish_private_text(path, text)
    except FsPermissionError as error:
        return LockUnavailable(path=path, detail=error.detail)
    if not published:
        # 直前に別processが取得した（推測せず、現在の保持者を読み直す）
        current = _read_owner(path)
        if isinstance(current, LockOwner):
            return LockHeld(path=path, owner=current, reason="別processが先にlockを取得した")
        return LockUnavailable(path=path, detail="別processの取得と競合した")
    return LockAcquired(path=path, owner=owner)


def _reclaim(path: Path, owner: LockOwner, text: str, *, stale: LockOwner) -> LockResult:
    """stale lockを**排他guardの下で**引き継ぐ（同時回収で複数が成功しない）。

    単なる置換 + 読み戻しでは、同じstale lockを読んだ2 processが順に置換して両方が
    成功し得る（読み戻しは「自分の確認より前に奪われた」場合しか検出できない）。
    一方、lock fileを一時退避してから確認する方式は、**生きているlockを一度動かして
    しまう**ため、退避後の差し戻しに失敗すると真の保持者のlockが消える。

    そこで回収操作自体をguard directory（`os.mkdir`は存在すれば失敗する原子的な排他）で
    直列化し、guardの下で**読み直してから**3条件を再判定する。生きているlockには
    一切触れず、回収できるのは常に1 processだけになる。

    guardを保持したままprocessが落ちるとguardが残り、以後の回収は
    `LockUnavailable`になる（fail closed。復旧はguard directoryの削除）。
    """
    guard = path.with_name(f"{path.name}{RECLAIM_GUARD_SUFFIX}")
    try:
        os.mkdir(guard)
    except FileExistsError:
        return LockUnavailable(
            path=path,
            detail=f"別processがstale lockを回収中（中断した場合は{guard.name}を削除する）",
        )
    except OSError as error:  # pragma: no cover - private dir配下のmkdir失敗は実質起きない
        return LockUnavailable(path=path, detail=f"回収guardを作成できない（errno={error.errno}）")
    try:
        return _reclaim_under_guard(path, owner, text, stale=stale)
    finally:
        _remove_guard(guard)


def _remove_guard(guard: Path) -> None:
    """回収guardを解放する（残ると以後の回収が止まるため、失敗も無視しない）。"""
    try:
        guard.rmdir()
    except OSError as error:  # pragma: no cover - 直前に自分で作成したdirectoryの削除失敗は実質起きない
        raise LockGuardError(f"回収guardを解放できない: {guard}（errno={error.errno}）") from error


def _reclaim_under_guard(path: Path, owner: LockOwner, text: str, *, stale: LockOwner) -> LockResult:
    """guard保持中の回収本体。**読み直してから**3条件を再判定する。"""
    current = _read_owner(path)
    if current is None:
        # 回収を判断した後にlockが消えていた（正常に解放された）: 新規取得と同じ扱い
        return _create_exclusively(path, owner, text)
    if isinstance(current, LockCorrupt | LockUnavailable):
        return current
    reason = _reclaim_reason(current, run_id=owner.run_id, host=owner.host)
    if reason is not None:
        # guardを取る前に別runの正当なlockへ入れ替わっていた: 生きているlockを奪わない
        return LockHeld(path=path, owner=current, reason=reason)
    if current != stale:
        return LockHeld(
            path=path, owner=current, reason="回収の判断根拠にしたlockと内容が異なる"
        )
    try:
        replace_private_text(path, text)
    except FsPermissionError as error:  # pragma: no cover - guard下の置換失敗は実質起きない
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
