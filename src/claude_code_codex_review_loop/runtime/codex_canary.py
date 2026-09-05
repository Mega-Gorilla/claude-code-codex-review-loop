# SPDX-License-Identifier: Apache-2.0
"""C-09の認証なしCodex canary用の管理下CODEX_HOMEと起動契約。

これはnative reviewer adapterではない。実Codexの起動、認証材料の複製、API呼出、
GitHub mutationを行わず、手動canaryへ渡す設定とargvだけを安全側で組み立てる。
専用homeのconfigはpermission profileを唯一のsandbox設定源とし、旧CLI sandbox
flagやuser/project execpolicyへ依存しない。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..identity.fs_permissions import (
    FsPermissionError,
    create_private_dir,
    verify_private_dir,
    verify_private_file,
    write_private_text,
)
from ..policy.permission_profile import ensure_argv_allowed
from ..policy.redaction import TOKEN_ENV_NAMES, RedactionResult, redact

_PROFILE_NAME: Final = "c09-canary"
_CONFIG_NAME: Final = "config.toml"


class CanaryError(Exception):
    """公開してよい固定stageだけを持つcanary構成エラー。"""

    def __init__(self, stage: str) -> None:
        super().__init__(f"codex_canary_error: {stage}")
        self.stage = stage


@dataclass(frozen=True)
class CodexCanaryHome:
    """1 checkoutへbindされた、credentialを含まないprivateなCODEX_HOME。"""

    root: Path
    config_path: Path
    workspace_root: Path
    protected_roots: tuple[Path, ...]
    configuration_digest: str


@dataclass(frozen=True)
class CodexCanaryInvocation:
    """将来のprocess facadeへ渡す固定引数・環境・cwd。promptはstdinでのみ与える。"""

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path


@dataclass(frozen=True)
class CanarySandboxCapability:
    """実測したpermission profileの強制可否。未確認・片側欠落は起動不可である。"""

    filesystem_enforced: bool
    shell_network_disabled: bool


def prepare_codex_canary_home(
    *,
    private_root: Path,
    name: str,
    workspace_root: Path,
    protected_roots: Iterable[Path],
) -> CodexCanaryHome:
    """private root配下へ専用homeとpermission profileを排他的に作る。

    `protected_roots`には少なくとも実repositoryとstate rootを渡す。workspaceと重なる
    rootは拒否するため、誤って実repositoryをwrite許可対象にする構成は成立しない。
    """
    _validate_name(name)
    _validate_private_root(private_root)
    workspace = _canonical_directory(workspace_root, "workspace")
    protected = _canonical_protected_roots(protected_roots, workspace)
    root = private_root / name
    try:
        create_private_dir(root)
        config_path = root / _CONFIG_NAME
        # rootは末尾へ固定する。後段の再検証で「外部保護root」とCODEX_HOMEを区別し、
        # config出力だけは外部rootのpath順を決定的に保つ。
        all_protected = (*protected, root)
        configuration = _render_configuration(workspace, all_protected)
        write_private_text(config_path, configuration)
    except (FsPermissionError, OSError) as error:
        raise CanaryError("configuration") from error
    return CodexCanaryHome(
        root=root,
        config_path=config_path,
        workspace_root=workspace,
        protected_roots=all_protected,
        configuration_digest=hashlib.sha256(configuration.encode("utf-8")).hexdigest(),
    )


def build_codex_canary_invocation(
    *,
    home: CodexCanaryHome,
    codex_executable: Path,
    reviewer_env: Mapping[str, str],
    sandbox_capability: CanarySandboxCapability,
) -> CodexCanaryInvocation:
    """専用configを読む最小の`codex exec` argvを構築する。

    configが専用homeのpolicy sourceであるためignore-user-configは使わない。一方で
    repository由来のexecpolicyはignore-rulesで遮断する。任意argvや`-c`上書きの入口は
    このAPIに持たせず、promptは次段のprocess facadeがstdinで渡す。
    """
    if not sandbox_capability.filesystem_enforced or not sandbox_capability.shell_network_disabled:
        raise CanaryError("sandbox_unavailable")
    _verify_home(home)
    executable = _canonical_file(codex_executable, "executable")
    env = _build_environment(reviewer_env, home)
    argv = (
        os.fspath(executable),
        "exec",
        "--ephemeral",
        "--ignore-rules",
        "-C",
        os.fspath(home.workspace_root),
        "-",
    )
    ensure_argv_allowed(argv)
    return CodexCanaryInvocation(argv=argv, env=env, cwd=home.workspace_root)


def redact_canary_diagnostic(text: str) -> RedactionResult:
    """native diagnosticを公開面へ渡す前の共通redaction入口。"""
    return redact(text)


def _validate_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\\" in name or os.path.isabs(name):
        raise CanaryError("name")


def _validate_private_root(path: Path) -> None:
    try:
        root = _canonical_directory(path, "private_root")
        verify_private_dir(root)
    except (CanaryError, FsPermissionError) as error:
        raise CanaryError("private_root") from error


def _canonical_directory(path: Path, stage: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate != candidate.resolve() or not candidate.is_dir():
        raise CanaryError(stage)
    return candidate


def _canonical_file(path: Path, stage: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate != candidate.resolve() or not candidate.is_file():
        raise CanaryError(stage)
    return candidate


def _canonical_protected_roots(protected_roots: Iterable[Path], workspace: Path) -> tuple[Path, ...]:
    protected = tuple(_canonical_directory(path, "protected_root") for path in protected_roots)
    if len(protected) < 2 or len(set(protected)) != len(protected):
        raise CanaryError("protected_root")
    for path in protected:
        if path.is_relative_to(workspace) or workspace.is_relative_to(path):
            raise CanaryError("protected_root")
    return tuple(sorted(protected, key=os.fspath))


def _render_configuration(workspace: Path, protected_roots: tuple[Path, ...]) -> str:
    lines = [
        f"default_permissions = {_toml_string(_PROFILE_NAME)}",
        "",
        f"[permissions.{_PROFILE_NAME}]",
        'extends = ":workspace"',
        "",
        f"[permissions.{_PROFILE_NAME}.filesystem]",
        '":root" = "deny"',
        '":minimal" = "read"',
        '":tmpdir" = "deny"',
        '":slash_tmp" = "deny"',
    ]
    lines.extend(f"{_toml_string(os.fspath(path))} = \"deny\"" for path in protected_roots)
    lines.extend(
        (
            "",
            f"[permissions.{_PROFILE_NAME}.filesystem.\":workspace_roots\"]",
            '"." = "write"',
            "",
            f"[permissions.{_PROFILE_NAME}.network]",
            "enabled = false",
            "",
        )
    )
    return "\n".join(lines)


def _toml_string(value: str) -> str:
    """JSON stringはTOML basic stringの有効なsubsetである。"""
    return json.dumps(value, ensure_ascii=False)


def _verify_home(home: CodexCanaryHome) -> None:
    try:
        _canonical_directory(home.root, "integrity")
        if home.config_path != home.root / _CONFIG_NAME:
            raise CanaryError("integrity")
        _canonical_directory(home.workspace_root, "integrity")
        _canonical_protected_roots(home.protected_roots[:-1], home.workspace_root)
        if home.protected_roots[-1:] != (home.root,):
            raise CanaryError("integrity")
        verify_private_dir(home.root)
        verify_private_file(home.config_path)
        current = home.config_path.read_bytes()
    except (CanaryError, FsPermissionError, OSError) as error:
        raise CanaryError("integrity") from error
    if hashlib.sha256(current).hexdigest() != home.configuration_digest:
        raise CanaryError("integrity")


def _build_environment(reviewer_env: Mapping[str, str], home: CodexCanaryHome) -> dict[str, str]:
    env = dict(reviewer_env)
    forbidden = {name.upper() for name in env} & set(TOKEN_ENV_NAMES)
    if forbidden:
        raise CanaryError("environment")
    env["CODEX_HOME"] = os.fspath(home.root)
    return env
