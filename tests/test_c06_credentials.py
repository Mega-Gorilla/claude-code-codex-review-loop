# SPDX-License-Identifier: Apache-2.0
"""reviewer env構築の受入test（AC-C06-03の純粋部分。P-015）。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from claude_code_codex_review_loop.identity import (
    COPY_ENV_NAMES,
    CredentialIsolationError,
    ReviewerHome,
    build_reviewer_env,
    prepare_reviewer_home,
)
from claude_code_codex_review_loop.policy import redact
from claude_code_codex_review_loop.policy.redaction import TOKEN_ENV_NAMES

_BASE_WITH_SECRETS = {
    "PATH": "/usr/bin",
    "LANG": "ja_JP.UTF-8",
    "GH_TOKEN": "ghp_" + "a" * 36,
    "ANTHROPIC_API_KEY": "sk-ant-" + "b" * 24,
    "AWS_SESSION_TOKEN": "c" * 40,
    "HOME": "/home/real-user",
    "GH_CONFIG_DIR": "/home/real-user/.config/gh",
    "UNRELATED_VAR": "value",
}


def _home(tmp_path: Path) -> ReviewerHome:
    return prepare_reviewer_home(tmp_path, "reviewer-1")


class TestPrepareReviewerHome:
    def test_creates_private_structure(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        assert home.root.is_dir()
        for directory in (
            home.tmp_dir,
            home.gh_config_dir,
            home.xdg_config_dir,
            home.xdg_cache_dir,
            home.xdg_state_dir,
            home.xdg_data_dir,
        ):
            assert directory.is_dir()
        assert home.git_config_file.is_file() and home.git_config_file.read_text(encoding="utf-8") == ""
        # askpassは「存在しないpath」であることが仕様（spawn失敗でfail closedにする）
        assert not home.askpass_path.exists()

    def test_existing_root_is_rejected(self, tmp_path: Path) -> None:
        """事前に存在するdirectoryの権限を信用せずerrorにする（fail closed）。"""
        (tmp_path / "reviewer-1").mkdir()
        with pytest.raises(Exception) as excinfo:
            prepare_reviewer_home(tmp_path, "reviewer-1")
        assert "reviewer-1" in str(excinfo.value)

    @pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b"])
    def test_invalid_name_is_rejected(self, tmp_path: Path, name: str) -> None:
        with pytest.raises(CredentialIsolationError) as excinfo:
            prepare_reviewer_home(tmp_path, name)
        assert excinfo.value.stage == "home"

    def test_absolute_name_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialIsolationError):
            prepare_reviewer_home(tmp_path, str(tmp_path / "elsewhere"))


class TestBuildReviewerEnv:
    """AC-C06-03: token変数はreviewer envへ到達せず、設定探索先は隔離領域を指す。"""

    def test_only_allowlisted_base_vars_are_copied(self, tmp_path: Path) -> None:
        env = build_reviewer_env(_BASE_WITH_SECRETS, _home(tmp_path))
        assert env["PATH"] == "/usr/bin" and env["LANG"] == "ja_JP.UTF-8"
        assert "UNRELATED_VAR" not in env
        for name in TOKEN_ENV_NAMES:
            assert name not in env

    def test_absent_base_vars_are_simply_missing(self, tmp_path: Path) -> None:
        env = build_reviewer_env({"PATH": "/usr/bin"}, _home(tmp_path))
        assert "LANG" not in env and "SYSTEMROOT" not in env

    def test_isolation_overlay_points_into_reviewer_home(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        env = build_reviewer_env(_BASE_WITH_SECRETS, home)
        assert env["HOME"] == str(home.root) and env["USERPROFILE"] == str(home.root)
        assert Path(env["HOMEDRIVE"] + env["HOMEPATH"]) == home.root
        assert env["TEMP"] == env["TMP"] == env["TMPDIR"] == str(home.tmp_dir)
        assert env["XDG_CONFIG_HOME"] == str(home.xdg_config_dir)
        assert env["XDG_CACHE_HOME"] == str(home.xdg_cache_dir)
        assert env["XDG_STATE_HOME"] == str(home.xdg_state_dir)
        assert env["XDG_DATA_HOME"] == str(home.xdg_data_dir)
        assert env["GH_CONFIG_DIR"] == str(home.gh_config_dir)
        assert env["GH_PROMPT_DISABLED"] == "1" and env["GH_NO_UPDATE_NOTIFIER"] == "1"

    def test_git_hardening_variables(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        env = build_reviewer_env({}, home)
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == str(home.git_config_file)
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ASKPASS"] == env["SSH_ASKPASS"] == str(home.askpass_path)
        assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
        # repo-local設定に残るhelperも最高優先度の空値でresetする
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_0"] == ""

    def test_extra_cannot_override_isolation_overlay(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        env = build_reviewer_env({}, home, extra={"HOME": "/home/real-user", "CODEX_PROFILE": "readonly"})
        assert env["HOME"] == str(home.root)
        assert env["CODEX_PROFILE"] == "readonly"

    @pytest.mark.parametrize("name", ["GH_TOKEN", "gh_token", "ANTHROPIC_API_KEY"])
    def test_token_name_in_extra_is_rejected(self, tmp_path: Path, name: str) -> None:
        """二重防御: allowlist構築でも届かないが、extra経由の混入をerrorにする。"""
        with pytest.raises(CredentialIsolationError) as excinfo:
            build_reviewer_env({}, _home(tmp_path), extra={name: "secret"})
        assert excinfo.value.stage == "env"


class TestTokenNameRegistry:
    def test_copy_allowlist_contains_no_token_names(self) -> None:
        assert not set(COPY_ENV_NAMES) & set(TOKEN_ENV_NAMES)

    def test_token_names_are_redacted_by_c04(self) -> None:
        """正本の集合がC-04 redactionと一致していることを常設検証する（drift防止）。"""
        for name in TOKEN_ENV_NAMES:
            result = redact(f"{name}=super-secret-value")
            assert "super-secret-value" not in result.text, name
            assert result.hits and result.hits[0].name == "env-assignment"


class TestAbsolutePathContainment:
    """相対pathは子processのcwd次第で別の場所を指すため、構築時に絶対path化・検証する。"""

    def test_relative_parent_is_resolved_to_absolute(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        home = prepare_reviewer_home(Path("."), "reviewer-1")
        assert home.root.is_absolute()
        assert home.root == (tmp_path.resolve() / "reviewer-1")
        env = build_reviewer_env({}, home)
        for name in ("HOME", "GH_CONFIG_DIR", "GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME", "TMPDIR"):
            assert Path(env[name]).is_absolute(), name

    def test_relative_home_cannot_be_constructed(self, tmp_path: Path) -> None:
        with pytest.raises(CredentialIsolationError) as excinfo:
            ReviewerHome(
                root=Path("relative-root"),
                tmp_dir=Path("relative-root/tmp"),
                gh_config_dir=Path("relative-root/gh-config"),
                xdg_config_dir=Path("relative-root/config"),
                xdg_cache_dir=Path("relative-root/cache"),
                xdg_state_dir=Path("relative-root/state"),
                xdg_data_dir=Path("relative-root/data"),
                git_config_file=Path("relative-root/gitconfig"),
                askpass_path=Path("relative-root/askpass-unavailable"),
            )
        assert excinfo.value.stage == "home"

    def test_member_outside_root_is_rejected(self, tmp_path: Path) -> None:
        """手動構築でも構成要素はroot配下に限る（隔離先のすり替えを防ぐ）。"""
        root = tmp_path / "reviewer-1"
        with pytest.raises(CredentialIsolationError) as excinfo:
            ReviewerHome(
                root=root,
                tmp_dir=root / "tmp",
                gh_config_dir=tmp_path / "outside-gh",
                xdg_config_dir=root / "config",
                xdg_cache_dir=root / "cache",
                xdg_state_dir=root / "state",
                xdg_data_dir=root / "data",
                git_config_file=root / "gitconfig",
                askpass_path=root / "askpass-unavailable",
            )
        assert "gh_config_dir" in excinfo.value.detail

    def test_env_build_requires_existing_private_home(self, tmp_path: Path) -> None:
        """隔離先が実在しない状態ではenvを配らない（fail closed）。"""
        home = prepare_reviewer_home(tmp_path, "reviewer-1")
        shutil.rmtree(home.root)
        with pytest.raises(Exception) as excinfo:
            build_reviewer_env({}, home)
        assert "verify" in str(excinfo.value)
