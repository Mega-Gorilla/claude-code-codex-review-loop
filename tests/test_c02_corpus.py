# SPDX-License-Identifier: Apache-2.0
"""Phase 0評価corpusのC-02 validatorへの接続（AC-C02-01、ADR-0003の引き継ぎ4）。

`tests/p001_corpus/`全件をmanifest駆動で製品pipelineへ流し、verdict / stage /
error pathの一致とsentinel非漏洩を検証する。sample specはADR-0003評価時の
`candidate_dedicated.SAMPLE_MESSAGE`と同一内容を製品Field APIで定義したもの。
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_code_codex_review_loop.schema import SchemaKind, validate
from claude_code_codex_review_loop.schema.registry import SchemaDefinition
from claude_code_codex_review_loop.schema.validate import Field, PublicError, VersionSpec

CORPUS = Path(__file__).resolve().parent / "p001_corpus"

SENTINEL = "SENTINELghp000"

_SAMPLE_FIELDS: dict[str, Field] = {
    "schema_version": Field(types=(int,)),
    "kind": Field(types=(str,), enum=("finding", "note")),
    "id": Field(types=(str,), non_empty=True),
    "summary": Field(types=(str,), max_len=10_000),
    "blocking": Field(types=(bool,)),
    "location": Field(
        types=(dict,),
        required=False,
        fields={
            "file": Field(types=(str,), non_empty=True),
            "line": Field(types=(int,), required=False, allow_none=True),
        },
    ),
    "tags": Field(types=(list,), required=False, items=Field(types=(str,), non_empty=True)),
    "evidence": Field(types=(str,), required=False, allow_none=True),
    "resolved": Field(types=(bool,), required=False),
    "resolution_note": Field(types=(str,), required=False, non_empty=True),
    "attachments": Field(
        types=(list,),
        required=False,
        items=Field(
            types=(dict,),
            fields={
                "name": Field(types=(str,), non_empty=True),
                "size": Field(types=(int,)),
            },
        ),
    ),
    "metrics": Field(types=(dict,), required=False, values=Field(types=(int,))),
}


def _rule_resolution_note_requires_resolved(data: dict[str, object]) -> list[PublicError]:
    if "resolution_note" in data and data.get("resolved") is not True:
        return [PublicError("cross_field", "resolution_note")]
    return []


def _rule_resolved_requires_note(data: dict[str, object]) -> list[PublicError]:
    if data.get("resolved") is True and "resolution_note" not in data:
        return [PublicError("cross_field", "resolution_note")]
    return []


def _rule_note_forbids_evidence(data: dict[str, object]) -> list[PublicError]:
    if data.get("kind") == "note" and "evidence" in data:
        return [PublicError("cross_field", "evidence")]
    return []


# 評価sampleと同一のschemaを、C-02の製品APIでSchemaDefinitionとして定義する
SAMPLE_DEFINITION = SchemaDefinition(
    kind=SchemaKind.REVIEW_RESULT,  # kindは識別にのみ使う（corpusはsample schemaのfixture）
    versions={
        1: VersionSpec(
            fields=_SAMPLE_FIELDS,
            rules=(
                _rule_resolution_note_requires_resolved,
                _rule_resolved_requires_note,
                _rule_note_forbids_evidence,
            ),
        )
    },
)


def _cases() -> list[dict[str, object]]:
    with (CORPUS / "manifest.json").open("rb") as handle:
        cases = json.load(handle)["cases"]
    assert isinstance(cases, list) and cases
    return cases


def test_corpus_matches_production_pipeline() -> None:
    """全62 caseでverdict / stage / error pathがmanifestの期待と一致する。"""
    for case in _cases():
        raw = (CORPUS / str(case["file"])).read_bytes()
        result = validate(SAMPLE_DEFINITION, raw)
        if case["expect"] == "accept":
            assert result.ok, (case["file"], result.errors)
            continue
        assert not result.ok, case["file"]
        assert result.stage == case["stage"], (case["file"], result.stage)
        if case["stage"] in {"version", "schema"}:
            expected_paths = (
                {str(case["error_path"])}
                if "error_path" in case
                else {str(p) for p in case["error_paths"]}  # type: ignore[union-attr]
            )
            assert {e.path for e in result.errors} == expected_paths, case["file"]


def test_public_errors_do_not_leak_sentinel_or_raw_keys() -> None:
    """sentinel値・sentinel入りkey名が公開errorへ漏れない（ADR-0003の引き継ぎ5）。"""
    for case in _cases():
        raw = (CORPUS / str(case["file"])).read_bytes()
        result = validate(SAMPLE_DEFINITION, raw)
        for error in result.errors:
            assert SENTINEL not in error.code and SENTINEL not in error.path, case["file"]


def test_pipeline_is_deterministic_on_corpus() -> None:
    for case in _cases():
        raw = (CORPUS / str(case["file"])).read_bytes()
        assert validate(SAMPLE_DEFINITION, raw) == validate(SAMPLE_DEFINITION, raw), case["file"]
