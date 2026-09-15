# SPDX-License-Identifier: Apache-2.0
"""C-09 reviewer認証のauth-setup（ADR-0031 決定3）。固定homeへの一度だけの登録を固定順序で行う。

ユーザーが素の`codex login`を実行する形は採らない。生成configが無い状態ではfile保存へ進み、
禁止している`auth.json`が作られ得るためである。順序:

1. 固定homeを取得する（lock → 配置の検証 → markerが検証できる前回entryだけ削除。`reviewer_home`）
2. 管理下config（`cli_auth_credentials_store = "keyring"`）を生成する
3. `codex doctor --json`で`auth storage mode: Keyring`を確認する（`File` / `Auto`なら停止）
4. `CODEX_HOME`を明示したenvで**productが**`codex login --device-auth`を起動し、ユーザーが認証する
5. `auth.json`が無いこと、`auth.credentials`が`ok`かつ`Keyring`であることを確認し、markerを置く

途中で失敗した場合はcredentialを使用せず停止する。`auth.json`が作られていれば削除して`file_credentials`
として報告する。device codeの表示はCLI層（C-15）がstdout fileを読んで行う。本moduleはtokenを
argv / env / logへ出さない。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from ..identity.fs_permissions import FsPermissionError, verify_private_dir, write_private_text
from ..policy.permission_profile import ensure_argv_allowed
from ..policy.redaction import TOKEN_ENV_NAMES
from ..process import Completed, SpawnError, SpawnSpec, StopError, run_tree
from .codex_canary import render_auth_setup_configuration
from .codex_launch import REQUIRED_AUTH_STORAGE, SUPPORTED_CODEX_VERSIONS
from .codex_preflight import AuthState, auth_state_from_check
from .reviewer_home import HomeError, acquire_fixed_home, create_home_dir, write_home_marker

_CONFIG_NAME: Final = "config.toml"
_AUTH_FILE_NAME: Final = "auth.json"
_MAX_DOCTOR_BYTES: Final = 1_048_576


class AuthSetupError(Exception):
    """固定stageだけを公開するauth-setupの失敗。path・native出力・tokenを含めない。"""

    def __init__(self, stage: str) -> None:
        super().__init__(f"reviewer_auth_setup_error: {stage}")
        self.stage = stage


class LoginPort(Protocol):
    """`codex login`の起動。本実装は`SpawnLogin`（C-03）。testはfakeを渡す。"""

    def login(self, *, env: Mapping[str, str], home: Path, evidence_root: Path) -> int: ...


@dataclass(frozen=True)
class SpawnLogin:
    """C-03で`codex login --device-auth`を固定argvで起動する。stdout / stderrはevidence root配下へ書く。"""

    codex_executable: Path
    timeout_seconds: float
    grace_seconds: float

    def login(self, *, env: Mapping[str, str], home: Path, evidence_root: Path) -> int:
        argv = (str(self.codex_executable), "login", "--device-auth")
        ensure_argv_allowed(argv)
        spec = SpawnSpec(
            argv=argv,
            cwd=home,
            env=env,
            stdout_path=evidence_root / "login.stdout",
            stderr_path=evidence_root / "login.stderr",
        )
        try:
            outcome = run_tree(spec, timeout_seconds=self.timeout_seconds, grace_seconds=self.grace_seconds)
        except (SpawnError, StopError) as error:
            raise AuthSetupError("login_spawn") from error
        if not isinstance(outcome, Completed):
            raise AuthSetupError("login_timeout")
        return outcome.exit_code


@dataclass(frozen=True)
class AuthSetupResult:
    """登録の記録。tokenやaccount識別子は持たない。"""

    home: Path
    marker: Path
    codex_version: str
    storage_mode: str


def run_reviewer_auth_setup(
    *,
    parent: Path,
    name: str,
    disjoint_from: tuple[Path, ...],
    codex_executable: Path,
    reviewer_env: Mapping[str, str],
    evidence_root: Path,
    login: LoginPort,
    timeout_seconds: float,
    grace_seconds: float,
) -> AuthSetupResult:
    """固定順序でauth-setupを実行する。失敗はcredentialを使用せず停止する。"""
    if _platform() != "win32":
        raise AuthSetupError("auth_platform")
    executable = _canonical_executable(codex_executable)
    root = _canonical_evidence_root(evidence_root)
    env = _environment(reviewer_env)
    try:
        lease = acquire_fixed_home(parent=parent, name=name, disjoint_from=disjoint_from)
    except HomeError as error:
        raise AuthSetupError(f"home:{error.stage}") from error
    try:
        return _setup_in_home(lease.home, executable, env, root, login, timeout_seconds, grace_seconds)
    finally:
        lease.release()


def _setup_in_home(
    home: Path,
    executable: str,
    env: dict[str, str],
    evidence_root: Path,
    login: LoginPort,
    timeout_seconds: float,
    grace_seconds: float,
) -> AuthSetupResult:
    try:
        create_home_dir(home)
    except HomeError as error:
        raise AuthSetupError(f"home:{error.stage}") from error
    try:
        write_private_text(home / _CONFIG_NAME, render_auth_setup_configuration())
    except (FsPermissionError, OSError) as error:
        raise AuthSetupError("configuration") from error
    env["CODEX_HOME"] = str(home)
    version = _read_version(executable, env, home, evidence_root, "version", timeout_seconds, grace_seconds)
    if version not in SUPPORTED_CODEX_VERSIONS:
        raise AuthSetupError("auth_version")
    before = _read_auth_state(executable, env, home, evidence_root, "doctor_before", timeout_seconds, grace_seconds)
    if before.storage_mode != REQUIRED_AUTH_STORAGE:
        raise AuthSetupError("storage_mode")
    if _auth_file_present(home):
        raise AuthSetupError("file_credentials")
    exit_code = login.login(env=env, home=home, evidence_root=evidence_root)
    if _auth_file_present(home):
        _remove_auth_file(home)
        raise AuthSetupError("file_credentials")
    if exit_code != 0:
        raise AuthSetupError("login")
    after = _read_auth_state(executable, env, home, evidence_root, "doctor_after", timeout_seconds, grace_seconds)
    if after.status != "ok" or after.storage_mode != REQUIRED_AUTH_STORAGE:
        raise AuthSetupError("not_registered")
    try:
        marker = write_home_marker(home)
    except HomeError as error:
        raise AuthSetupError(f"home:{error.stage}") from error
    return AuthSetupResult(home=home, marker=marker, codex_version=version, storage_mode=after.storage_mode)


def _environment(reviewer_env: Mapping[str, str]) -> dict[str, str]:
    env = dict(reviewer_env)
    if {name.upper() for name in env} & set(TOKEN_ENV_NAMES):
        raise AuthSetupError("environment")
    return env


def _canonical_executable(path: Path) -> str:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate != candidate.resolve() or not candidate.is_file():
        raise AuthSetupError("executable")
    return str(candidate)


def _canonical_evidence_root(path: Path) -> Path:
    candidate = Path(path)
    try:
        if not candidate.is_absolute() or candidate != candidate.resolve() or not candidate.is_dir():
            raise AuthSetupError("evidence_root")
        verify_private_dir(candidate)
    except (FsPermissionError, OSError) as error:
        raise AuthSetupError("evidence_root") from error
    return candidate


def _output(
    argv: tuple[str, ...],
    env: Mapping[str, str],
    cwd: Path,
    evidence_root: Path,
    stage: str,
    timeout_seconds: float,
    grace_seconds: float,
) -> tuple[int, str]:
    ensure_argv_allowed(argv)
    stdout_path = evidence_root / f"{stage}.stdout"
    spec = SpawnSpec(
        argv=argv, cwd=cwd, env=env, stdout_path=stdout_path, stderr_path=evidence_root / f"{stage}.stderr"
    )
    try:
        outcome = run_tree(spec, timeout_seconds=timeout_seconds, grace_seconds=grace_seconds)
    except (SpawnError, StopError) as error:
        raise AuthSetupError(stage) from error
    if not isinstance(outcome, Completed):
        raise AuthSetupError(stage)
    try:
        with stdout_path.open("rb") as handle:
            text = handle.read(_MAX_DOCTOR_BYTES).decode("utf-8", errors="replace")
    except OSError as error:
        raise AuthSetupError(stage) from error
    return outcome.exit_code, text


def _read_version(
    executable: str,
    env: Mapping[str, str],
    home: Path,
    evidence_root: Path,
    stage: str,
    timeout_seconds: float,
    grace_seconds: float,
) -> str:
    argv = (executable, "--version")
    exit_code, text = _output(argv, env, home, evidence_root, stage, timeout_seconds, grace_seconds)
    version = text.strip()
    if exit_code != 0 or not version or "\n" in version:
        raise AuthSetupError(stage)
    return version


def _read_auth_state(
    executable: str,
    env: Mapping[str, str],
    home: Path,
    evidence_root: Path,
    stage: str,
    timeout_seconds: float,
    grace_seconds: float,
) -> AuthState:
    """doctorの終了codeは認証欠如でfailになるため見ず、`auth.credentials` checkだけを読む。"""
    _, text = _output((executable, "doctor", "--json"), env, home, evidence_root, stage, timeout_seconds, grace_seconds)
    try:
        report = json.loads(text)
        checks = report["checks"]
        home_seen = str(checks["config.load"]["details"]["CODEX_HOME"])
        state = auth_state_from_check(checks.get("auth.credentials"))
    except (ValueError, KeyError, TypeError) as error:
        raise AuthSetupError(f"{stage}_output") from error
    if home_seen != str(home):
        raise AuthSetupError(f"{stage}_home")
    return state


def _auth_file_present(home: Path) -> bool:
    target = home / _AUTH_FILE_NAME
    return target.is_symlink() or target.exists()


def _remove_auth_file(home: Path) -> None:
    """file保存されたcredentialを残さない（best effort。停止理由は`file_credentials`のまま）。"""
    try:
        (home / _AUTH_FILE_NAME).unlink()
    except OSError:
        pass


def _platform() -> str:
    return sys.platform
