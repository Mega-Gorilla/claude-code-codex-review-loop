# SPDX-License-Identifier: Apache-2.0
"""C-09 elevated Windows sandbox backendのprovisioning成果物を専用`CODEX_HOME`へ複製する（ADR-0029）。

elevated backendのprovisioning完了判定は`CODEX_HOME`ごとの2 file（marker / users）のversion一致で
決まり、provisioning自体は実行のたびにsandbox userのpasswordを回転させる。runごとの専用homeを
追加provisioningすると`~/.codex`側が失効するため、本moduleはauthoritative homeの2 fileだけを
専用homeへ複製する。provisioningは走らせない。

- 複製するのは`.sandbox/setup_marker.json`と`.sandbox-secrets/sandbox_users.json`だけ。
  provider認証（`auth.json`）・config・session・logは複製しない
- 複製元は呼出側が明示する。`~/.codex`や`CODEX_HOME`環境変数を暗黙に探索しない
- markerはproxy設定が無いことを要求する（専用configはproxyを設定せず、差があるとCodexが
  setup driftとみなして再provisioningを試み得る）
- secretsの中身（password blob）は検証・復号・logしない。例外へ本文を含めない
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..identity.fs_permissions import FsPermissionError, create_private_dir, write_private_text
from .codex_canary import CodexCanaryHome

MARKER_RELATIVE_PATH: Final = Path(".sandbox") / "setup_marker.json"
USERS_RELATIVE_PATH: Final = Path(".sandbox-secrets") / "sandbox_users.json"
MAX_ARTIFACT_BYTES: Final = 65_536


class ProvisioningError(Exception):
    """固定stageだけを公開するprovisioning成果物の複製の失敗。内容やpathは含めない。"""

    def __init__(self, stage: str) -> None:
        super().__init__(f"codex_provisioning_error: {stage}")
        self.stage = stage


@dataclass(frozen=True)
class ProvisioningMirror:
    """複製の記録。versionはevidence用で、secretsの内容は持たない。"""

    source: Path
    marker_path: Path
    users_path: Path
    marker_version: int
    users_version: int


def mirror_provisioning_artifacts(home: CodexCanaryHome, source: Path) -> ProvisioningMirror:
    """authoritative homeの2 fileを検証して専用homeへ複製する。既に複製済みなら`destination`で停止する。"""
    root = _canonical_source(source)
    marker_text, marker = _load_artifact(root / MARKER_RELATIVE_PATH, "marker")
    users_text, users = _load_artifact(root / USERS_RELATIVE_PATH, "users")
    marker_version = _version(marker)
    users_version = _version(users)
    for key in ("offline_username", "online_username"):
        value = marker.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ProvisioningError("usernames")
    if marker.get("proxy_ports") != [] or marker.get("allow_local_binding") is not False:
        raise ProvisioningError("proxy")
    marker_path = home.root / MARKER_RELATIVE_PATH
    users_path = home.root / USERS_RELATIVE_PATH
    try:
        for path, text in ((marker_path, marker_text), (users_path, users_text)):
            create_private_dir(path.parent)
            write_private_text(path, text)
    except (FsPermissionError, OSError) as error:
        raise ProvisioningError("destination") from error
    return ProvisioningMirror(
        source=root,
        marker_path=marker_path,
        users_path=users_path,
        marker_version=marker_version,
        users_version=users_version,
    )


def _canonical_source(source: Path) -> Path:
    try:
        if not source.is_absolute() or source.is_symlink() or not source.is_dir() or source != source.resolve():
            raise ProvisioningError("source")
    except OSError as error:
        raise ProvisioningError("source") from error
    return source


def _load_artifact(path: Path, stage: str) -> tuple[str, dict[str, object]]:
    """通常fileのUTF-8 JSON objectだけを受理し、原文と解析結果を返す。"""
    try:
        if path.is_symlink() or not path.is_file():
            raise ProvisioningError(stage)
        raw = path.read_bytes()
    except OSError as error:
        raise ProvisioningError(stage) from error
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ProvisioningError(stage)
    try:
        text = raw.decode("utf-8")
        parsed = json.loads(text)
    except (UnicodeDecodeError, ValueError) as error:
        raise ProvisioningError(stage) from error
    if not isinstance(parsed, dict):
        raise ProvisioningError(stage)
    return text, parsed


def _version(artifact: dict[str, object]) -> int:
    version = artifact.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise ProvisioningError("version")
    return version
