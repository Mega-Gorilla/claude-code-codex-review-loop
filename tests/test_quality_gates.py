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


def test_plain_subprocess_is_measured_automatically(tmp_path: Path) -> None:
    """`patch = subprocess`により、通常のPython subprocessが自動計測されることを確認する。

    coverage配下の親scriptが素の`python child.py`をspawnし、childだけが実行した行が
    combine後のreportへ含まれることを検証する。外側のpytest coverageと干渉しないよう、
    COVERAGE_FILE / COVERAGE_RCFILE / COVERAGE_PROCESS_STARTをtmpへ隔離する。
    """

    child = tmp_path / "child.py"
    child.write_text(
        "import claude_code_codex_review_loop as pkg\nprint(pkg.__version__)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess\nimport sys\n"
        'subprocess.run([sys.executable, "child.py"], check=True)\n',
        encoding="utf-8",
    )
    rcfile = tmp_path / "coveragerc"
    rcfile.write_text("[run]\nbranch = True\nparallel = True\npatch = subprocess\n", encoding="utf-8")
    data_file = tmp_path / ".coverage"
    env = {**os.environ, "COVERAGE_FILE": str(data_file), "COVERAGE_RCFILE": str(rcfile)}
    env.pop("COVERAGE_PROCESS_START", None)

    def _run(*args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, "-m", "coverage", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=tmp_path,
        )
        assert result.returncode == 0, f"coverage {args[0]} failed:\n{result.stdout}\n{result.stderr}"
        return result

    _run("run", str(parent))
    # parallel dataが親・子それぞれから書かれているはず
    assert list(tmp_path.glob(".coverage*")), "parallel dataが生成されていない"
    _run("combine")
    report = _run("report")
    # coverage 7のfile patternは`*`がpath separatorを跨がないため、includeでは絞らず全reportを検証する。
    # 親scriptはpackageをimportしないため、次の2行はchildの実行がcombineへ含まれた証拠になる。
    assert "child.py" in report.stdout, "childの実行がcoverageへ計上されていない:\n" + report.stdout
    assert "claude_code_codex_review_loop" in report.stdout, (
        "childがimportしたpackageがcoverageへ計上されていない:\n" + report.stdout
    )
