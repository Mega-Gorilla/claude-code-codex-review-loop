# SPDX-License-Identifier: Apache-2.0

import os
import subprocess
import sys
import tomllib
from pathlib import Path

from test_repository_contract import ROOT, _tracked_files

BASELINE_PATH = ROOT / "quality-baseline.toml"

REGISTRATION_HINT = (
    "quality-baseline.tomlの[module-size]へ登録し、上限の根拠をPRへ記載する。"
    "手順はCONTRIBUTING.mdの「品質ゲートの運用」を参照。"
)


def _baseline() -> dict[str, object]:
    with BASELINE_PATH.open("rb") as handle:
        return tomllib.load(handle)


def _module_size_table() -> dict[str, int]:
    table = _baseline()["module-size"]
    assert isinstance(table, dict)
    return {path: int(limit) for path, limit in table.items()}


def _tracked_python_files() -> tuple[str, ...]:
    return tuple(path for path in _tracked_files() if path.endswith(".py"))


def test_coverage_floor_is_a_valid_percentage() -> None:
    floor = _baseline()["coverage"]["floor"]  # type: ignore[index]
    assert isinstance(floor, int)
    assert 0 <= floor <= 100


def test_all_python_modules_are_registered_in_size_baseline() -> None:
    """未登録moduleが検査を回避しないよう、登録漏れをfailにする。"""

    table = _module_size_table()
    unregistered = [path for path in _tracked_python_files() if path not in table]
    assert not unregistered, f"module size baseline未登録: {unregistered}。{REGISTRATION_HINT}"


def test_size_baseline_has_no_stale_entries() -> None:
    """存在しないfileの登録が残ると、baselineの信頼性が下がる。"""

    tracked = set(_tracked_python_files())
    stale = [path for path in _module_size_table() if path not in tracked]
    assert not stale, f"module size baselineへ登録されているがgit管理下に存在しない: {stale}"


def test_modules_do_not_exceed_size_baseline() -> None:
    table = _module_size_table()
    violations = []
    for path, limit in table.items():
        actual = len((ROOT / path).read_text(encoding="utf-8").splitlines())
        if actual > limit:
            violations.append(f"{path}: {actual}行 > 上限{limit}行")
    assert not violations, (
        f"module size上限を超過: {violations}。分割を検討し、上限を引き上げる場合は"
        "quality-baseline.tomlを変更して理由をPRへ記載する。"
    )


def test_subprocess_execution_is_measured_by_coverage(tmp_path: Path) -> None:
    """subprocessとして実行したPython codeがcoverageへ計上されることを確認する。

    親のcoverage設定から隔離するため、COVERAGE_FILEとCOVERAGE_RCFILEをtmpへ向ける。
    """

    script = tmp_path / "child_script.py"
    script.write_text(
        "import claude_code_codex_review_loop as pkg\nprint(pkg.__version__)\n",
        encoding="utf-8",
    )
    rcfile = tmp_path / "coveragerc"
    rcfile.write_text("[run]\nbranch = True\n", encoding="utf-8")
    data_file = tmp_path / ".coverage"
    env = {
        **os.environ,
        "COVERAGE_FILE": str(data_file),
        "COVERAGE_RCFILE": str(rcfile),
    }

    run = subprocess.run(
        [sys.executable, "-m", "coverage", "run", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=tmp_path,
    )
    assert run.returncode == 0, run.stderr
    assert data_file.exists()

    report = subprocess.run(
        [sys.executable, "-m", "coverage", "report"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=tmp_path,
    )
    assert report.returncode == 0, report.stderr
    # coverage 7のfile patternは`*`がpath separatorを跨がないため、includeでは絞らず全reportを検証する。
    assert "claude_code_codex_review_loop" in report.stdout, (
        "subprocessの実行がcoverageへ計上されていない:\n" + report.stdout
    )
