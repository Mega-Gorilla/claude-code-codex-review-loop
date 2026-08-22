# SPDX-License-Identifier: Apache-2.0
"""OS別のfile権限（artifact / temp fileの作成者限定アクセス。P-009、AC-C06-05）。

artifactにはprivate repositoryのdiffとreview内容が含まれるため、POSIXのmode指定だけ
ではWindowsで保護されない。本moduleがOS共通の契約を定義し、backend（mode_posix /
acl_windows）が実装する。OS分岐は本module末尾のconditional import 1箇所に閉じる
（C-03のspawn facadeと同じ構造）。

契約上の要点:

- 作成は**排他**（既存pathはerror）。事前に存在するdirectoryの権限を信用しない
  （攻撃者が緩い権限で先に作ったdirectoryをそのまま使う経路を作らない = fail closed）
- 作成後に実効権限を**読み戻して検証**する。検証に失敗した場合はsilentに続行せず
  `FsPermissionError`とする
- 「作成者のみ」の定義: POSIXは`0o700` / `0o600`かつ所有者が自分、Windowsは現userの
  単一許可ACEのみ（継承遮断）。administratorのtake ownershipで到達可能である点は
  POSIXのrootと同格の限界として受け入れる（ADR-0009）
"""

from __future__ import annotations

import sys
from pathlib import Path

from .errors import IdentityError


class FsPermissionError(IdentityError):
    """file権限操作の失敗。os_errorはOSのerror code（無い場合はNone）。"""

    def __init__(self, stage: str, detail: str, os_error: int | None = None) -> None:
        super().__init__(stage, detail)
        self.os_error = os_error


if sys.platform == "win32":  # pragma: no cover - OS dispatch(単一分岐点。各backendは自OSのCIで検証する)
    from . import acl_windows

    _backend = acl_windows
else:  # pragma: no cover - OS dispatch(単一分岐点。各backendは自OSのCIで検証する)
    from . import mode_posix

    _backend = mode_posix


def create_private_dir(path: Path) -> None:
    """作成者のみがアクセスできるdirectoryを排他的に作成し、実効権限を検証する。"""
    _backend.create_private_dir(path)


def write_private_text(path: Path, text: str) -> None:
    """private directory内へ、作成者のみが読書きできるfileを排他的に作成する。"""
    _backend.write_private_text(path, text)


def verify_private_dir(path: Path) -> None:
    """既存directoryの実効権限が作成者限定であることを検証する（違反はerror）。"""
    _backend.verify_private_dir(path)


def verify_private_file(path: Path) -> None:
    """既存fileの実効権限が作成者限定であることを検証する（違反はerror）。"""
    _backend.verify_private_file(path)
