# SPDX-License-Identifier: Apache-2.0
"""merge gateのschema v1: intent / 承認record / merge完了record / gate会話record。

正本はtarget experienceの5.5（L259-287）、merge承認gate（L552-556）、入力形式（L583-589）。
承認recordの必須項目はTE L276の8項目（記録時刻を含む。AC-C13-08は記録時刻を列挙しないが、
gold documentであるtarget experienceを優先する）。時刻はopaque文字列でC-02は解釈しない。
"""

from __future__ import annotations

from .registry import (
    SchemaDefinition,
    SchemaKind,
    boolean,
    enum_field,
    integer,
    opaque,
    schema_version_field,
    sha,
    text,
)
from .validate import PublicError, VersionSpec

MERGE_INTENTS = ("QUESTION", "REQUEST_CHANGES", "APPROVE_MERGE", "CANCEL")
MERGE_METHODS = ("merge", "squash", "rebase")


def _rule_question_and_changes_have_body(data: dict[str, object]) -> list[PublicError]:
    if data.get("intent") in ("QUESTION", "REQUEST_CHANGES") and "body" not in data:
        return [PublicError("cross_field", "body")]
    return []


MERGE_INTENT = SchemaDefinition(
    kind=SchemaKind.MERGE_INTENT,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "intent": enum_field(MERGE_INTENTS),
                "repository": text(),
                "pr_number": integer(),
                "target_head_sha": sha(),
                "input_route": text(),
                "body": text(required=False),
            },
            rules=(_rule_question_and_changes_have_body,),
        )
    },
)

MERGE_APPROVAL = SchemaDefinition(
    kind=SchemaKind.MERGE_APPROVAL,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "repository": text(),
                "pr_number": integer(),
                "approved_head_sha": sha(),
                "merge_method": enum_field(MERGE_METHODS),
                # 構造化intent（承認recordは常にAPPROVE_MERGE）
                "intent": enum_field(("APPROVE_MERGE",)),
                "input_route": text(),
                "recorded_at": opaque(),
                "comment_id": opaque(),
            },
        )
    },
)

MERGE_OUTCOME = SchemaDefinition(
    kind=SchemaKind.MERGE_OUTCOME,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "repository": text(),
                "pr_number": integer(),
                "approved_head_sha": sha(),
                "merged_commit_sha": sha(),
                "merge_method": enum_field(MERGE_METHODS),
                "approval_record_url": opaque(),
                # GitHub上で再確認した結果（成否と詳細）
                "verified_on_github": boolean(),
                "verification_detail": text(required=False),
            },
        )
    },
)

GATE_QUESTION = SchemaDefinition(
    kind=SchemaKind.GATE_QUESTION,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "target_head_sha": sha(),
                "body": text(),
                "input_route": text(),
            },
        )
    },
)

GATE_ANSWER = SchemaDefinition(
    kind=SchemaKind.GATE_ANSWER,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "target_head_sha": sha(),
                "body": text(),
                "reply_to": opaque(required=False),
            },
        )
    },
)

GATE_CHANGES = SchemaDefinition(
    kind=SchemaKind.GATE_CHANGES,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "target_head_sha": sha(),
                "body": text(),
                "input_route": text(),
            },
        )
    },
)
