# SPDX-License-Identifier: Apache-2.0
"""active host protocolのschema v1: HOST_ACTION envelopeとsubmit envelope。

正本はimplementation planのSection 2（active host protocol）とC-08節（L295のbinding 8項目、
AC-C08-05 / 07）。**action kindの一覧と本envelopeの最終形状はPhase 8（C-08）で確定する**。
v1はC-01のHostAction 6値とimplementation planの初期案を合わせた暫定enumを置き、
確定時はadditive変更またはversion bumpで追従する。
"""

from __future__ import annotations

from .registry import (
    SchemaDefinition,
    SchemaKind,
    array,
    enum_field,
    integer,
    obj,
    opaque,
    schema_version_field,
    sha,
    text,
)
from .validate import Field, PublicError, VersionSpec

# 暫定: C-01のHostAction 6値 + implementation plan初期案の6値（Phase 8で確定）
HOST_ACTION_KINDS = (
    "APPLY_FINDINGS",
    "DRAFT_DECISION_REQUEST",
    "DRAFT_DECISION_BRIEF",
    "RECORD_DECISION",
    "REVISE_DECISION_REQUEST",
    "ANSWER_GATE_QUESTION",
    "ASK_CLARIFICATION",
    "ANSWER_CLARIFICATION",
    "DRAFT_FOLLOWUP_CANDIDATES",
    "STRUCTURE_USER_INTENT",
    "RUN_LOCAL_TESTS",
    "IMPLEMENT_ISSUE",
)

SUBMIT_OUTCOMES = ("COMPLETED", "FAILED")

HOST_ACTION = SchemaDefinition(
    kind=SchemaKind.HOST_ACTION,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                # binding 8項目（implementation plan L295）
                "run_id": opaque(),
                "action_id": opaque(),
                "action_kind": enum_field(HOST_ACTION_KINDS),
                "repository": text(),
                "number": integer(),
                "expected_head_sha": sha(),
                "payload_hash": opaque(),
                # submitを一度だけconsumeするone-time nonce
                "nonce": opaque(),
                # 検証済みrecordのcomment IDと対象head SHA（AC-C08-07）
                "verified_records": array(
                    obj({"comment_id": opaque(), "head_sha": sha()})
                ),
                # action固有payload。内部形状はPhase 8で確定するためv1ではopaque object
                "payload": Field(types=(dict,)),
            },
        )
    },
)


def _rule_failed_requires_error_category(data: dict[str, object]) -> list[PublicError]:
    if data.get("outcome") == "FAILED" and "error_category" not in data:
        return [PublicError("cross_field", "error_category")]
    return []


SUBMIT = SchemaDefinition(
    kind=SchemaKind.SUBMIT,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                # HOST_ACTION envelopeのbinding echo（一致しないsubmitはC-08が拒否する）
                "run_id": opaque(),
                "action_id": opaque(),
                "action_kind": enum_field(HOST_ACTION_KINDS),
                "expected_head_sha": sha(),
                "nonce": opaque(),
                # resultはControllerが払い出したrun directory内のfileで受け渡し、hashで照合する
                "result_hash": opaque(),
                "outcome": enum_field(SUBMIT_OUTCOMES),
                "error_category": text(required=False),
            },
            rules=(_rule_failed_requires_error_category,),
        )
    },
)
