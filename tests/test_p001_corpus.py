# SPDX-License-Identifier: Apache-2.0
"""P-001評価corpus（ADR-0003）の健全性を検証する。

corpusはC-02のvalidator regression testへ接続するため恒久的に保持する。
このtestは候補validatorへ依存せず、corpusとmanifest自体の整合だけを検証する。
"""

import json
from pathlib import Path

from p001_evaluation.common import MAX_INPUT_BYTES, STAGES

CORPUS = Path(__file__).resolve().parent / "p001_corpus"


def _cases() -> list[dict[str, object]]:
    with (CORPUS / "manifest.json").open("rb") as handle:
        cases = json.load(handle)["cases"]
    assert isinstance(cases, list) and cases
    return cases


def test_manifest_entries_are_well_formed() -> None:
    for case in _cases():
        assert case["expect"] in {"accept", "reject"}, case
        if case["expect"] == "reject":
            # 期待stageは一意とする（曖昧な複数分類を許さない）
            assert case["stage"] in STAGES, case
            if case["stage"] in {"version", "schema"}:
                assert isinstance(case.get("error_path"), str) and case["error_path"], case
        else:
            assert "stage" not in case and "error_path" not in case, case


def test_all_manifest_files_exist_and_no_orphans() -> None:
    listed = {str(case["file"]) for case in _cases()}
    actual = {
        str(p.relative_to(CORPUS)).replace("\\", "/")
        for p in CORPUS.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    assert listed == actual, f"manifestとfileの不一致: listedのみ={listed - actual} fileのみ={actual - listed}"


def test_accept_cases_parse_as_json_objects() -> None:
    for case in _cases():
        if case["expect"] != "accept":
            continue
        data = json.loads((CORPUS / str(case["file"])).read_text(encoding="utf-8"))
        assert isinstance(data, dict), case["file"]


def test_size_stage_cases_exceed_input_limit() -> None:
    for case in _cases():
        if case.get("stage") != "size":
            continue
        raw = (CORPUS / str(case["file"])).read_bytes()
        assert len(raw) > MAX_INPUT_BYTES, case["file"]


def test_utf8_stage_cases_fail_to_decode() -> None:
    for case in _cases():
        if case.get("stage") != "utf8":
            continue
        raw = (CORPUS / str(case["file"])).read_bytes()
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        raise AssertionError(f"{case['file']} はUTF-8として解析できてしまう")


def test_json_stage_cases_decode_but_fail_to_parse() -> None:
    for case in _cases():
        if case.get("stage") != "json":
            continue
        text = (CORPUS / str(case["file"])).read_text(encoding="utf-8")
        try:
            json.loads(text)
        except (json.JSONDecodeError, RecursionError):
            continue
        raise AssertionError(f"{case['file']} はJSONとして解析できてしまう")


def test_version_and_schema_stage_cases_parse_as_json() -> None:
    for case in _cases():
        if case.get("stage") not in {"version", "schema"}:
            continue
        json.loads((CORPUS / str(case["file"])).read_text(encoding="utf-8"))
        if case["stage"] == "version":
            assert case["error_path"] == "schema_version", case["file"]
