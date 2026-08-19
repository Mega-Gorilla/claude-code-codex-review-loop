# SPDX-License-Identifier: Apache-2.0
"""P-001評価corpus（ADR-0003）の整合を検証する。

corpusは将来C-02のvalidator regression testへ接続するため恒久的に保持する。
このtestはvalidator実装に依存せず、corpusとmanifest自体の健全性だけを検証する。
"""

import json
from pathlib import Path

CORPUS = Path(__file__).resolve().parent / "p001_corpus"

VALID_EXPECTS = {"accept", "reject"}
VALID_REASONS = {"json", "schema"}


def _manifest() -> dict[str, object]:
    with (CORPUS / "manifest.json").open("rb") as handle:
        return json.load(handle)


def _cases() -> list[dict[str, object]]:
    cases = _manifest()["cases"]
    assert isinstance(cases, list)
    return cases


def test_manifest_entries_are_well_formed() -> None:
    for case in _cases():
        assert case["expect"] in VALID_EXPECTS, case
        if case["expect"] == "reject":
            reasons = case.get("reasons")
            assert isinstance(reasons, list) and reasons, case
            assert set(reasons) <= VALID_REASONS, case
        else:
            assert "reasons" not in case and "error_path" not in case, case


def test_all_manifest_files_exist_and_no_orphans() -> None:
    listed = {str(case["file"]) for case in _cases()}
    actual = {
        str(p.relative_to(CORPUS)).replace("\\", "/")
        for p in CORPUS.rglob("*.json")
        if p.name != "manifest.json"
    }
    assert listed == actual, f"manifestとfileの不一致: listedのみ={listed - actual} fileのみ={actual - listed}"


def test_representative_cases_parse_as_json_objects() -> None:
    for case in _cases():
        if case["expect"] != "accept":
            continue
        data = json.loads((CORPUS / str(case["file"])).read_text(encoding="utf-8"))
        assert isinstance(data, dict), case["file"]


def test_json_reason_cases_fail_to_parse() -> None:
    """reasonがjsonのみのcaseは、JSONとして本当に解析できないことを保証する。"""

    for case in _cases():
        if case["expect"] != "reject" or case.get("reasons") != ["json"]:
            continue
        raw = (CORPUS / str(case["file"])).read_text(encoding="utf-8")
        try:
            json.loads(raw)
        except (json.JSONDecodeError, RecursionError):
            continue
        raise AssertionError(f"{case['file']} はJSONとして解析できてしまう")


def test_schema_reason_cases_parse_as_json() -> None:
    """reasonがschemaのみのcaseは、JSON層は通過することを保証する。"""

    for case in _cases():
        if case["expect"] != "reject" or case.get("reasons") != ["schema"]:
            continue
        json.loads((CORPUS / str(case["file"])).read_text(encoding="utf-8"))
        assert "error_path" in case, f"{case['file']} はerror_pathの期待値を持つべき"
