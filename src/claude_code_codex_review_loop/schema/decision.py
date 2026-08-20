# SPDX-License-Identifier: Apache-2.0
"""decision flowのschema v1: request / verdict / brief / decision record / user decision。

正本はtarget experienceのL437-500（実装中のユーザー判断フロー、D-010）。briefの必須9項目は
L461-473の表と一致させる。入力経路の語彙はC-08 / C-06（Phase 8 / 6）で確定するため、
v1では非空文字列とする。
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
from .validate import PublicError, VersionSpec

DECISION_VERDICTS = ("ASK_USER", "PROCEED_WITH_RECORD", "REVISE_AND_RESUBMIT")


def _rule_candidates_not_empty(data: dict[str, object]) -> list[PublicError]:
    candidates = data.get("candidates")
    if isinstance(candidates, list) and not candidates:
        return [PublicError("cross_field", "candidates")]
    return []


DECISION_REQUEST = SchemaDefinition(
    kind=SchemaKind.DECISION_REQUEST,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "target_head_sha": sha(),
                "reason": text(),
                "constraints": text(),
                "candidates": array(
                    obj(
                        {
                            "content": text(),
                            "pros": text(),
                            "cons": text(),
                            "impact": text(),
                        }
                    )
                ),
                "recommendation": text(),
            },
            rules=(_rule_candidates_not_empty,),
        )
    },
)

DECISION_VERDICT = SchemaDefinition(
    kind=SchemaKind.DECISION_VERDICT,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "target_head_sha": sha(),
                "verdict": enum_field(DECISION_VERDICTS),
                "rationale": text(),
                # 問題定義への修正、追加候補、riskの指摘（TE L459）
                "assessment": text(required=False),
                "fingerprint": opaque(required=False),
            },
        )
    },
)


def _rule_brief_has_recommended(data: dict[str, object]) -> list[PublicError]:
    """番号付き候補の少なくとも1件がRecommendedを持つ（TE L472-473）。"""
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return []
    if not any(isinstance(c, dict) and c.get("recommended") is True for c in candidates):
        return [PublicError("cross_field", "candidates")]
    return []


DECISION_BRIEF = SchemaDefinition(
    kind=SchemaKind.DECISION_BRIEF,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "decision_id": opaque(),
                "target_head_sha": sha(),
                "question": text(),
                "constraints_and_impact": text(),
                "candidates": array(
                    obj(
                        {
                            "number": integer(),
                            "content": text(),
                            "pros": text(),
                            "cons": text(),
                            "risk": text(required=False),
                            "impact_if_deferred": text(required=False),
                            "recommended": boolean(),
                        }
                    )
                ),
                "claude_position": text(),
                "codex_review": text(),
                # 一致しない点を省略せず表示する（一致する場合もその旨を明記する）
                "disagreements": text(),
                "recommendation": text(),
                "how_to_answer": text(),
            },
            rules=(_rule_candidates_not_empty, _rule_brief_has_recommended),
        )
    },
)

DECISION_RECORD = SchemaDefinition(
    kind=SchemaKind.DECISION_RECORD,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "decision_id": opaque(),
                "target_head_sha": sha(),
                "considered": text(),
                "initial_position": text(),
                "verdict": enum_field(DECISION_VERDICTS),
                "verdict_rationale": text(),
                "adopted_implementation": text(),
            },
        )
    },
)

USER_DECISION = SchemaDefinition(
    kind=SchemaKind.USER_DECISION,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "decision_id": opaque(),
                "target_head_sha": sha(),
                "answer": text(),
                # 入力経路の語彙はC-08 / C-06で確定（terminal転記 / GitHub直接comment）
                "input_route": text(),
            },
        )
    },
)
