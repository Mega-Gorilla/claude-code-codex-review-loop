# SPDX-License-Identifier: Apache-2.0
"""候補2: jsonschema library（Draft 2020-12）によるvalidator。

公開errorへは`ValidationError.message` / `instance`を使用せず、`validator`種別、
`absolute_path`、および決定論的なfield導出からcode + pathへ正規化する。

libraryとprotocolの意味論差はadapter側で吸収する必要がある:
- Draft 2020-12は小数部0のnumber（`1.0`）をintegerとして受理するため、
  「integer tokenのみ」というprotocol意味論のための追加checkを行う
- cross-field ruleはallOf位置 -> canonical code / pathのmapping表で変換する
- 未知field名とmap keyは序数tokenへ正規化する（common参照）
"""
from __future__ import annotations

import jsonschema

from p001_evaluation.common import (
    PublicError,
    map_key_token,
    unknown_field_token,
)

SAMPLE_MESSAGE_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "kind", "id", "summary", "blocking"],
    "properties": {
        "schema_version": {"type": "integer"},
        "kind": {"type": "string", "enum": ["finding", "note"]},
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

# cross-field ruleのschema位置 -> canonicalなpath
_CROSS_FIELD_PATHS = {0: "resolution_note", 1: "resolution_note", 2: "evidence"}

_VALIDATOR = jsonschema.Draft202012Validator(SAMPLE_MESSAGE_SCHEMA)

# schema上でmapとして定義された位置（additionalPropertiesがschema dictのproperty）。
# field名の文字列比較ではなくschema構造から導出する。
_MAP_PARENTS = {
    (name,)
    for name, spec in SAMPLE_MESSAGE_SCHEMA["properties"].items()  # type: ignore[union-attr]
    if isinstance(spec, dict) and isinstance(spec.get("additionalProperties"), dict)
}


def _instance_path(error: jsonschema.ValidationError, data: dict[str, object]) -> str:
    parts: list[str] = []
    raw: list[object] = []
    node: object = data
    for p in error.absolute_path:
        if isinstance(p, int):
            parts[-1] = f"{parts[-1]}[{p}]" if parts else f"$[{p}]"
            raw.append(p)
            if isinstance(node, list) and 0 <= p < len(node):
                node = node[p]
            continue
        if tuple(raw) in _MAP_PARENTS and isinstance(node, dict):
            # このsegmentはmapのdynamic key。現在のmap container自身から全keyを取得する
            parts.append(map_key_token(list(node.keys()), str(p)))
        else:
            parts.append(str(p))
        raw.append(str(p))
        if isinstance(node, dict):
            node = node.get(str(p))
    return ".".join(parts) if parts else "$"


def _convert(error: jsonschema.ValidationError, data: dict[str, object]) -> list[PublicError]:
    schema_path = list(error.absolute_schema_path)
    if schema_path and schema_path[0] == "allOf":
        rule = int(schema_path[1])
        return [PublicError("cross_field", _CROSS_FIELD_PATHS[rule])]

    path = _instance_path(error, data)
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
        unknown = [name for name in error.instance if name not in allowed]
        return [
            PublicError("unknown_field", prefix + unknown_field_token(unknown, name))
            for name in unknown
        ]
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


def _strict_integer_errors(data: dict[str, object]) -> list[PublicError]:
    """Draft 2020-12が受理する小数部0のfloatを、protocol意味論に従い型不一致にする。"""

    errors: list[PublicError] = []

    def _flag(value: object, path: str) -> None:
        if isinstance(value, float):
            errors.append(PublicError("type_mismatch", path))

    for name in ("schema_version",):
        if name in data:
            _flag(data[name], name)
    location = data.get("location")
    if isinstance(location, dict) and "line" in location:
        _flag(location["line"], "location.line")
    attachments = data.get("attachments")
    if isinstance(attachments, list):
        for i, item in enumerate(attachments):
            if isinstance(item, dict) and "size" in item:
                _flag(item["size"], f"attachments[{i}].size")
    metrics = data.get("metrics")
    if isinstance(metrics, dict):
        keys = list(metrics.keys())
        for key, value in metrics.items():
            _flag(value, f"metrics.{map_key_token(keys, key)}")
    return errors


def structural(data: dict[str, object]) -> list[PublicError]:
    errors: list[PublicError] = []
    for error in _VALIDATOR.iter_errors(data):
        errors.extend(_convert(error, data))
    errors.extend(_strict_integer_errors(data))
    # 同一問題がallOf経由で重複報告される場合があるため決定論的にdedupeする
    seen: set[tuple[str, str]] = set()
    unique: list[PublicError] = []
    for e in errors:
        key = (e.code, e.path)
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique
