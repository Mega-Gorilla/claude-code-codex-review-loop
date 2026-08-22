# SPDX-License-Identifier: Apache-2.0
"""credential隔離の受入test（AC-C06-03）。実processと実gitで両OS・CIで常時実行する（R-06）。

**Phase 6の検証境界**: ここで実証するのは「reviewer環境からGitHub write credentialへ
到達できない（認証が構造的に成立しない）」ことまで。実GitHubへのmutation試行はnetworkを
要するため行わず、push可能remoteを与えない保証はcheckoutを作るC-09が完成させる
（`file://` remoteへのpushは認証なしで成功するため、Phase 6のnegative controlにならない）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from c06_support.helpers import write_env_dump_script

from claude_code_codex_review_loop.errors import ErrorCategory, classify_gh_failure
from claude_code_codex_review_loop.identity import build_reviewer_env, prepare_reviewer_home
from claude_code_codex_review_loop.policy.redaction import TOKEN_ENV_NAMES
from claude_code_codex_review_loop.process import Completed, SpawnSpec, run_tree

_TIMEOUT_SECONDS = 60.0
_GRACE_SECONDS = 2.0


def _run(argv: Sequence[str], *, env: Mapping[str, str], workdir: Path, tag: str) -> tuple[int, str]:
    """reviewer envのもとで実processを起動し、(exit code, stdout)を返す。"""
    stdout_path = workdir / f"{tag}.out"
    stderr_path = workdir / f"{tag}.err"
    spec = SpawnSpec(
        argv=tuple(argv), cwd=workdir, env=env, stdout_path=stdout_path, stderr_path=stderr_path
    )
    outcome = run_tree(spec, timeout_seconds=_TIMEOUT_SECONDS, grace_seconds=_GRACE_SECONDS)
    assert isinstance(outcome, Completed), f"{tag}がtimeoutした"
    return outcome.exit_code, stdout_path.read_text(encoding="utf-8", errors="replace")


def _origin_path(line: str) -> Path:
    """`git config --show-origin`の`file:<path>`を解く（Windowsでは引用・escapeされる）。"""
    origin = line.split("\t", 1)[0][len("file:") :]
    if origin.startswith('"') and origin.endswith('"'):
        origin = origin[1:-1].replace('\\\\', '\\').replace('\\"', '"')
    return Path(origin)


def _reviewer_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    home = prepare_reviewer_home(tmp_path, "reviewer-home")
    base = dict(os.environ)
    # 実envにtokenが無い環境でも「複写されない」ことを検証できるよう、明示的に混ぜる
    base["GH_TOKEN"] = "ghp_" + "z" * 36
    base["GITHUB_TOKEN"] = "ghp_" + "y" * 36
    return build_reviewer_env(base, home), home.root


class TestEnvReachability:
    """token変数が子processへ到達せず、設定探索先が隔離領域を指す。"""

    def test_child_process_sees_no_token_and_isolated_home(self, tmp_path: Path) -> None:
        env, home_root = _reviewer_env(tmp_path)
        script = write_env_dump_script(tmp_path)
        dump_path = tmp_path / "child-env.json"
        exit_code, _ = _run(
            [sys.executable, str(script), str(dump_path)], env=env, workdir=tmp_path, tag="envdump"
        )
        assert exit_code == 0
        child_env = json.loads(dump_path.read_text(encoding="utf-8"))
        for name in TOKEN_ENV_NAMES:
            assert name not in child_env, name
        assert "z" * 36 not in json.dumps(child_env)
        assert Path(child_env["HOME"]) == home_root
        assert Path(child_env["GH_CONFIG_DIR"]).is_relative_to(home_root)
        assert Path(child_env["XDG_CONFIG_HOME"]).is_relative_to(home_root)
        assert Path(child_env["TMPDIR"]).is_relative_to(home_root)


@pytest.mark.skipif(shutil.which("git") is None, reason="gitが無い環境（CI runnerには常備）")
class TestGitIsolation:
    def test_global_config_write_lands_in_reviewer_home(self, tmp_path: Path) -> None:
        env, home_root = _reviewer_env(tmp_path)
        git = shutil.which("git")
        assert git is not None
        exit_code, _ = _run(
            [git, "config", "--global", "user.name", "cc-review-reviewer"],
            env=env,
            workdir=tmp_path,
            tag="gitwrite",
        )
        assert exit_code == 0
        written = Path(env["GIT_CONFIG_GLOBAL"])
        assert written.is_relative_to(home_root)
        assert "cc-review-reviewer" in written.read_text(encoding="utf-8")

    def test_config_origins_stay_inside_reviewer_home(self, tmp_path: Path) -> None:
        """system / 実HOMEのgit設定（credential helperを含む）が読まれない。"""
        env, home_root = _reviewer_env(tmp_path)
        git = shutil.which("git")
        assert git is not None
        _run(
            [git, "config", "--global", "user.name", "cc-review-reviewer"],
            env=env,
            workdir=tmp_path,
            tag="seed",
        )
        exit_code, stdout = _run(
            [git, "config", "--list", "--show-origin"], env=env, workdir=tmp_path, tag="gitlist"
        )
        assert exit_code == 0
        origins = [_origin_path(line) for line in stdout.splitlines() if line.startswith("file:")]
        assert origins, stdout
        for origin in origins:
            assert origin.is_relative_to(home_root), origin

    def test_credential_helper_is_reset_to_empty(self, tmp_path: Path) -> None:
        """repo-local設定に残るhelperも最高優先度の空値でresetされる。"""
        env, _ = _reviewer_env(tmp_path)
        git = shutil.which("git")
        assert git is not None
        _run([git, "init", "--quiet", "."], env=env, workdir=tmp_path, tag="gitinit")
        _run(
            [git, "config", "--local", "credential.helper", "manager"],
            env=env,
            workdir=tmp_path,
            tag="gitlocalhelper",
        )
        exit_code, stdout = _run(
            [git, "config", "--get-all", "credential.helper"], env=env, workdir=tmp_path, tag="githelper"
        )
        assert exit_code == 0
        entries = stdout.splitlines()
        # env由来の設定は最も高い優先度（listの末尾）へ入る。gitcredentials(7)の
        # 「空値はhelper listをresetする」規約により、それ以前のhelperは無効になる
        assert entries[-1].strip() == "", stdout
        assert "manager" in entries, stdout


@pytest.mark.skipif(shutil.which("gh") is None, reason="ghが無い環境（CI runnerには常備）")
class TestGhAuthUnreachable:
    def test_gh_api_call_fails_with_auth_error(self, tmp_path: Path) -> None:
        """空のGH_CONFIG_DIRとtoken不在で、gh apiは認証error（exit 4）になる。"""
        env, _ = _reviewer_env(tmp_path)
        gh = shutil.which("gh")
        assert gh is not None
        exit_code, _ = _run([gh, "api", "user"], env=env, workdir=tmp_path, tag="ghapi")
        assert exit_code == 4
        assert classify_gh_failure(None, exit_code, retry_after_present=False, ratelimit_remaining_zero=False) is (
            ErrorCategory.AUTH
        )
        # 隔離領域の外へ設定fileを作らない
        assert list(Path(env["GH_CONFIG_DIR"]).iterdir()) == []
