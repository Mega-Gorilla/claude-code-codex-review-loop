# SPDX-License-Identifier: Apache-2.0
"""host resultの受理（Phase 8。AC-C08-05 / ADR-0015）。

result pathは**Controllerがrun directory内へ払い出す**（implementation plan L295）。
呼び出し側から任意pathを受理しないため、受理時に次を検証する。

1. relative pathであり、run directory配下へ収まる（containment）
2. path上にsymlink / reparse pointが無い（`path == path.resolve()`で実体判定する。
   字句判定だけでは`..`とsymlinkを素通りさせる。C-06 / C-07で同じ判定へ揃えた）
3. 実在するregular fileである
4. size limit以下（**読み込む前に**`stat`で判定する）
5. 作成者限定の権限で、他pathと実体を共有しない（`verify_private_file`）
6. 内容が当該result variantのschema検証を通る

`max_bytes`は引数で受け取る（既定値の解決はC-12）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..identity.fs_permissions import FsPermissionError, verify_private_file
from ..schema.registry import SchemaDefinition, validate


@dataclass(frozen=True)
class ResultAccepted:
    """検証を通ったresult file。"""

    path: Path
    payload: dict[str, object]
    content_hash: str


@dataclass(frozen=True)
class ResultRejected:
    """受理しない理由。codeは診断とtestの安定した識別子。"""

    code: str
    detail: str


ResultOutcome = ResultAccepted | ResultRejected


def _resolve(base: Path, recorded_path: str) -> Path | ResultRejected:
    candidate = Path(recorded_path)
    if candidate.is_absolute():
        return ResultRejected("absolute_path", f"result pathが絶対pathである: {recorded_path}")
    target = base / candidate
    resolved = target.resolve()
    if not resolved.is_relative_to(base):
        return ResultRejected("outside_run_directory", f"result pathがrun directory外を指す: {recorded_path}")
    if target != resolved:
        # run directory内へ収まっていても、`..`やsymlinkを経由するpathは受理しない
        # （実体との差として現れるため、字句判定に依存しない）
        return ResultRejected(
            "non_canonical_path", f"result pathが正規化済みでない（.. またはsymlink）: {recorded_path}"
        )
    return target


def read_result(
    base: Path, recorded_path: str, *, definition: SchemaDefinition, max_bytes: int
) -> ResultOutcome:
    """run directory配下のresult fileを受理する（読めない・信用できない場合は理由を返す）。"""
    root = base.resolve()
    target = _resolve(root, recorded_path)
    if isinstance(target, ResultRejected):
        return target
    if not target.is_file():
        return ResultRejected("missing", f"result fileが無い: {recorded_path}")
    size = target.stat().st_size
    if size > max_bytes:
        return ResultRejected("too_large", f"result fileがsize上限{max_bytes}を超える（{size}）")
    try:
        verify_private_file(target)
    except FsPermissionError as error:
        return ResultRejected("not_private", f"result fileが作成者限定でない: {error}")
    raw = target.read_bytes()
    result = validate(definition, raw)
    if not result.ok or result.payload is None:
        codes = ",".join(sorted(error.code for error in result.errors))
        return ResultRejected("schema_invalid", f"result fileがschema検証を通らない（{codes}）")
    return ResultAccepted(
        path=target, payload=result.payload, content_hash=hashlib.sha256(raw).hexdigest()
    )
