# SPDX-License-Identifier: Apache-2.0
"""schema定義の形式（kind / version / migration）とvalidation pipelineの入口。

各schemaはkindごとの`SchemaDefinition`としてdataで定義される。versionはkindごとの
整数（1始まり・単調増加）で、未知versionはversion stageのvalidation errorとして
拒否する（ADR-0004）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum, unique

from .validate import (
    DEFAULT_MAX_INPUT_BYTES,
    Field,
    PublicError,
    ValidationResult,
    VersionSpec,
    apply_defaults,
    is_integer_token,
    parse_json,
    strip_bom,
    structural_errors,
)


@unique
class SchemaKind(Enum):
    """C-02が所有するschemaの種別。

    C-01の`RecordKind` 21種（canonical record）に、record化されないprotocol message
    （merge intent / merge outcome / follow-up候補と評価 / HOST_ACTION envelope /
    submit envelope / AWAIT_USER request / user-input submit / permission resume /
    session config / checkpoint envelope / PR lock file）を加えた集合。schemaをtransportや各workflowへ
    分散させないため、canonical recordの全kindをここで所有する。
    """

    # C-01のRecordKindと同名（内部record）
    REVIEW_RESULT = "REVIEW_RESULT"
    FIX_RESULT = "FIX_RESULT"
    CLARIFICATION_QUESTION = "CLARIFICATION_QUESTION"
    CLARIFICATION_ANSWER = "CLARIFICATION_ANSWER"
    DECISION_REQUEST = "DECISION_REQUEST"
    DECISION_VERDICT = "DECISION_VERDICT"
    DECISION_BRIEF = "DECISION_BRIEF"
    DECISION_RECORD = "DECISION_RECORD"
    EXTERNAL_DEPENDENCY = "EXTERNAL_DEPENDENCY"
    PERMISSION_BLOCK = "PERMISSION_BLOCK"
    CI_TIMEOUT = "CI_TIMEOUT"
    CI_CODE_FAILURE = "CI_CODE_FAILURE"
    FINAL_REPORT = "FINAL_REPORT"
    INTEGRITY_INCIDENT = "INTEGRITY_INCIDENT"
    GATE_ANSWER = "GATE_ANSWER"
    # C-01のRecordKindと同名（user-input record）
    USER_DECISION = "USER_DECISION"
    GATE_QUESTION = "GATE_QUESTION"
    GATE_CHANGES = "GATE_CHANGES"
    MERGE_APPROVAL = "MERGE_APPROVAL"
    BLOCK_INTERVENTION = "BLOCK_INTERVENTION"
    USER_CANCEL = "USER_CANCEL"
    # record化されないprotocol message
    MERGE_INTENT = "MERGE_INTENT"
    MERGE_OUTCOME = "MERGE_OUTCOME"
    FOLLOWUP_CANDIDATES = "FOLLOWUP_CANDIDATES"
    FOLLOWUP_EVALUATION = "FOLLOWUP_EVALUATION"
    FOLLOWUP_PERMISSION = "FOLLOWUP_PERMISSION"
    HOST_ACTION = "HOST_ACTION"
    SUBMIT = "SUBMIT"
    HOST_FAILURE = "HOST_FAILURE"
    USER_REQUEST = "USER_REQUEST"
    USER_SUBMIT = "USER_SUBMIT"
    PERMISSION_RESUME = "PERMISSION_RESUME"
    SESSION_CONFIG = "SESSION_CONFIG"
    AGENT_SELECTION = "AGENT_SELECTION"
    CHECKPOINT = "CHECKPOINT"
    RUN_LOCK = "RUN_LOCK"


# 損失のないpure関数によるversion間変換（v_n -> v_n+1のpayload変換。ADR-0004）
Migration = Callable[[dict[str, object]], dict[str, object]]


@dataclass(frozen=True)
class SchemaDefinition:
    """1 kindのschema定義（version別spec + migration chain）。"""

    kind: SchemaKind
    versions: Mapping[int, VersionSpec]
    migrations: Mapping[int, Migration] | None = None
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES

    @property
    def current_version(self) -> int:
        return max(self.versions)


def parse_json_object(raw: bytes, *, max_input_bytes: int) -> dict[str, object] | ValidationResult:
    """`size -> utf8 -> json -> root-object`だけを通す（**schema検証は行わない**）。

    どの定義で検証するかがpayloadの構造で決まる場合（C-08のsubmit envelope判別）に、
    guardを重複実装せず前段だけを共有するためのentry point。返すのはparse済みobjectか、
    失敗stageを持つ`ValidationResult`である。
    """
    if len(raw) > max_input_bytes:
        return ValidationResult(False, "size", (PublicError("input_too_large", "$"),), None)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ValidationResult(False, "utf8", (PublicError("invalid_utf8", "$"),), None)
    try:
        # ValueErrorはJSONDecodeErrorの親であり、整数桁数上限の超過も含めて捕捉する
        data = parse_json(text)
    except (ValueError, RecursionError):
        return ValidationResult(False, "json", (PublicError("invalid_json", "$"),), None)
    if not isinstance(data, dict):
        return ValidationResult(False, "schema", (PublicError("root_not_object", "$"),), None)
    return data


def _pipeline(
    definition: SchemaDefinition, raw: bytes, *, repair: bool
) -> ValidationResult:
    if repair:
        raw = strip_bom(raw)
    parsed = parse_json_object(raw, max_input_bytes=definition.max_input_bytes)
    if isinstance(parsed, ValidationResult):
        return parsed
    return _validate_parsed(definition, parsed, repair=repair)


def _validate_parsed(
    definition: SchemaDefinition, data: dict[str, object], *, repair: bool
) -> ValidationResult:
    version_value = data.get("schema_version")
    if is_integer_token(version_value) and version_value not in definition.versions:
        return ValidationResult(
            False, "version", (PublicError("unknown_version", "schema_version"),), None
        )
    # versionが不正・欠落の場合は現行specで検証し、schema_version fieldの違反として報告する
    version: int | None = version_value if isinstance(version_value, int) and is_integer_token(version_value) else None
    spec = definition.versions[version if version is not None else definition.current_version]
    if repair:
        data = apply_defaults(spec, data)
    errors = structural_errors(spec, data)
    if errors:
        return ValidationResult(False, "schema", errors, None)
    return ValidationResult(True, None, (), data, version=version)


def validate(definition: SchemaDefinition, raw: bytes) -> ValidationResult:
    """bytes入力を `size -> utf8 -> json -> version -> schema` のpipelineで検証する。"""
    return _pipeline(definition, raw, repair=False)


def validate_object(definition: SchemaDefinition, data: dict[str, object]) -> ValidationResult:
    """parse済みobjectを検証する（version gate + structural）。"""
    return _validate_parsed(definition, data, repair=False)


def repair_and_validate(definition: SchemaDefinition, raw: bytes) -> ValidationResult:
    """損失のないrepair（BOM除去・宣言済み既定値の補完）を適用し、同じvalidatorで検証する。

    repair経路を通った出力もrepairを経ない出力と同じstructural検証を通過する
    （AC-C02-02）。意味的fieldの捏造は行わない。
    """
    return _pipeline(definition, raw, repair=True)


# ---------------------------------------------------------------------------
# spec定義のためのfield factory（各schema moduleが使う）
# ---------------------------------------------------------------------------


def text(
    *,
    required: bool = True,
    non_empty: bool = True,
    max_len: int | None = 10_000,
    allow_none: bool = False,
) -> Field:
    """文字列field。既定で非空・10,000字上限。"""
    return Field(
        types=(str,), required=required, non_empty=non_empty, max_len=max_len, allow_none=allow_none
    )


def sha(*, required: bool = True) -> Field:
    """head SHA等のopaqueな識別子（非空文字列。C-02は形式を解釈しない）。"""
    return Field(types=(str,), required=required, non_empty=True, max_len=200)


def opaque(*, required: bool = True, allow_none: bool = False) -> Field:
    """binding / fingerprint / hash / URL等のopaque文字列（等価比較のみに使う値）。"""
    return Field(types=(str,), required=required, non_empty=True, max_len=1_000, allow_none=allow_none)


def integer(*, required: bool = True, allow_none: bool = False) -> Field:
    return Field(types=(int,), required=required, allow_none=allow_none)


def boolean(*, required: bool = True) -> Field:
    return Field(types=(bool,), required=required)


def enum_field(values: tuple[str, ...], *, required: bool = True) -> Field:
    return Field(types=(str,), required=required, enum=values)


def obj(fields: Mapping[str, Field], *, required: bool = True) -> Field:
    return Field(types=(dict,), required=required, fields=fields)


def array(items: Field, *, required: bool = True, max_items: int | None = None) -> Field:
    return Field(types=(list,), required=required, items=items, max_items=max_items)


def schema_version_field() -> Field:
    """schema_version field（整数必須。既知集合の検査はversion gateとSchemaDefinition.versionsが行う）。"""
    return Field(types=(int,))
