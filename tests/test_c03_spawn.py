# SPDX-License-Identifier: Apache-2.0
"""C-03 spawn契約の受入test。

explicit env（継承なし）/ cwd / stdout・stderrのfile redirect / stdin=DEVNULL /
argv検証（P-014）/ spawn失敗時に残骸（process・file handle）を残さないことを検証する。
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest
from c03_support.helpers import WAIT_LIMIT_SECONDS, child_env

from claude_code_codex_review_loop.process import Completed, SpawnError, SpawnSpec, run_tree


def _python_spec(
    tmp_path: Path,
    code: str,
    *,
    extra_env: dict[str, str] | None = None,
    stdout: Path | None = None,
    stderr: Path | None = None,
    cwd: Path | None = None,
) -> SpawnSpec:
    return SpawnSpec(
        argv=(sys.executable, "-c", code),
        cwd=cwd if cwd is not None else tmp_path,
        env={**child_env(), **(extra_env or {})},
        stdout_path=stdout,
        stderr_path=stderr,
    )


def test_child_sees_only_explicit_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CC_REVIEW_PARENT_ONLY", "leaked")
    out = tmp_path / "env.json"
    spec = _python_spec(
        tmp_path,
        "import json, os, sys; sys.stdout.write(json.dumps(dict(os.environ)))",
        extra_env={"CC_REVIEW_MARKER": "yes"},
        stdout=out,
    )
    result = run_tree(spec, timeout_seconds=WAIT_LIMIT_SECONDS, grace_seconds=1.0)
    assert result == Completed(exit_code=0)
    child_environ = json.loads(out.read_text(encoding="utf-8"))
    assert child_environ["CC_REVIEW_MARKER"] == "yes"
    assert "CC_REVIEW_PARENT_ONLY" not in child_environ
    # patch=["subprocess"]のcoverage起動も、explicit envの子へは注入されない
    assert "COVERAGE_PROCESS_CONFIG" not in child_environ


def test_cwd_is_applied(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    out = tmp_path / "cwd.txt"
    spec = _python_spec(tmp_path, "import os, sys; sys.stdout.write(os.getcwd())", stdout=out, cwd=workdir)
    result = run_tree(spec, timeout_seconds=WAIT_LIMIT_SECONDS, grace_seconds=1.0)
    assert result == Completed(exit_code=0)
    assert Path(out.read_text(encoding="utf-8")).resolve() == workdir.resolve()


def test_stdout_and_stderr_redirect_to_separate_files(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    err = tmp_path / "err.txt"
    spec = _python_spec(
        tmp_path,
        "import sys; sys.stdout.write('to-stdout'); sys.stderr.write('to-stderr')",
        stdout=out,
        stderr=err,
    )
    result = run_tree(spec, timeout_seconds=WAIT_LIMIT_SECONDS, grace_seconds=1.0)
    assert result == Completed(exit_code=0)
    assert out.read_text(encoding="utf-8") == "to-stdout"
    assert err.read_text(encoding="utf-8") == "to-stderr"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIXのfile permission検証")
def test_redirect_files_are_created_owner_only(tmp_path: Path) -> None:
    out = tmp_path / "owner-only.txt"
    spec = _python_spec(tmp_path, "print('x')", stdout=out)
    result = run_tree(spec, timeout_seconds=WAIT_LIMIT_SECONDS, grace_seconds=1.0)
    assert result == Completed(exit_code=0)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_stdin_is_devnull(tmp_path: Path) -> None:
    spec = _python_spec(tmp_path, "import sys; sys.exit(0 if sys.stdin.read() == '' else 3)")
    result = run_tree(spec, timeout_seconds=WAIT_LIMIT_SECONDS, grace_seconds=1.0)
    assert result == Completed(exit_code=0)


def test_exit_code_is_reported(tmp_path: Path) -> None:
    spec = _python_spec(tmp_path, "import sys; sys.exit(7)")
    result = run_tree(spec, timeout_seconds=WAIT_LIMIT_SECONDS, grace_seconds=1.0)
    assert result == Completed(exit_code=7)


class TestSpecValidation:
    def test_empty_argv_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SpawnError) as excinfo:
            SpawnSpec(argv=(), cwd=tmp_path, env={})
        assert excinfo.value.stage == "validate"

    def test_empty_argument_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SpawnError):
            SpawnSpec(argv=(sys.executable, ""), cwd=tmp_path, env={})

    def test_non_string_argument_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SpawnError):
            SpawnSpec(argv=(sys.executable, 5), cwd=tmp_path, env={})  # type: ignore[arg-type]

    def test_same_redirect_path_is_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "both.txt"
        with pytest.raises(SpawnError):
            SpawnSpec(argv=(sys.executable,), cwd=tmp_path, env={}, stdout_path=target, stderr_path=target)


def test_spawn_failure_releases_file_handles(tmp_path: Path) -> None:
    """実行file不在の失敗で、開いたredirect fileが閉じられている（Windowsでは削除可能になる）。"""
    out = tmp_path / "never-written.txt"
    spec = SpawnSpec(
        argv=(str(tmp_path / "missing-executable"),),
        cwd=tmp_path,
        env=child_env(),
        stdout_path=out,
    )
    with pytest.raises(SpawnError) as excinfo:
        run_tree(spec, timeout_seconds=WAIT_LIMIT_SECONDS, grace_seconds=1.0)
    assert excinfo.value.stage == "popen"
    assert excinfo.value.os_error is not None
    out.unlink()  # handleが残っているとWindowsでPermissionErrorになる
