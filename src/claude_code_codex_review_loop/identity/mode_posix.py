# SPDX-License-Identifier: Apache-2.0
"""C-06のPOSIX backend（mode bitによるfile権限。AC-C06-05）。

directoryは`0o700`、fileは`0o600`で作成し、作成後にstatで実効modeと所有者を検証する。
umaskは権限を減らす方向にしか働かないが、減った場合も「作成者のみ」の契約から外れる
（自分自身が読めなくなる）ため、作成直後にchmodで期待値へ揃えてから検証する。
"""

from __future__ import annotations

import stat
import sys

if sys.platform == "win32":  # pragma: no cover - POSIX専用module(Windows側CIはreport対象からomitする)
    raise ImportError("mode_posixはPOSIX専用moduleである")

import os
from pathlib import Path

from .fs_permissions import FsPermissionError, write_all

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def _check(path: Path, expected_mode: int, expect_dir: bool) -> None:
    try:
        info = os.stat(path)
    except OSError as error:
        raise FsPermissionError("verify", f"statに失敗した: {path}", error.errno) from error
    if stat.S_ISDIR(info.st_mode) is not expect_dir:
        raise FsPermissionError("verify", f"対象の種別が期待と異なる: {path}")
    if stat.S_IMODE(info.st_mode) != expected_mode:
        raise FsPermissionError("verify", f"modeが作成者限定でない: {path}")
    if info.st_uid != os.getuid():
        raise FsPermissionError("verify", f"所有者が現userでない: {path}")


def create_private_dir(path: Path) -> None:
    """`0o700`のdirectoryを排他的に作成する（既存pathはerror）。"""
    try:
        os.mkdir(path, _DIR_MODE)
    except OSError as error:
        raise FsPermissionError("create_dir", f"directoryを作成できない: {path}", error.errno) from error
    try:
        os.chmod(path, _DIR_MODE)
    except OSError as error:  # pragma: no cover - 直後のchmod失敗はmkdir成功後には実質起きない
        raise FsPermissionError("create_dir", f"modeを設定できない: {path}", error.errno) from error
    _check(path, _DIR_MODE, expect_dir=True)


def write_private_text(path: Path, text: str) -> None:
    """`0o600`のfileを排他的に作成してtextを書く（既存pathはerror）。"""
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _FILE_MODE)
    except OSError as error:
        raise FsPermissionError("create_file", f"fileを作成できない: {path}", error.errno) from error
    try:
        os.fchmod(descriptor, _FILE_MODE)
        write_all(descriptor, text.encode("utf-8"), path)
        # 作成も置換も耐久性を持たせる（checkpointのatomic replaceの前提）
        os.fsync(descriptor)
    except BaseException:
        # 書き切れなかったfileを残さない（短い内容がreplaceされるのを防ぐ）
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    _check(path, _FILE_MODE, expect_dir=False)


def sync_directory(path: Path) -> None:
    """directory entryの更新をdiskへ確定させる（`os.replace`後の耐久性）。"""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as error:  # pragma: no cover - 直前に検証済みのprivate dirで実質起きない
        raise FsPermissionError("sync", f"directoryを開けない: {path}", error.errno) from error
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_private_dir(path: Path) -> None:
    """既存directoryが`0o700`かつ現user所有であることを検証する。"""
    _check(path, _DIR_MODE, expect_dir=True)


def verify_private_file(path: Path) -> None:
    """既存fileが`0o600`かつ現user所有であることを検証する。"""
    _check(path, _FILE_MODE, expect_dir=False)
