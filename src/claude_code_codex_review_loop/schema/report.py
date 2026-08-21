# SPDX-License-Identifier: Apache-2.0
"""final reportのschema v1。

正本はtarget experienceのSection 11（L726-759）。schema検証済みJSONを正とし、
Markdown / PR comment / terminal summaryはここから決定論的にrenderされる（C-12）。
languageの対応値検証はC-12の設定解決が行う（未対応値はそこでvalidation error）。
"""

from __future__ import annotations

from .registry import (
    SchemaDefinition,
    SchemaKind,
    array,
    boolean,
    integer,
    obj,
    opaque,
    schema_version_field,
    sha,
    text,
)
from .validate import PublicError, VersionSpec

# final reportは長文になるため入力上限を広げる（P-005: 本文はfile経由で受け渡す）
REPORT_MAX_INPUT_BYTES = 262_144


def _rule_report_is_pre_merge(data: dict[str, object]) -> list[PublicError]:
    """final reportはmerge前の記録であり、merged=false（未mergeの明示）を要求する。"""
    if data.get("merged") is not False:
        return [PublicError("cross_field", "merged")]
    return []


FINAL_REPORT = SchemaDefinition(
    kind=SchemaKind.FINAL_REPORT,
    max_input_bytes=REPORT_MAX_INPUT_BYTES,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "language": text(max_len=50),
                "summary": text(),
                "why": text(),
                "user_visible_changes": array(text()),
                "acceptance_criteria": array(
                    obj({"criterion": text(), "result": text(), "evidence": text()})
                ),
                "review_history": array(
                    obj(
                        {
                            "round": integer(),
                            "findings": array(
                                obj(
                                    {
                                        "finding_id": opaque(required=False),
                                        "description": text(),
                                        "resolution": text(),
                                    }
                                )
                            ),
                        }
                    )
                ),
                "approved_head_sha": sha(),
                "local_tests": array(obj({"command": text(), "result": text()})),
                "ci_results": array(
                    obj({"check_name": text(), "result": text(), "url": opaque(required=False)})
                ),
                "remaining_risks": array(text()),
                "followups": array(
                    obj(
                        {
                            "candidate_id": opaque(),
                            "title": text(),
                            "codex_evaluation": text(),
                            "permission_status": text(),
                            "created_issue_url": opaque(required=False),
                        }
                    )
                ),
                "pre_merge_checks": array(text()),
                # merge前の記録であることを明示する（merge完了の追記recordはMERGE_OUTCOME）
                "merged": boolean(),
            },
            rules=(_rule_report_is_pre_merge,),
        )
    },
)
