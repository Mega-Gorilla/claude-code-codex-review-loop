# SPDX-License-Identifier: Apache-2.0
"""C-01が要求する残りのcanonical record kindのschema v1。

permission block（TE L570）、CI timeout / code failure（TE L422）、external dependency、
block intervention、integrity incident（Phase 1計画の節5.4）、user cancelを定義する。
implementation planのC-02節の列挙にはないが、「schemaをtransportや各workflowへ分散させない」
（同節）ため、canonical record全kindのschemaを本packageが所有する。
"""

from __future__ import annotations

from .registry import (
    SchemaDefinition,
    SchemaKind,
    array,
    integer,
    obj,
    opaque,
    schema_version_field,
    sha,
    text,
)
from .validate import PublicError, VersionSpec

PERMISSION_BLOCK = SchemaDefinition(
    kind=SchemaKind.PERMISSION_BLOCK,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                # TE L570の項目: Permission ID、tool / command、理由、risk、repository、
                # Issue / PR、head SHA、要求scope、再開方法
                "permission_id": opaque(),
                "tool": text(),
                "reason": text(),
                "risk": text(),
                "repository": text(),
                "number": integer(),
                "target_head_sha": sha(),
                "requested_scope": text(),
                "resume_command": text(),
            },
        )
    },
)

_CHECK = obj(
    {
        "check_name": text(),
        "url": opaque(required=False),
        "result": text(required=False),
    }
)

CI_TIMEOUT = SchemaDefinition(
    kind=SchemaKind.CI_TIMEOUT,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "target_head_sha": sha(),
                "checks": array(_CHECK),
                "waited_seconds": integer(),
            },
        )
    },
)

CI_CODE_FAILURE = SchemaDefinition(
    kind=SchemaKind.CI_CODE_FAILURE,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "target_head_sha": sha(),
                "checks": array(_CHECK),
                "summary": text(),
            },
        )
    },
)

EXTERNAL_DEPENDENCY = SchemaDefinition(
    kind=SchemaKind.EXTERNAL_DEPENDENCY,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "target_head_sha": sha(),
                "description": text(),
                "resume_hint": text(required=False),
            },
        )
    },
)

BLOCK_INTERVENTION = SchemaDefinition(
    kind=SchemaKind.BLOCK_INTERVENTION,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                # 解除対象のblock attempt binding（canonical本文へ含め、C-06が検証する）
                "target_block_binding": opaque(),
                "target_head_sha": sha(),
                "body": text(),
                "input_route": text(),
            },
        )
    },
)


def _rule_violations_not_empty(data: dict[str, object]) -> list[PublicError]:
    violations = data.get("violation_bindings")
    if isinstance(violations, list) and not violations:
        return [PublicError("cross_field", "violation_bindings")]
    return []


INTEGRITY_INCIDENT = SchemaDefinition(
    kind=SchemaKind.INTEGRITY_INCIDENT,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                # 記録するviolation bindingの集合（空にできない。Phase 1計画の節5.4）
                "violation_bindings": array(opaque()),
                "summary": text(),
                # cancelで未完了になったturnの監査参照
                "audit_reference": obj(
                    {"kind": text(), "binding": opaque()}, required=False
                ),
            },
            rules=(_rule_violations_not_empty,),
        )
    },
)

USER_CANCEL = SchemaDefinition(
    kind=SchemaKind.USER_CANCEL,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "target_head_sha": sha(),
                "input_route": text(),
                "reason": text(required=False),
            },
        )
    },
)
