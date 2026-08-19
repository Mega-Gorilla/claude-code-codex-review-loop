# SPDX-License-Identifier: Apache-2.0
"""P-001評価（ADR-0003）の再現test。

両候補を同一interface（共通pipeline + structural validator）で全corpusへ適用し、
verdict / stage / error path / public errorの一致を検証する。CIはUbuntu / Windowsの
両方でこれを実行する。候補2は評価専用のoptional dependency（`.[p001]`）を要する。
"""

import importlib.metadata
import json
import re
from pathlib import Path

import pytest

# 評価依存が欠落した環境ではskipではなく明示的にfailさせる（collection error）。
# 開発手順は`pip install -e ".[dev,p001]" -c constraints/p001.txt`（CONTRIBUTING参照）。
from p001_evaluation import candidate_dedicated, candidate_jsonschema, common

# ADR-0003「再現方法」に記録した評価環境。resolved versionの乖離をfailにする。
P001_RESOLVED_VERSIONS = {
    "jsonschema": "4.26.0",
    "attrs": "25.3.0",
    "jsonschema-specifications": "2025.9.1",
    "referencing": "0.37.0",
    "rpds-py": "2026.6.3",
}

CORPUS = Path(__file__).resolve().parent / "p001_corpus"
SENTINEL = "SENTINELghp000"

CANDIDATES = {
    "dedicated": candidate_dedicated.structural,
    "jsonschema": candidate_jsonschema.structural,
}


def _cases() -> list[dict[str, object]]:
    with (CORPUS / "manifest.json").open("rb") as handle:
        return json.load(handle)["cases"]


def _run_case(name: str, case: dict[str, object]) -> tuple[str, list[common.PublicError]]:
    raw = (CORPUS / str(case["file"])).read_bytes()
    return common.run(CANDIDATES[name], raw)


@pytest.mark.parametrize("name", sorted(CANDIDATES))
def test_candidate_matches_manifest(name: str) -> None:
    failures: list[str] = []
    for case in _cases():
        verdict, errors = _run_case(name, case)
        if case["expect"] == "accept":
            if verdict != "accept":
                failures.append(f"{case['file']}: 期待accept、実際{verdict} {errors}")
            continue
        expected = f"reject:{case['stage']}"
        if verdict != expected:
            failures.append(f"{case['file']}: 期待{expected}、実際{verdict} {errors}")
            continue
        want_path = case.get("error_path")
        if want_path and want_path not in [e.path for e in errors]:
            failures.append(f"{case['file']}: 期待path {want_path}、実際 {[e.path for e in errors]}")
    assert not failures, "\n".join(failures)


def test_candidates_produce_identical_public_results() -> None:
    """verdictとpublic error（code + path）の集合が両候補で一致する。"""

    mismatches: list[str] = []
    for case in _cases():
        results = {}
        for name in CANDIDATES:
            verdict, errors = _run_case(name, case)
            results[name] = (verdict, sorted((e.code, e.path) for e in errors))
        if results["dedicated"] != results["jsonschema"]:
            mismatches.append(
                f"{case['file']}: dedicated={results['dedicated']} jsonschema={results['jsonschema']}"
            )
    assert not mismatches, "\n".join(mismatches)


@pytest.mark.parametrize("name", sorted(CANDIDATES))
def test_no_input_values_leak_into_public_errors(name: str) -> None:
    """malformed入力へ埋め込んだsentinelが、公開error（code / path）へ現れない。"""

    leaked: list[str] = []
    for case in _cases():
        raw = (CORPUS / str(case["file"])).read_bytes()
        if SENTINEL.encode() not in raw:
            continue
        _, errors = _run_case(name, case)
        for e in errors:
            if SENTINEL in e.code or SENTINEL in e.path:
                leaked.append(f"{case['file']}: {e}")
    assert not leaked, "\n".join(leaked)


def test_sentinel_cases_exist() -> None:
    """sentinel testが空振りしていないことを保証する。"""

    count = sum(
        1 for case in _cases()
        if SENTINEL.encode() in (CORPUS / str(case["file"])).read_bytes()
    )
    assert count >= 5, f"sentinel入りcaseが少なすぎる: {count}"


@pytest.mark.parametrize("name", sorted(CANDIDATES))
def test_results_are_deterministic(name: str) -> None:
    for case in _cases():
        first = _run_case(name, case)
        second = _run_case(name, case)
        assert first == second, case["file"]


_PATH_SAFE = re.compile(r"^[A-Za-z0-9_.$#<>\[\]…-]+$")


@pytest.mark.parametrize("name", sorted(CANDIDATES))
def test_public_paths_are_safe(name: str) -> None:
    """公開pathが安全な文字集合と長さ上限に収まる（dynamic keyのraw値を含まない）。"""

    for case in _cases():
        _, errors = _run_case(name, case)
        for e in errors:
            assert len(e.path) <= common.MAX_PATH_LENGTH, f"{case['file']}: {len(e.path)}"
            assert _PATH_SAFE.match(e.path), f"{case['file']}: 不正な文字を含むpath {e.path!r}"


def test_evaluation_dependency_versions_match_adr_record() -> None:
    """resolved versionがADR-0003の記録と一致する（推移依存のdriftをfailにする）。"""

    mismatches = []
    for package, expected in P001_RESOLVED_VERSIONS.items():
        actual = importlib.metadata.version(package)
        if actual != expected:
            mismatches.append(f"{package}: 期待{expected}、実際{actual}")
    assert not mismatches, (
        "評価依存のresolved versionがADR記録と乖離: " + ", ".join(mismatches)
        + "。constraints/p001.txtとADR-0003を同時に更新すること。"
    )
