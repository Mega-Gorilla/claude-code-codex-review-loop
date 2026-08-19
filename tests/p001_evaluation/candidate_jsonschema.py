# SPDX-License-Identifier: Apache-2.0
"""候補2: jsonschema library（Draft 2020-12）によるvalidator。

公開errorへは`ValidationError.message` / `instance`を使用せず、`validator`と
`absolute_path`（および欠落・過剰fieldの決定論的な導出）からcode + pathへ正規化する。
cross-field ruleはallOf / dependentSchemasで表現し、rule単位のmapping表で
canonicalなcode / pathへ変換する。
"""
from __future__ import annotations

import jsonschema

from p001_evaluation.common import PublicError

SAMPLE_MESSAGE_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "kind", "id", "summary", "blocking"],
    "properties": {
        "schema_version": {"type": "integer"},
        "kind": {"enum": ["finding", "note"]},
        "id": {"type": "string", "minLength": 1},
        "summary": {"type": "string", "maxLength": 10_000},
        "blocking": {"type": "boolean"},
        "location": {
            "type": "object",
            "additionalProperties": False,
            "required": ["file"],
            "properties": {
                "file": {"type": "string", "minLength": 1},
                "line": {"type": ["integer", "null"]},
            },
        },
        "tags": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "evidence": {"type": ["string", "null"]},
        "resolved": {"type": "boolean"},
        "resolution_note": {"type": "string", "minLength": 1},
        "attachments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "size"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "size": {"type": "integer"},
                },
            },
        },
        "metrics": {"type": "object", "additionalProperties": {"type": "integer"}},
    },
    "allOf": [
        # rule 0: resolution_noteはresolved == trueのときだけ許可
        {
            "if": {"required": ["resolution_note"]},
            "then": {"properties": {"resolved": {"const": True}}, "required": ["resolved"]},
        },
        # rule 1: resolved == trueならresolution_note必須
        {
            "if": {"properties": {"resolved": {"const": True}}, "required": ["resolved"]},
            "then": {"required": ["resolution_note"]},
        },
        # rule 2: kind == noteのときevidence禁止
        {
            "if": {"properties": {"kind": {"const": "note"}}, "required": ["kind"]},
            "then": {"not": {"required": ["evidence"]}},
        },
    ],
}

# cross-field ruleのschema位置 -> canonicalなpath。
# allOf配下のerrorは既定ではrootや関連fieldに位置づくため、rule単位の変換が必要になる。
_CROSS_FIELD_PATHS = {0: "resolution_note", 1: "resolution_note", 2: "evidence"}

_VALIDATOR = jsonschema.Draft202012Validator(SAMPLE_MESSAGE_SCHEMA)

# nullを許可するfield（type listに"null"を含むもの）
_NULLABLE = {("location", "line"), ("evidence",)}


def _instance_path(error: jsonschema.ValidationError) -> str:
    parts: list[str] = []
    for p in error.absolute_path:
        if isinstance(p, int):
            parts[-1] = f"{parts[-1]}[{p}]" if parts else f"$[{p}]"
        else:
            parts.append(str(p))
    return ".".join(parts) if parts else "$"


def _convert(error: jsonschema.ValidationError, data: dict[str, object]) -> list[PublicError]:
    schema_path = list(error.absolute_schema_path)
    if schema_path and schema_path[0] == "allOf":
        rule = int(schema_path[1])
        return [PublicError("cross_field", _CROSS_FIELD_PATHS[rule])]

    path = _instance_path(error)
    prefix = "" if path == "$" else path + "."
    validator = error.validator

    if validator == "required":
        assert isinstance(error.validator_value, list)
        missing = [
            name for name in error.validator_value
            if isinstance(error.instance, dict) and name not in error.instance
        ]
        return [PublicError("required_missing", prefix + name) for name in missing]
    if validator == "additionalProperties":
        assert isinstance(error.instance, dict)
        allowed = set((error.schema or {}).get("properties", {}))
        extras = [name for name in error.instance if name not in allowed]
        return [PublicError("unknown_field", prefix + name) for name in extras]
    if validator == "type":
        allowed_types = error.validator_value
        allows_null = allowed_types == "null" or (
            isinstance(allowed_types, list) and "null" in allowed_types
        )
        if error.instance is None and not allows_null:
            return [PublicError("null_not_allowed", path)]
        return [PublicError("type_mismatch", path)]
    if validator == "minLength":
        return [PublicError("empty_string", path)]
    if validator == "maxLength":
        return [PublicError("max_length", path)]
    if validator in ("enum", "const"):
        return [PublicError("enum_invalid", path)]
    return [PublicError("validation_failed", path)]


def structural(data: dict[str, object]) -> list[PublicError]:
    errors: list[PublicError] = []
    for error in _VALIDATOR.iter_errors(data):
        errors.extend(_convert(error, data))
    # 同一問題がallOf経由で重複報告される場合があるため決定論的にdedupeする
    seen: set[tuple[str, str]] = set()
    unique: list[PublicError] = []
    for e in errors:
        key = (e.code, e.path)
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique
