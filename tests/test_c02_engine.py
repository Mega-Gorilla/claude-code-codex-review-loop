# SPDX-License-Identifier: Apache-2.0
"""C-02 validator engineの単体test（stage境界・診断正規化・repair。AC-C02-01 / 02）。"""

from __future__ import annotations

import json

import pytest

from claude_code_codex_review_loop.schema import build_registry
from claude_code_codex_review_loop.schema.registry import (
    SchemaDefinition,
    SchemaKind,
    array,
    integer,
    obj,
    repair_and_validate,
    schema_version_field,
    text,
    validate,
    validate_object,
)
from claude_code_codex_review_loop.schema.validate import (
    Field,
    PublicError,
    VersionSpec,
    canonicalize,
    map_key_token,
    sanitize_path,
    strip_bom,
    unknown_field_token,
)


def _definition(fields: dict[str, Field], **kwargs: object) -> SchemaDefinition:
    fields = {"schema_version": schema_version_field(), **fields}
    return SchemaDefinition(
        kind=SchemaKind.USER_CANCEL, versions={1: VersionSpec(fields=fields)}, **kwargs  # type: ignore[arg-type]
    )


def _raw(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class TestStages:
    def test_size_stage_precedes_everything(self) -> None:
        definition = _definition({}, max_input_bytes=16)
        result = validate(definition, b" " * 17)
        assert (result.ok, result.stage) == (False, "size")
        assert result.errors == (PublicError("input_too_large", "$"),)

    def test_utf8_stage(self) -> None:
        result = validate(_definition({}), b'\xff\xfe{"schema_version":1}')
        assert (result.ok, result.stage) == (False, "utf8")

    def test_json_stage_rejects_nonstandard_tokens(self) -> None:
        result = validate(_definition({}), b'{"schema_version": NaN}')
        assert (result.ok, result.stage) == (False, "json")

    def test_root_must_be_object(self) -> None:
        result = validate(_definition({}), b"[1, 2]")
        assert (result.ok, result.stage) == (False, "schema")
        assert result.errors == (PublicError("root_not_object", "$"),)

    def test_unknown_integer_version_wins_over_other_violations(self) -> None:
        definition = _definition({"name": text()})
        result = validate(definition, _raw({"schema_version": 99, "name": 123}))
        assert (result.stage, result.errors) == (
            "version",
            (PublicError("unknown_version", "schema_version"),),
        )

    def test_float_version_is_a_schema_type_mismatch(self) -> None:
        result = validate(_definition({}), _raw({"schema_version": 1.0}))
        assert result.stage == "schema"
        assert result.errors == (PublicError("type_mismatch", "schema_version"),)

    def test_missing_version_is_required_missing(self) -> None:
        result = validate(_definition({}), _raw({}))
        assert result.errors == (PublicError("required_missing", "schema_version"),)


class TestDiagnostics:
    def test_canonicalize_prefers_null_then_type(self) -> None:
        errors = [
            PublicError("enum_invalid", "$.kind"),
            PublicError("type_mismatch", "$.kind"),
            PublicError("null_not_allowed", "$.kind"),
            PublicError("required_missing", "$.other"),
        ]
        assert canonicalize(errors) == [
            PublicError("null_not_allowed", "$.kind"),
            PublicError("required_missing", "$.other"),
        ]

    def test_canonicalize_keeps_first_error_when_priority_is_not_lower(self) -> None:
        errors = [
            PublicError("null_not_allowed", "$.kind"),
            PublicError("type_mismatch", "$.kind"),
        ]
        assert canonicalize(errors) == [PublicError("null_not_allowed", "$.kind")]

    def test_list_without_item_spec_is_accepted_as_is(self) -> None:
        definition = _definition({"data": Field(types=(list,), required=False)})
        result = validate(definition, _raw({"schema_version": 1, "data": [1, "x", None]}))
        assert result.ok

    def test_dynamic_key_tokens_are_ordinal(self) -> None:
        assert unknown_field_token(["zz", "aa"], "zz") == "<unknown#2>"
        assert map_key_token(["b", "a", "c"], "c") == "<key#3>"

    def test_sanitize_path_removes_control_chars_and_truncates(self) -> None:
        assert sanitize_path("a\nb\x00c") == "abc"
        long_path = "x" * 300
        cleaned = sanitize_path(long_path)
        assert len(cleaned) == 120 and cleaned.endswith("…")

    def test_max_items_is_reported(self) -> None:
        definition = _definition({"items": array(integer(), max_items=2)})
        result = validate(definition, _raw({"schema_version": 1, "items": [1, 2, 3]}))
        assert result.errors == (PublicError("max_items", "items"),)

    def test_validate_object_skips_transport_stages(self) -> None:
        definition = _definition({"name": text()})
        result = validate_object(definition, {"schema_version": 1, "name": "ok"})
        assert result.ok and result.version == 1


class TestRepair:
    def test_bom_is_stripped_and_validated_by_same_validator(self) -> None:
        definition = _definition({"name": text()})
        raw = b"\xef\xbb\xbf" + _raw({"schema_version": 1, "name": "ok"})
        assert not validate(definition, raw).ok  # repairなしではutf8/json境界で不正
        repaired = repair_and_validate(definition, raw)
        assert repaired.ok and repaired.payload == {"schema_version": 1, "name": "ok"}

    def test_strip_bom_only_removes_prefix(self) -> None:
        assert strip_bom(b"\xef\xbb\xbfabc") == b"abc"
        assert strip_bom(b"abc") == b"abc"

    def test_declared_default_is_applied_to_missing_optional_field(self) -> None:
        definition = _definition(
            {
                "name": text(),
                "tags": Field(types=(list,), required=False, items=Field(types=(str,)), default=[]),
            }
        )
        result = repair_and_validate(definition, _raw({"schema_version": 1, "name": "ok"}))
        assert result.ok and result.payload is not None
        assert result.payload["tags"] == []

    def test_default_never_overwrites_existing_value(self) -> None:
        definition = _definition(
            {"tags": Field(types=(list,), required=False, items=Field(types=(str,)), default=[])}
        )
        result = repair_and_validate(definition, _raw({"schema_version": 1, "tags": ["x"]}))
        assert result.ok and result.payload is not None
        assert result.payload["tags"] == ["x"]

    def test_required_field_is_never_fabricated(self) -> None:
        """既定値はoptional fieldの損失のない補完に限る（意味的fieldの捏造をしない）。"""
        definition = _definition({"name": Field(types=(str,), default="fabricated")})
        result = repair_and_validate(definition, _raw({"schema_version": 1}))
        assert not result.ok
        assert result.errors == (PublicError("required_missing", "name"),)

    def test_nested_defaults_are_applied(self) -> None:
        definition = _definition(
            {
                "meta": obj(
                    {"note": Field(types=(str,), required=False, default="")}, required=False
                )
            }
        )
        result = repair_and_validate(definition, _raw({"schema_version": 1, "meta": {}}))
        assert result.ok and result.payload is not None
        assert result.payload["meta"] == {"note": ""}

    def test_default_is_not_shared_between_repairs(self) -> None:
        """返却payloadの変更がspec定義のdefaultへ漏れず、次のrepairは宣言時defaultのまま。"""
        definition = _definition(
            {"tags": Field(types=(list,), required=False, items=Field(types=(str,)), default=[])}
        )
        first = repair_and_validate(definition, _raw({"schema_version": 1}))
        assert first.ok and first.payload is not None
        tags = first.payload["tags"]
        assert isinstance(tags, list)
        tags.append("leaked")
        second = repair_and_validate(definition, _raw({"schema_version": 1}))
        assert second.ok and second.payload is not None
        assert second.payload["tags"] == []

    def test_repaired_output_passes_the_same_validator(self) -> None:
        """AC-C02-02: repair経路の出力が、repairを経ない出力と同じvalidatorで検証される。"""
        definition = _definition(
            {"tags": Field(types=(list,), required=False, items=Field(types=(str,)), default=[])}
        )
        repaired = repair_and_validate(definition, _raw({"schema_version": 1}))
        assert repaired.ok and repaired.payload is not None
        direct = validate_object(definition, repaired.payload)
        assert direct.ok and direct.payload == repaired.payload


class TestRegistryConstruction:
    def test_duplicate_kind_is_rejected(self) -> None:
        definition = _definition({})
        with pytest.raises(ValueError):
            build_registry((definition, definition))

    def test_current_version_is_the_maximum(self) -> None:
        definition = SchemaDefinition(
            kind=SchemaKind.USER_CANCEL,
            versions={
                1: VersionSpec(fields={"schema_version": schema_version_field()}),
                2: VersionSpec(fields={"schema_version": schema_version_field()}),
            },
        )
        assert definition.current_version == 2
