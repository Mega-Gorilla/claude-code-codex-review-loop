# SPDX-License-Identifier: Apache-2.0
"""checkpoint envelopeのschema v1。

正本はtarget experienceの10.1「保存するcheckpoint」（18項目）とimplementation planの
「構造を追加して採用」（deviations表）・Phase別field追加予定（Phase 5〜16）。

構造（ADR-0004）: 必須の外枠（schema_version / run_id / repository / number）と、
全section optionalの本体。各sectionの内側fieldもoptionalで、**fieldはそれを利用する
Phaseと同じPhaseで追加する**。既存sectionへのoptional field追加はversionを上げない
additive変更とし、非互換な変更のみversionをbumpしてmigrationを登録する。
"""

from __future__ import annotations

from ..domain.states import State
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
from .validate import Field, VersionSpec

# checkpointは複数sectionを持つため入力上限を広げる
CHECKPOINT_MAX_INPUT_BYTES = 262_144

_STATE_VALUES = tuple(sorted(state.value for state in State))


def _optional_text() -> Field:
    return text(required=False)


def _optional_sha() -> Field:
    return sha(required=False)


def _optional_opaque() -> Field:
    return opaque(required=False)


# TE 10.1の18項目を16のoptional sectionへ写像する（外枠の識別項目1を除く）
_SECTIONS: dict[str, Field] = {
    # 2. base / observed / approved head SHA
    "heads": obj(
        {
            "base_sha": _optional_sha(),
            "observed_sha": _optional_sha(),
            "approved_sha": _optional_sha(),
        },
        required=False,
    ),
    # 3. state、round、agent role、session ID
    "state": obj(
        {
            "state": enum_field(_STATE_VALUES, required=False),
            "round": integer(required=False),
            "agent_role": _optional_text(),
            "session_id": _optional_opaque(),
        },
        required=False,
    ),
    # 4. finding ledgerとresolution
    "ledger": obj(
        {
            "findings": Field(
                types=(list,),
                required=False,
                items=obj(
                    {
                        "id": opaque(),
                        "fingerprint": _optional_opaque(),
                        "resolution": _optional_text(),
                    }
                ),
            ),
        },
        required=False,
    ),
    # 5. GitHub conversation cursorと投稿record群
    "conversation": obj(
        {
            "cursor": _optional_opaque(),
            # high_water_markはPhase 6（C-06）のadditive追加: 確認済み最大sequence `N`
            # （AC-C06-07。既知recordの再構成はseq付きrecords entryから行う）
            "high_water_mark": integer(required=False),
            "records": Field(
                types=(list,),
                required=False,
                items=obj(
                    {
                        "comment_id": opaque(),
                        # review_id / thread_idはPhase 5（C-05）のadditive追加（ADR-0004。
                        # thread操作の再開用にcomment IDと種別を分離して保持する）
                        "review_id": _optional_opaque(),
                        "thread_id": _optional_opaque(),
                        # seq / kindはPhase 6（C-06）のadditive追加: chain checkpointの
                        # 既知record（KnownRecord）をseq付きentryから再構成する
                        "seq": integer(required=False),
                        "kind": _optional_text(),
                        "url": _optional_opaque(),
                        "reply_to": _optional_opaque(),
                        "body_hash": _optional_opaque(),
                        "author_role": _optional_text(),
                        "head_sha": _optional_sha(),
                    }
                ),
            ),
        },
        required=False,
    ),
    # 6. clarification counterとfingerprint
    "clarification": obj(
        {
            "counter": integer(required=False),
            "fingerprint": _optional_opaque(),
        },
        required=False,
    ),
    # 7. coder実行前後のHEAD、dirty status、push後head
    "coder": obj(
        {
            "pre_head_sha": _optional_sha(),
            "post_head_sha": _optional_sha(),
            "pushed_head_sha": _optional_sha(),
            "dirty_before": boolean(required=False),
            "dirty_after": boolean(required=False),
        },
        required=False,
    ),
    # 8. test command、cwd、result、duration
    "tests": array(
        obj(
            {
                "command": text(),
                "cwd": _optional_text(),
                "result": text(),
                "duration_ms": integer(required=False),
            }
        ),
        required=False,
    ),
    # 9. GitHub check名、result、URL
    "ci": array(
        obj({"check_name": text(), "result": text(), "url": _optional_opaque()}),
        required=False,
    ),
    # 10. artifactとlogへのpath
    "artifacts": array(text(), required=False),
    # 11. 最後に成功したGitHub mutation、idempotency marker、read-after-write確認結果
    "mutation": obj(
        {
            "last_success": _optional_opaque(),
            "idempotency_marker": _optional_opaque(),
            "read_after_write_verified": boolean(required=False),
        },
        required=False,
    ),
    # 12. error category、再開可能地点、推奨resume command
    "error": obj(
        {
            "category": _optional_text(),
            "resume_point": _optional_text(),
            "resume_command": _optional_text(),
        },
        required=False,
    ),
    # 13. 未解決decision request、両者の意見、ユーザー回答、回答時head
    "decision": obj(
        {
            "decision_id": _optional_opaque(),
            "claude_position": _optional_text(),
            "codex_position": _optional_text(),
            "user_answer": _optional_text(),
            "answer_head_sha": _optional_sha(),
            # answer_comment_id / answer_body_hashはPhase 6（C-06）のadditive追加:
            # GitHub直接comment経路（external evidence）の失効照合用（ADR-0008）
            "answer_comment_id": _optional_opaque(),
            "answer_body_hash": _optional_opaque(),
        },
        required=False,
    ),
    # 14. Approved follow-upの追跡情報
    "followup": array(
        obj(
            {
                "candidate_id": opaque(),
                "fingerprint": _optional_opaque(),
                "body_hash": _optional_opaque(),
                "dedupe_result": _optional_text(),
                "verdict": _optional_text(),
                "permission_record": _optional_opaque(),
                "created_issue_url": _optional_opaque(),
            }
        ),
        required=False,
    ),
    # 15. permission mode / profile、Permission ID、blockされたtool等
    "permission": obj(
        {
            "mode": _optional_text(),
            "profile": _optional_text(),
            "permission_id": _optional_opaque(),
            "blocked_tool": _optional_text(),
            "risk": _optional_text(),
            "requested_scope": _optional_text(),
            "user_changes": _optional_text(),
            "resume_checkpoint": _optional_opaque(),
            # head_shaはPhase 6（C-06）のadditive追加: resume gateがPermission IDと
            # 併せて再検証するblock時のhead（AC-C06-04）
            "head_sha": _optional_sha(),
        },
        required=False,
    ),
    # 16. reviewer隔離checkoutと実行profile
    "reviewer": obj(
        {
            "checkout_path": _optional_text(),
            "sandbox_profile": _optional_text(),
            "network_profile": _optional_text(),
            "executed": _optional_text(),
            "dirty_before": boolean(required=False),
            "dirty_after": boolean(required=False),
            "discard_result": _optional_text(),
        },
        required=False,
    ),
    # 17-18. merge gate intentとmerge実行の記録
    "merge": obj(
        {
            "intent": _optional_text(),
            "pr_number": integer(required=False),
            "approved_head_sha": _optional_sha(),
            "input_route": _optional_text(),
            "approval_comment_id": _optional_opaque(),
            # approval_body_hash / candidate_fingerprint / approval_bindingはPhase 6
            # （C-06）のadditive追加: 承認bind情報（D-031。編集・削除・head変更の失効照合用）
            "approval_body_hash": _optional_opaque(),
            "candidate_fingerprint": _optional_opaque(),
            "approval_binding": _optional_opaque(),
            "merge_method": _optional_text(),
            "api_result": _optional_text(),
            "merged_commit_sha": _optional_sha(),
            "verified_result": _optional_text(),
        },
        required=False,
    ),
}

CHECKPOINT = SchemaDefinition(
    kind=SchemaKind.CHECKPOINT,
    max_input_bytes=CHECKPOINT_MAX_INPUT_BYTES,
    versions={
        1: VersionSpec(
            fields={
                # 必須の外枠: 1. run ID、repository、Issue / PR番号
                "schema_version": schema_version_field(),
                "run_id": opaque(),
                "repository": text(),
                "number": integer(),
                **_SECTIONS,
            },
        )
    },
)
