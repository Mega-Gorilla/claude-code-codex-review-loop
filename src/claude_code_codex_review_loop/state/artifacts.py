# SPDX-License-Identifier: Apache-2.0
"""local artifactとcanonical recordのbind照合（AC-C07-05。C-07 / ADR-0012）。

PR-2で追加した`artifact_records`（path / kind / content hash / approved head SHA /
record binding / comment ID）の読み手。resumeは「GitHub recordとlocal artifactが
**いずれもapproved head SHAへbindされている**」ことを確認し、確認できないartifactは
**cacheとして破棄**する（GitHub側が常に上位。ADR-0011 決定4のsilent repair禁止と同じ
方針で、破棄した事実は結果として返す）。

判定は純粋関数で、file内容のhashは`digest`として注入する。実I/O側の
`artifact_content_hash`は、containment違反・作成者限定でないfile・不在をいずれも
「読み出せない」（=bindを確認できない）へ倒す（fail closed）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path

from ..identity.fs_permissions import FsPermissionError, verify_private_file

_READ_CHUNK = 1 << 20


@dataclass(frozen=True)
class ArtifactBinding:
    """checkpointが記録するartifactのbind情報。"""

    path: str
    kind: str
    content_hash: str
    approved_head_sha: str
    record_binding: str | None = None
    comment_id: str | None = None


def _text(entry: Mapping[str, object], key: str) -> str | None:
    value = entry.get(key)
    return value if isinstance(value, str) else None


def read_artifact_bindings(payload: Mapping[str, object]) -> tuple[ArtifactBinding, ...]:
    """checkpoint payloadから`artifact_records`を読む（schema検証済みの値だけを見る）。

    必須fieldが文字列として揃っていないentryは、bindの主張として読めないため無視する
    （schema検証を通ったcheckpointでは起こらないが、cache側の欠損で判定を歪めない）。
    """
    records = payload.get("artifact_records")
    if not isinstance(records, list):
        return ()
    bindings: list[ArtifactBinding] = []
    for entry in records:
        if not isinstance(entry, dict):
            continue
        path = _text(entry, "path")
        kind = _text(entry, "kind")
        content_hash = _text(entry, "content_hash")
        approved = _text(entry, "approved_head_sha")
        if path is None or kind is None or content_hash is None or approved is None:
            continue
        bindings.append(
            ArtifactBinding(
                path=path,
                kind=kind,
                content_hash=content_hash,
                approved_head_sha=approved,
                record_binding=_text(entry, "record_binding"),
                comment_id=_text(entry, "comment_id"),
            )
        )
    return tuple(bindings)


@unique
class ArtifactStatus(Enum):
    """bind照合の結果。BOUND以外はcacheとして使わない。"""

    BOUND = "BOUND"
    STALE_HEAD = "STALE_HEAD"
    UNBOUND_RECORD = "UNBOUND_RECORD"
    MISSING = "MISSING"
    CONTENT_MISMATCH = "CONTENT_MISMATCH"


@dataclass(frozen=True)
class ArtifactCheck:
    """artifact 1件の照合結果。"""

    binding: ArtifactBinding
    status: ArtifactStatus
    detail: str | None = None

    @property
    def usable(self) -> bool:
        """cacheとして使ってよいか（BOUNDのみ）。"""
        return self.status is ArtifactStatus.BOUND


def verify_artifact_bindings(
    bindings: tuple[ArtifactBinding, ...],
    *,
    approved_head_sha: str,
    record_bindings: Collection[str],
    digest: Callable[[str], str | None],
) -> tuple[ArtifactCheck, ...]:
    """各artifactがapproved headと検証済みrecordの両方へbindされているか照合する。

    head不一致はfileを読む前に落とす（古いheadのartifactを読み出す理由が無い）。
    `record_bindings`は検証済みrecordのbinding集合で、そこに無いartifactは
    「GitHub側の記録に対応しないcache」として破棄する。
    """
    checks: list[ArtifactCheck] = []
    for binding in bindings:
        if binding.approved_head_sha != approved_head_sha:
            checks.append(
                ArtifactCheck(
                    binding=binding,
                    status=ArtifactStatus.STALE_HEAD,
                    detail=f"artifactは{binding.approved_head_sha}へbindされている",
                )
            )
            continue
        if binding.record_binding is None or binding.record_binding not in record_bindings:
            checks.append(
                ArtifactCheck(
                    binding=binding,
                    status=ArtifactStatus.UNBOUND_RECORD,
                    detail="検証済みrecordに対応するbindingが無い",
                )
            )
            continue
        observed = digest(binding.path)
        if observed is None:
            checks.append(
                ArtifactCheck(
                    binding=binding,
                    status=ArtifactStatus.MISSING,
                    detail="artifactを読み出せない（不在・権限・containment違反）",
                )
            )
            continue
        if observed != binding.content_hash:
            checks.append(
                ArtifactCheck(
                    binding=binding,
                    status=ArtifactStatus.CONTENT_MISMATCH,
                    detail="content hashが記録と一致しない",
                )
            )
            continue
        checks.append(ArtifactCheck(binding=binding, status=ArtifactStatus.BOUND))
    return tuple(checks)


def artifact_content_hash(base: Path, recorded_path: str) -> str | None:
    """artifactのcontent hash（読み出せない場合はNone）。

    `base`（通常はrun directory）配下のrelative pathとして解決し、次のいずれかに
    該当する場合はhashを返さない: base外を指す / 不在 / 作成者限定でない
    （AC-C06-05。private directory内のcacheという前提が崩れたfileを信用しない）。
    """
    candidate = Path(recorded_path)
    if candidate.is_absolute():
        return None
    root = base.resolve()
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        return None
    if not resolved.is_file():
        return None
    try:
        verify_private_file(resolved)
    except FsPermissionError:
        return None
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as handle:
            while chunk := handle.read(_READ_CHUNK):
                digest.update(chunk)
    except OSError:  # pragma: no cover - 権限検証直後の読取失敗は実質起きない
        return None
    return digest.hexdigest()
