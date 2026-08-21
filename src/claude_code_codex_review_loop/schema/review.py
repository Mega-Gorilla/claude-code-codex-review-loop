# SPDX-License-Identifier: Apache-2.0
"""review系protocolのschema v1: Codex review result / coder result / clarification。

正本はtarget experienceの5.1（review round）とL517-550（clarification protocol）。
field名とJSON形状は本moduleが確定する（正本は項目listのみを定める）。
per-finding dispositionの語彙はPhase 10（C-10）で確定するため、v1では自由記述とする。
"""

from __future__ import annotations

from .registry import (
    SchemaDefinition,
    SchemaKind,
    array,
    boolean,
    enum_field,
    integer,
    obj,
    opaque,
    schema_version_field,
    sha,
    text,
)
from .validate import Field, PublicError, VersionSpec

REVIEW_VERDICTS = ("APPROVED", "CHANGES_REQUESTED")
SEVERITIES = ("HIGH", "MEDIUM", "LOW")
# TE L530-538の5値。C-01 eventへの対応（USER_DECISION_REQUIRED -> Escalated等）はPhase 11で確定する
CLARIFICATION_RESULTS = (
    "CONFIRMED",
    "REVISED",
    "WITHDRAWN",
    "MORE_EVIDENCE_REQUIRED",
    "USER_DECISION_REQUIRED",
)

# findingの属性（問題定義・severity・scope・修正案。TE L535のREVISED対象と一致させる）
FINDING_FIELDS: dict[str, Field] = {
    "id": opaque(),
    "fingerprint": opaque(),
    "problem": text(),
    "severity": enum_field(SEVERITIES),
    "blocking": boolean(),
    "scope": text(required=False),
    "suggested_fix": text(required=False),
    "evidence": text(required=False),
    "file": text(required=False),
    "line": integer(required=False, allow_none=True),
}


def _rule_verdict_matches_blocking(data: dict[str, object]) -> list[PublicError]:
    """APPROVEDはblocking findingなし、CHANGES_REQUESTEDはblocking findingを1件以上持つ。"""
    findings = data.get("findings")
    if not isinstance(findings, list):
        return []
    has_blocking = any(isinstance(f, dict) and f.get("blocking") is True for f in findings)
    if data.get("verdict") == "APPROVED" and has_blocking:
        return [PublicError("cross_field", "verdict")]
    if data.get("verdict") == "CHANGES_REQUESTED" and not has_blocking:
        return [PublicError("cross_field", "verdict")]
    return []


REVIEW_RESULT = SchemaDefinition(
    kind=SchemaKind.REVIEW_RESULT,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "target_head_sha": sha(),
                "round": integer(),
                "verdict": enum_field(REVIEW_VERDICTS),
                "findings": array(obj(FINDING_FIELDS)),
                # 隔離checkout内で実行した検証（test / build / 再現）の記録
                "verification_runs": array(
                    obj({"command": text(), "result": text()}), required=False
                ),
            },
            rules=(_rule_verdict_matches_blocking,),
        )
    },
)

FIX_RESULT = SchemaDefinition(
    kind=SchemaKind.FIX_RESULT,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "summary": text(),
                "pre_head_sha": sha(),
                "pushed_head_sha": sha(),
                "dispositions": array(
                    obj(
                        {
                            "finding_id": opaque(),
                            # 語彙はPhase 10で確定するため、v1は自由記述
                            "disposition": text(),
                            "note": text(required=False),
                        }
                    )
                ),
                "tests": array(
                    obj(
                        {
                            "command": text(),
                            "cwd": text(required=False),
                            "result": text(),
                            "duration_ms": integer(required=False),
                        }
                    )
                ),
                "dirty_before": boolean(required=False),
                "dirty_after": boolean(required=False),
            },
        )
    },
)

CLARIFICATION_QUESTION = SchemaDefinition(
    kind=SchemaKind.CLARIFICATION_QUESTION,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "run_id": opaque(),
                "fingerprint": opaque(),
                "turn": integer(),
                "target_head_sha": sha(),
                # file / line固有findingはreview threadへのreply、cross-cuttingは返信元URL
                "reply_to": opaque(required=False),
                "target_finding": opaque(),
                "question": text(),
                "grounds": text(),
                "expected_confirmation": text(),
            },
        )
    },
)


def _rule_revised_requires_finding(data: dict[str, object]) -> list[PublicError]:
    if data.get("result") == "REVISED" and "revised_finding" not in data:
        return [PublicError("cross_field", "revised_finding")]
    return []


def _rule_more_evidence_requires_detail(data: dict[str, object]) -> list[PublicError]:
    """MORE_EVIDENCE_REQUIREDは、判断に必要な調査・test・再現条件の明示を要求する。"""
    if data.get("result") == "MORE_EVIDENCE_REQUIRED" and "required_evidence" not in data:
        return [PublicError("cross_field", "required_evidence")]
    return []


CLARIFICATION_ANSWER = SchemaDefinition(
    kind=SchemaKind.CLARIFICATION_ANSWER,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "run_id": opaque(),
                "fingerprint": opaque(),
                "turn": integer(),
                "target_head_sha": sha(),
                "result": enum_field(CLARIFICATION_RESULTS),
                "rationale": text(),
                "evidence": text(required=False),
                "revised_finding": obj(FINDING_FIELDS, required=False),
                "required_evidence": text(required=False),
            },
            rules=(_rule_revised_requires_finding, _rule_more_evidence_requires_detail),
        )
    },
)
