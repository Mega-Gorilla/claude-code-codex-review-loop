# SPDX-License-Identifier: Apache-2.0
"""候補1: 標準libraryのみの専用protocol validator。

汎用JSON Schema validatorではなく、C-02が必要とする機能
（必須/optional、nested object、list、list of object、map、enum、基本型、null許可、
非空文字列、size limit、extra field拒否、cross-field rule）だけを宣言的specとして扱う。
"""
from __future__ import annotations

from dataclasses import dataclass

from p001_evaluation.common import PublicError, is_integer_token, map_key_token, unknown_field_token


@dataclass(frozen=True)
class Field:
    types: tuple[type, ...]
    required: bool = True
    allow_none: bool = False
    non_empty: bool = False
    max_len: int | None = None
    enum: tuple[str, ...] | None = None
    fields: dict[str, Field] | None = None  # nested object
    items: Field | None = None  # list
    values: Field | None = None  # map（任意keyのobject）


def _check(value: object, spec: Field, path: str, errors: list[PublicError]) -> None:
    if value is None:
        if not spec.allow_none:
            errors.append(PublicError("null_not_allowed", path))
        return
    if int in spec.types and bool not in spec.types and not isinstance(value, str | list | dict):
        # integerはJSON integer tokenのみ（common参照）。boolとfloatは型不一致とする。
        if not is_integer_token(value):
            errors.append(PublicError("type_mismatch", path))
            return
    elif not isinstance(value, spec.types):
        errors.append(PublicError("type_mismatch", path))
        return
    if isinstance(value, str):
        if spec.non_empty and value == "":
            errors.append(PublicError("empty_string", path))
        if spec.max_len is not None and len(value) > spec.max_len:
            errors.append(PublicError("max_length", path))
        if spec.enum is not None and value not in spec.enum:
            errors.append(PublicError("enum_invalid", path))
    if isinstance(value, dict):
        if spec.fields is not None:
            _check_object(value, spec.fields, path, errors)
        elif spec.values is not None:
            all_keys = list(value.keys())
            for key, item in value.items():
                _check(item, spec.values, f"{path}.{map_key_token(all_keys, key)}", errors)
    if isinstance(value, list) and spec.items is not None:
        for i, item in enumerate(value):
            _check(item, spec.items, f"{path}[{i}]", errors)


def _check_object(
    obj: dict[str, object], fields: dict[str, Field], path: str, errors: list[PublicError]
) -> None:
    prefix = "" if path == "$" else path + "."
    for name, spec in fields.items():
        if name not in obj:
            if spec.required:
                errors.append(PublicError("required_missing", prefix + name))
            continue
        _check(obj[name], spec, prefix + name, errors)
    unknown = [name for name in obj if name not in fields]
    for name in unknown:
        errors.append(PublicError("unknown_field", prefix + unknown_field_token(unknown, name)))


SAMPLE_MESSAGE = {
    "schema_version": Field(types=(int,)),
    "kind": Field(types=(str,), enum=("finding", "note")),
    "id": Field(types=(str,), non_empty=True),
    "summary": Field(types=(str,), max_len=10_000),
    "blocking": Field(types=(bool,)),
    "location": Field(
        types=(dict,), required=False,
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
        types=(list,), required=False,
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


_RULES = (
    _rule_resolution_note_requires_resolved,
    _rule_resolved_requires_note,
    _rule_note_forbids_evidence,
)


def structural(data: dict[str, object]) -> list[PublicError]:
    errors: list[PublicError] = []
    _check_object(data, SAMPLE_MESSAGE, "$", errors)
    for rule in _RULES:
        errors.extend(rule(data))
    return errors
