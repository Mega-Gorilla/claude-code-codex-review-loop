# SPDX-License-Identifier: Apache-2.0
"""C-09 canary harnessのhermetic test。実Codex・認証・networkは使わない。"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from claude_code_codex_review_loop.identity import create_private_dir
from claude_code_codex_review_loop.runtime import codex_canary as module
from claude_code_codex_review_loop.runtime.codex_canary import (
    CanaryError,
    CanarySandboxCapability,
    build_codex_canary_invocation,
    prepare_codex_canary_home,
    redact_canary_diagnostic,
)

_SANDBOX_READY = CanarySandboxCapability(filesystem_enforced=True, shell_network_disabled=True)


def _paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    private = tmp_path / "private"
    create_private_dir(private)
    workspace = (tmp_path / "checkout").resolve()
    real_repository = (tmp_path / "real-repository").resolve()
    state_root = (tmp_path / "state").resolve()
    for path in (workspace, real_repository, state_root):
        path.mkdir()
    return private.resolve(), workspace, real_repository, state_root


def _home(tmp_path: Path):
    private, workspace, real_repository, state_root = _paths(tmp_path)
    return prepare_codex_canary_home(
        private_root=private,
        name="codex-home",
        workspace_root=workspace,
        protected_roots=(real_repository, state_root),
    )


class TestPrepareCodexCanaryHome:
    def test_writes_single_profile_with_only_workspace_write_and_disabled_network(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        with home.config_path.open("rb") as handle:
            config = tomllib.load(handle)
        profile = config["permissions"]["c09-canary"]
        filesystem = profile["filesystem"]
        assert config["default_permissions"] == "c09-canary"
        assert profile["extends"] == ":workspace"
        assert filesystem[":root"] == "deny"
        assert filesystem[":minimal"] == "read"
        assert filesystem[":tmpdir"] == "deny"
        assert filesystem[":slash_tmp"] == "deny"
        assert filesystem[":workspace_roots"] == {".": "write"}
        assert profile["network"] == {"enabled": False}
        assert filesystem[os.fspath(home.root)] == "deny"
        assert set(home.protected_roots[:-1]) == {
            (tmp_path / "real-repository").resolve(),
            (tmp_path / "state").resolve(),
        }
        raw = home.config_path.read_text(encoding="utf-8")
        assert "sandbox_mode" not in raw
        assert "sandbox_workspace_write" not in raw

    @pytest.mark.parametrize("name", ("", ".", "..", "nested/home", "nested\\home"))
    def test_invalid_home_name_is_rejected(self, tmp_path: Path, name: str) -> None:
        private, workspace, real_repository, state_root = _paths(tmp_path)
        with pytest.raises(CanaryError) as stopped:
            prepare_codex_canary_home(
                private_root=private, name=name, workspace_root=workspace, protected_roots=(real_repository, state_root)
            )
        assert stopped.value.stage == "name"

    @pytest.mark.parametrize("private_mode", ("relative", "public"))
    def test_non_private_root_is_rejected(self, tmp_path: Path, private_mode: str) -> None:
        private, workspace, real_repository, state_root = _paths(tmp_path)
        candidate = Path("relative") if private_mode == "relative" else tmp_path
        with pytest.raises(CanaryError) as stopped:
            prepare_codex_canary_home(
                private_root=candidate, name="codex-home", workspace_root=workspace,
                protected_roots=(real_repository, state_root),
            )
        assert stopped.value.stage == "private_root"

    @pytest.mark.parametrize("protected", ((), ("one",), ("duplicate", "duplicate"), ("workspace", "state")))
    def test_missing_duplicate_or_workspace_overlapping_protected_root_is_rejected(
        self, tmp_path: Path, protected: tuple[str, ...]
    ) -> None:
        private, workspace, real_repository, state_root = _paths(tmp_path)
        choices = {"one": real_repository, "duplicate": real_repository, "workspace": workspace, "state": state_root}
        with pytest.raises(CanaryError) as stopped:
            prepare_codex_canary_home(
                private_root=private, name="codex-home", workspace_root=workspace,
                protected_roots=tuple(choices[item] for item in protected),
            )
        assert stopped.value.stage == "protected_root"

    def test_configuration_write_failure_is_classified(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        private, workspace, real_repository, state_root = _paths(tmp_path)
        monkeypatch.setattr(
            module,
            "write_private_text",
            lambda *args: (_ for _ in ()).throw(module.FsPermissionError("write", "test")),
        )
        with pytest.raises(CanaryError) as stopped:
            prepare_codex_canary_home(
                private_root=private,
                name="codex-home",
                workspace_root=workspace,
                protected_roots=(real_repository, state_root),
            )
        assert stopped.value.stage == "configuration"


class TestCodexCanaryInvocation:
    def test_uses_managed_home_and_fixed_argv_without_legacy_sandbox_flags(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        invocation = build_codex_canary_invocation(
            home=home,
            codex_executable=Path(sys.executable).resolve(),
            reviewer_env={"PATH": "safe"},
            sandbox_capability=_SANDBOX_READY,
        )
        assert invocation.cwd == home.workspace_root
        assert invocation.env == {"PATH": "safe", "CODEX_HOME": os.fspath(home.root)}
        assert invocation.argv == (
            os.fspath(Path(sys.executable).resolve()), "exec", "--ephemeral", "--ignore-rules", "-C",
            os.fspath(home.workspace_root), "-",
        )
        assert "--ignore-user-config" not in invocation.argv
        assert "--sandbox" not in invocation.argv
        assert "-c" not in invocation.argv

    def test_modified_configuration_fails_closed(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        home.config_path.write_text("modified", encoding="utf-8")
        with pytest.raises(CanaryError) as stopped:
            build_codex_canary_invocation(
                home=home,
                codex_executable=Path(sys.executable).resolve(),
                reviewer_env={},
                sandbox_capability=_SANDBOX_READY,
            )
        assert stopped.value.stage == "integrity"

    @pytest.mark.parametrize("kind", ("config_path", "protected_roots"))
    def test_inconsistent_home_metadata_fails_closed(self, tmp_path: Path, kind: str) -> None:
        home = _home(tmp_path)
        if kind == "config_path":
            tampered = replace(home, config_path=home.root / "other.toml")
        else:
            tampered = replace(home, protected_roots=(home.root, *home.protected_roots[:-1]))
        with pytest.raises(CanaryError) as stopped:
            build_codex_canary_invocation(
                home=tampered,
                codex_executable=Path(sys.executable).resolve(),
                reviewer_env={},
                sandbox_capability=_SANDBOX_READY,
            )
        assert stopped.value.stage == "integrity"

    def test_token_environment_is_rejected_without_leaking_its_value(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        token = "sk-" + "x" * 40
        with pytest.raises(CanaryError) as stopped:
            build_codex_canary_invocation(
                home=home,
                codex_executable=Path(sys.executable).resolve(),
                reviewer_env={"OPENAI_API_KEY": token},
                sandbox_capability=_SANDBOX_READY,
            )
        assert stopped.value.stage == "environment"
        assert token not in str(stopped.value)

    @pytest.mark.parametrize("executable", (Path("relative"), Path("missing")))
    def test_invalid_executable_is_rejected(self, tmp_path: Path, executable: Path) -> None:
        home = _home(tmp_path)
        candidate = executable if executable == Path("relative") else (tmp_path / executable).resolve()
        with pytest.raises(CanaryError) as stopped:
            build_codex_canary_invocation(
                home=home,
                codex_executable=candidate,
                reviewer_env={},
                sandbox_capability=_SANDBOX_READY,
            )
        assert stopped.value.stage == "executable"

    @pytest.mark.parametrize(
        "capability",
        (
            CanarySandboxCapability(filesystem_enforced=False, shell_network_disabled=True),
            CanarySandboxCapability(filesystem_enforced=True, shell_network_disabled=False),
        ),
    )
    def test_unverified_sandbox_capability_fails_closed(
        self, tmp_path: Path, capability: CanarySandboxCapability
    ) -> None:
        with pytest.raises(CanaryError) as stopped:
            build_codex_canary_invocation(
                home=_home(tmp_path),
                codex_executable=Path(sys.executable).resolve(),
                reviewer_env={},
                sandbox_capability=capability,
            )
        assert stopped.value.stage == "sandbox_unavailable"


def test_diagnostic_uses_the_shared_redaction_registry() -> None:
    token = "sk-" + "x" * 40
    result = redact_canary_diagnostic("OPENAI_API_KEY=" + token)
    assert token not in result.text
    assert result.hits
