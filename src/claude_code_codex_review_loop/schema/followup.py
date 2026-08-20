# SPDX-License-Identifier: Apache-2.0
"""Approved follow-upのschema v1: 候補draft / Codex評価 / permission状態。

正本はtarget experienceのL502-515（D-024）。候補は最大3件。許可は候補と本文hashへ
bindし、意味的内容の変更で失効する（本文hash・fingerprintの算出はC-06 / C-11が確定）。
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
from .validate import PublicError, VersionSpec

FOLLOWUP_VERDICTS = ("CREATE_ISSUE", "SUMMARY_ONLY", "LINK_EXISTING", "REVISE_AND_RESUBMIT")
# ユーザーの選択肢（TE L509）+ 未回答（未回答はmergeを止めない。TE L512）
FOLLOWUP_PERMISSION_STATES = ("APPROVED", "REPORT_ONLY", "REVISE", "UNANSWERED")

FOLLOWUP_MAX_CANDIDATES = 3

FOLLOWUP_CANDIDATES = SchemaDefinition(
    kind=SchemaKind.FOLLOWUP_CANDIDATES,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "repository": text(),
                "number": integer(),
                "target_head_sha": sha(),
                "candidates": array(
                    obj(
                        {
                            "candidate_id": opaque(),
                            "title": text(),
                            "background": text(),
                            "scope": text(),
                            "out_of_scope": text(),
                            "acceptance_criteria": array(text()),
                        }
                    ),
                    max_items=FOLLOWUP_MAX_CANDIDATES,
                ),
            },
        )
    },
)


def _rule_link_existing_requires_issue(data: dict[str, object]) -> list[PublicError]:
    evaluations = data.get("evaluations")
    if not isinstance(evaluations, list):
        return []
    errors: list[PublicError] = []
    for i, item in enumerate(evaluations):
        if isinstance(item, dict) and item.get("verdict") == "LINK_EXISTING" and "existing_issue" not in item:
            errors.append(PublicError("cross_field", f"evaluations[{i}].existing_issue"))
    return errors


FOLLOWUP_EVALUATION = SchemaDefinition(
    kind=SchemaKind.FOLLOWUP_EVALUATION,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "target_head_sha": sha(),
                "evaluations": array(
                    obj(
                        {
                            "candidate_id": opaque(),
                            "verdict": enum_field(FOLLOWUP_VERDICTS),
                            "reason": text(),
                            "existing_issue": opaque(required=False),
                        }
                    ),
                    max_items=FOLLOWUP_MAX_CANDIDATES,
                ),
            },
            rules=(_rule_link_existing_requires_issue,),
        )
    },
)

FOLLOWUP_PERMISSION = SchemaDefinition(
    kind=SchemaKind.FOLLOWUP_PERMISSION,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "candidate_id": opaque(),
                "candidate_fingerprint": opaque(),
                "body_hash": opaque(),
                "status": enum_field(FOLLOWUP_PERMISSION_STATES),
                "input_route": text(required=False),
                "approval_comment_id": opaque(required=False),
                "created_issue_url": opaque(required=False),
                # Issue作成APIが失敗した場合の失敗理由と再開方法（TE L513）
                "failure_reason": text(required=False),
                "resume_hint": text(required=False),
            },
        )
    },
)
