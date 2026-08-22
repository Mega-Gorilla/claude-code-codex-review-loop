# SPDX-License-Identifier: Apache-2.0
"""reviewer環境のcredential隔離（P-015、AC-C06-03）。隔離手段の集約点（R-06）。

本projectのcodeはcredentialを保持せず、認証は認証済みCLIへ委譲する。「渡さない」だけ
ではsubprocessが環境変数やhome配下の設定へ到達できるため、reviewerへ渡すenvを
**除去方式ではなく構築方式**（allowlistした基本変数の複写 + 隔離overlay）で組む。
未知のtoken変数は複写対象に無いため構造的に届かず、builderは秘密値を読むことすらない。

- HOME相当 / `XDG_*` / `GH_CONFIG_DIR`をreviewer専用の一時領域へ差し替える
- git credential helperを無効化し（`GIT_CONFIG_NOSYSTEM` + private global config +
  `GIT_CONFIG_COUNT`によるrepo-local設定のreset）、`GIT_ASKPASS` / `SSH_ASKPASS`が
  対話的に資格情報を取得しないようにする（存在しないpathを指し、spawn失敗でfail closed）
- 二重防御として、結果envのkeyがC-04の`TOKEN_ENV_NAMES`（正本）と一致したらerror

Phase 6の保証境界は「reviewerからGitHub write credentialへ到達できない」ことまでで、
隔離checkoutのremote構成（push可能remoteを与えない）はcheckoutを作るC-09が担う。
env契約の実測根拠と判断はADR-0009を正本とする。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..policy.redaction import TOKEN_ENV_NAMES
from .errors import IdentityError
from .fs_permissions import (
    create_private_dir,
    verify_private_dir,
    verify_private_file,
    write_private_text,
)

# 親envから複写してよい基本変数（credentialを運ばない実行基盤の変数だけ）。
# 両OSの名前の和集合を単一listで扱い、存在しない名前は単に複写されない
COPY_ENV_NAMES: Final[tuple[str, ...]] = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "TZ",
)

_ASKPASS_NAME: Final = "askpass-unavailable"


class CredentialIsolationError(IdentityError):
    """credential隔離の失敗（token変数の混入・reviewer home構成の不正）。"""


@dataclass(frozen=True)
class ReviewerHome:
    """reviewer専用の一時領域。全構成要素は作成者限定権限で作成済み。

    全pathは**canonical（正規化済み・symlink解決済み）な絶対path**でなければならない:
    相対pathをenvへ入れると、Controllerと子processのcwdが異なる場合に、作成・検証した
    領域とは別の場所（子cwd配下）が`HOME` / `GH_CONFIG_DIR` / `GIT_CONFIG_GLOBAL`として
    解決され、隔離契約が破れる。また`..`やsymlinkを含むpathは字句上rootの配下に見えても
    実体はroot外を指し得るため、**正規化した実体で**containmentを判定する。手動構築
    経路でも構築時に検証する。

    askpass_pathは**存在しないpath**であり、gitが資格情報を対話取得しようとした時点で
    spawnに失敗させる（promptで待たずfail closedにする）。
    """

    root: Path
    tmp_dir: Path
    gh_config_dir: Path
    xdg_config_dir: Path
    xdg_cache_dir: Path
    xdg_state_dir: Path
    xdg_data_dir: Path
    git_config_file: Path
    askpass_path: Path

    def __post_init__(self) -> None:
        if not self.root.is_absolute() or self.root != self.root.resolve():
            raise CredentialIsolationError("home", "reviewer homeのrootは正規化済みの絶対pathでなければならない")
        for name, path in self._members():
            # `..`やsymlinkは字句上の包含判定を素通りするため、実体（resolve結果）で判定する
            if not path.is_absolute() or path != path.resolve() or not path.is_relative_to(self.root):
                raise CredentialIsolationError("home", f"{name}がreviewer home配下の正規化済み絶対pathでない")

    def _members(self) -> tuple[tuple[str, Path], ...]:
        """rootを除く構成要素（名前つき）。containment検証と実体検証で共有する。"""
        return (
            ("tmp_dir", self.tmp_dir),
            ("gh_config_dir", self.gh_config_dir),
            ("xdg_config_dir", self.xdg_config_dir),
            ("xdg_cache_dir", self.xdg_cache_dir),
            ("xdg_state_dir", self.xdg_state_dir),
            ("xdg_data_dir", self.xdg_data_dir),
            ("git_config_file", self.git_config_file),
            ("askpass_path", self.askpass_path),
        )

    @property
    def private_dirs(self) -> tuple[Path, ...]:
        """作成者限定であることを要求するdirectory全体。"""
        return (
            self.root,
            self.tmp_dir,
            self.gh_config_dir,
            self.xdg_config_dir,
            self.xdg_cache_dir,
            self.xdg_state_dir,
            self.xdg_data_dir,
        )


def prepare_reviewer_home(parent: Path, name: str) -> ReviewerHome:
    """reviewer専用領域を作成者限定権限で作成する（既存pathはerror = fail closed）。

    parentは絶対path・symlink解決済みへ正規化してから使う（子processのcwdに依存しない
    隔離先を固定するため）。
    """
    if not name or name in {".", ".."} or "/" in name or "\\" in name or os.path.isabs(name):
        raise CredentialIsolationError("home", "reviewer home名は単一のpath要素でなければならない")
    root = Path(parent).resolve() / name
    create_private_dir(root)
    home = ReviewerHome(
        root=root,
        tmp_dir=root / "tmp",
        gh_config_dir=root / "gh-config",
        xdg_config_dir=root / "config",
        xdg_cache_dir=root / "cache",
        xdg_state_dir=root / "state",
        xdg_data_dir=root / "data",
        git_config_file=root / "gitconfig",
        askpass_path=root / _ASKPASS_NAME,
    )
    for directory in (
        home.tmp_dir,
        home.gh_config_dir,
        home.xdg_config_dir,
        home.xdg_cache_dir,
        home.xdg_state_dir,
        home.xdg_data_dir,
    ):
        create_private_dir(directory)
    # global configは実HOMEの`.gitconfig`を参照させないための空file（devnullは書込が
    # 壊れるため実fileを使う。ADR-0009）
    write_private_text(home.git_config_file, "")
    return home


def _verify_home(home: ReviewerHome) -> None:
    """envを配る直前の実体検証（containmentは構築時に済み、ここではfs側の状態を見る）。"""
    for directory in home.private_dirs:
        verify_private_dir(directory)
    verify_private_file(home.git_config_file)
    # symlink自体の存在も検出する（実体の無いsymlinkはexists()がFalseになる）
    if os.path.lexists(home.askpass_path):
        raise CredentialIsolationError("home", "askpass pathが存在する（対話的な資格情報取得が成立し得る）")


def _isolation_overlay(home: ReviewerHome) -> dict[str, str]:
    """隔離のために必ず上書きする変数（両OSの名前を無条件に設定する）。"""
    drive, tail = os.path.splitdrive(str(home.root))
    return {
        # HOME相当の差し替え（gitのHOME解決はHOME -> HOMEDRIVE+HOMEPATH -> USERPROFILE）
        "HOME": str(home.root),
        "USERPROFILE": str(home.root),
        "HOMEDRIVE": drive,
        "HOMEPATH": tail,
        # temp fileをreviewer領域内へ閉じ込める
        "TEMP": str(home.tmp_dir),
        "TMP": str(home.tmp_dir),
        "TMPDIR": str(home.tmp_dir),
        # XDG探索path（gh等のLinux側設定探索の遮断）
        "XDG_CONFIG_HOME": str(home.xdg_config_dir),
        "XDG_CACHE_HOME": str(home.xdg_cache_dir),
        "XDG_STATE_HOME": str(home.xdg_state_dir),
        "XDG_DATA_HOME": str(home.xdg_data_dir),
        # ghは空のconfig dirとtoken不在で認証不能になる（exit 4 = AUTH）
        "GH_CONFIG_DIR": str(home.gh_config_dir),
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
        # gitのsystem / global設定（credential helperを含む）を遮断する
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(home.git_config_file),
        # repo-local設定に残るcredential helperも最高優先度の空値でresetする
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        # 資格情報の対話取得を封じる（promptでhangさせず即失敗させる）
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": str(home.askpass_path),
        "SSH_ASKPASS": str(home.askpass_path),
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
    }


def build_reviewer_env(
    base: Mapping[str, str],
    home: ReviewerHome,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """reviewerへ渡すenvの全体を構築する（C-03のexplicit env契約へそのまま渡せる）。

    baseからは`COPY_ENV_NAMES`だけを複写する（token変数は値を読むことすらない）。
    extraはC-09 / C-12が足す追加変数で、隔離overlayより先に適用するためoverlayを
    上書きできない。結果keyがtoken変数名と一致した場合はerror（二重防御）。

    envを組む前にreviewer homeの全不変条件を再確認する（`ReviewerHome`は手動でも構築
    でき、fs側の状態は構築後にも変わり得るため。隔離先が実在しない・作成者限定でない・
    askpassが存在する状態ではenvを配らない = fail closed）。
    """
    _verify_home(home)
    env: dict[str, str] = {}
    for name in COPY_ENV_NAMES:
        value = base.get(name)
        if value is not None:
            env[name] = value
    if extra is not None:
        env.update(extra)
    env.update(_isolation_overlay(home))
    forbidden = {name.upper() for name in env} & set(TOKEN_ENV_NAMES)
    if forbidden:
        raise CredentialIsolationError("env", f"token変数がreviewer envへ混入した: {sorted(forbidden)}")
    return env
