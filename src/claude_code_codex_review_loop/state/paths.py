# SPDX-License-Identifier: Apache-2.0
"""state root配下のlocal state配置（C-07。ADR-0011）。

state rootは**同一マシンの全worktreeから同じ場所を指す**per-user領域で、次を持つ。

```
<state root>/
  runs/<run id>/checkpoint.json      run単位のcheckpoint（atomic replaceで更新）
  locks/<repository digest>/<number>.lock   PR単位のlock（同時runの検出）
```

lockをrun directory配下へ置くと、worktreeごと・run IDごとにlockが分裂して同一PRへの
同時runを検出できない（AC-C10-03の前提を壊す）。そのためlockはrepository / PR単位の
別treeへ置く。

state root自体のpath既定値の解決はC-12（設定解決）であり、本moduleは**注入された
absolute path**だけを扱う。全directoryはC-06のprivate dir契約（作成者限定・排他作成・
読み戻し検証）で作り、pathがstate root配下にあることを実体で検証する。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..identity.errors import IdentityError
from ..identity.fs_permissions import create_private_dir, verify_private_dir
from ..schema.projection import validate_run_id

CHECKPOINT_FILE_NAME: Final = "checkpoint.json"
LOCK_SUFFIX: Final = ".lock"
_RUNS_DIR: Final = "runs"
_LOCKS_DIR: Final = "locks"
# repository slugは`/`を含みpath要素にできないため、digestでdirectory名にする
# （可読性はlock file本体の`repository` fieldが担保する）
_DIGEST_CHARS: Final = 32
_REPOSITORY_PATTERN: Final = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")


class StatePathError(IdentityError):
    """state配置の不正（root・run ID・repository・containment）。"""


@dataclass(frozen=True)
class StatePaths:
    """注入されたstate rootと、その配下の固定subtree。"""

    root: Path
    runs_dir: Path
    locks_dir: Path


def _require_canonical_absolute(path: Path, label: str) -> Path:
    """絶対path・正規化済み（`..`やsymlinkを含まない）であることを要求する。"""
    if not path.is_absolute() or path != path.resolve():
        raise StatePathError("paths", f"{label}は正規化済みの絶対pathでなければならない")
    return path


def prepare_state_root(root: Path) -> StatePaths:
    """state rootと固定subtreeを作成者限定で用意する（既存なら権限を検証する）。

    checkpointもlockも繰り返し使うため、`create_private_dir`の排他作成だけでは足りない。
    存在する場合は「作成者限定であること」を読み戻して確認し、違えば停止する
    （緩い権限のdirectoryをそのまま使う経路を作らない = fail closed）。
    """
    resolved = _require_canonical_absolute(Path(root), "state root")
    paths = StatePaths(
        root=resolved,
        runs_dir=resolved / _RUNS_DIR,
        locks_dir=resolved / _LOCKS_DIR,
    )
    for directory in (paths.root, paths.runs_dir, paths.locks_dir):
        _ensure_private_dir(directory)
    return paths


def _ensure_private_dir(path: Path) -> None:
    """存在しなければ作成し、存在すれば作成者限定であることを検証する。"""
    if path.exists():
        verify_private_dir(path)
        return
    create_private_dir(path)


def run_directory(paths: StatePaths, run_id: str) -> Path:
    """run単位のdirectory（作成者限定で用意する）。

    containmentは**作成より前**に判定する（state root外へdirectoryを作らない）。
    """
    validate_run_id(run_id)
    directory = _require_contained(paths.root, paths.runs_dir / run_id, "run directory")
    _ensure_private_dir(directory)
    return directory


def checkpoint_path(paths: StatePaths, run_id: str) -> Path:
    """run単位のcheckpoint file path（file自体は`state.store`が作る）。"""
    return run_directory(paths, run_id) / CHECKPOINT_FILE_NAME


def repository_digest(repository: str) -> str:
    """repository slugのpath安全な識別子（`/`を含むslugをdirectory名にできない）。"""
    if _REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise StatePathError("paths", "repositoryは`owner/name`形式でなければならない")
    return hashlib.sha256(repository.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def lock_path(paths: StatePaths, repository: str, number: int) -> Path:
    """PR単位のlock file path（同一マシンのどのworktreeからも同じpathになる）。"""
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise StatePathError("paths", "PR / Issue番号は1以上の整数でなければならない")
    directory = _require_contained(
        paths.root, paths.locks_dir / repository_digest(repository), "lock directory"
    )
    _ensure_private_dir(directory)
    return directory / f"{number}{LOCK_SUFFIX}"


def _require_contained(root: Path, path: Path, label: str) -> Path:
    """pathがstate root配下の実体であることを検証する（symlink差し替えの検出）。"""
    resolved = path.resolve() if path.exists() else path.parent.resolve() / path.name
    if not resolved.is_relative_to(root):
        raise StatePathError("paths", f"{label}がstate root配下にない")
    return resolved
