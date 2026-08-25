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

from ..domain._ruledefs import BlockKind, ProcedureKind
from ..domain.states import State
from ..domain.values import Awaiting, RecordKind
from .action import HOST_ACTION_KINDS, SUBMIT_ERROR_CATEGORIES, SUBMIT_OUTCOMES
from .projection import (
    COUNT_KEY,
    DIGEST_KEY,
    FINGERPRINT_KEY,
    PAYLOAD_HASH_KEY,
    RESULT_KEY,
    ROUND_KEY,
    SUBJECT_KEY,
    TARGET_KEY,
    TURN_KEY,
)
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

# receipt ledgerの上限。1 logical actionのattempt数と等しく、retry budgetがこれより大きくても
# checkpointが書けなくなる方向へは伸ばさない（C-08がこの値でretryを打ち切る）
MAX_SUBMIT_RECEIPTS = 32

_PROCEDURE_KINDS: tuple[str, ...] = tuple(sorted(kind.value for kind in ProcedureKind))
_BLOCK_KINDS: tuple[str, ...] = tuple(sorted(kind.value for kind in BlockKind))
_STATE_VALUES = tuple(sorted(state.value for state in State))
_AWAITING_VALUES = tuple(sorted(awaiting.value for awaiting in Awaiting))
_RECORD_KIND_VALUES = tuple(sorted(kind.value for kind in RecordKind))
# 未投稿recordの本文は公開本文と同じ上限（C-05のMAX_COMMENT_CHARS）まで保持する
MAX_PENDING_BODY_CHARS = 65_536


def _optional_text() -> Field:
    return text(required=False)


def _optional_sha() -> Field:
    return sha(required=False)


def _optional_opaque() -> Field:
    return opaque(required=False)


# TE 10.1の18項目を16のoptional sectionへ写像する（外枠の識別項目1を除く）
# host_actionはPhase 8（C-08）の追加: 未完了の`HOST_ACTION`と、受理済みsubmitの記録。
# 別processからのresume（AC-C08-06）が**同じactionを再提示**し、新しいactionを生成しない
# ために要る（ADR-0014）。
#
# binding 8項目だけでは、payload / verified recordsが違う2つの有効なactionが同じ保存値へ
# 潰れる。envelope全体をcanonical hashで固定し、実体はrun directory内のenvelope fileから
# 読み直す（hashが一致しなければ再提示しない）。submitも同様にenvelope全体のhashを持つ。
#
# v1は未完了actionのfieldをsection直下に置き、受理済みsubmitを**1件**だけ保持していた。
# retryはattemptごとに新しいaction ID / nonceを発行するため（ADR-0015）、過去attemptの
# 同一再送を冪等に扱うにはreceiptを**複数**保持する必要がある。v2で未完了actionを`pending`
# へ入れ、`submit`を`receipts`配列へ置き換えた（version bumpの理由）


def _violation_field() -> Field:
    """integrity violationの参照（`IntegrityEvidenceRef`と同じ3値）。"""
    return obj({"binding": opaque(), "descriptor": opaque(), "head": opaque()})


def _pending_action_fields() -> dict[str, Field]:
    """未完了`HOST_ACTION`の識別子（v1はsection直下、v2は`pending`配下に置く）。"""
    return {
        "action_id": opaque(),
        "action_kind": enum_field(HOST_ACTION_KINDS),
        "nonce": opaque(),
        "expected_head_sha": sha(),
        "result_path": text(),
        # 発行した`HOST_ACTION` envelopeの実体（run directory相対）とcanonical hash
        "envelope_path": text(),
        "envelope_hash": opaque(),
        "issued_at": _optional_text(),
    }


def _receipt_fields(*, keyed: bool) -> dict[str, Field]:
    """受理済みsubmitの記録。判定は`submit_hash`（envelope全体のcanonical hash）で行う。

    `keyed`はattemptを識別する`action_id` / `nonce`を持つか（v2のreceipt配列で必須）。
    v1はsectionが単一actionを表していたため、この2値をsection直下から引いていた。
    """
    keys: dict[str, Field] = {"action_id": opaque(), "nonce": opaque()} if keyed else {}
    return {
        **keys,
        "outcome": enum_field(SUBMIT_OUTCOMES),
        "submit_hash": opaque(),
        "result_hash": opaque(),
        # `result_kind`はeventの組み立てに、`error_category`は失敗の診断に使う
        "result_kind": enum_field(_RECORD_KIND_VALUES, required=False),
        "error_category": enum_field(SUBMIT_ERROR_CATEGORIES, required=False),
        "accepted_at": _optional_text(),
    }


def _host_action_v1() -> Field:
    return obj(
        {
            **_pending_action_fields(),
            "submit": obj(_receipt_fields(keyed=False), required=False),
        },
        required=False,
    )


def _host_action_v2() -> Field:
    """未完了actionを`pending`へ、受理済みsubmitを`receipts`配列へ分けた形（ADR-0015）。"""
    return obj(
        {
            "pending": obj(
                {
                    **_pending_action_fields(),
                    # logical action（同一作業のattempt列）と、その中での試行番号。
                    # attemptごとに新しいaction ID / nonceを発行するため、binding 8項目
                    # （plan L295）は変えずに済む
                    "correlation_id": opaque(required=False),
                    "attempt": integer(required=False),
                },
                required=False,
            ),
            # 受理済みsubmit。同一内容の再送を冪等に扱い、内容の異なる再送を止める。
            # 保持するのはlogical action 1件分のattemptだけで（fresh action発行時に
            # 入れ替える）、上限はcheckpointが常に書ける大きさへ構造的に固定する
            "receipts": array(
                obj(_receipt_fields(keyed=True)), required=False, max_items=MAX_SUBMIT_RECEIPTS
            ),
        },
        required=False,
    )


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
            # awaiting / return_to / recovery_to / pending_recordはPhase 7（C-07）の
            # additive追加: 中断点を一意に再開するためのMachineState断片（AC-C07-01 / 02）。
            # procedure / block / deferred_integrityは完全replayを行うPhaseで追加する
            # （blockはC-06のchain検証で毎回再導出するため保存しない。ADR-0011）
            "awaiting": enum_field(_AWAITING_VALUES, required=False),
            "return_to": enum_field(_STATE_VALUES, required=False),
            "recovery_to": enum_field(_STATE_VALUES, required=False),
            "pending_record": obj(
                {
                    "kind": enum_field(_RECORD_KIND_VALUES),
                    "binding": opaque(),
                    "source_state": enum_field(_STATE_VALUES),
                },
                required=False,
            ),
            # procedure / block / deferred_integrityはPhase 8 PR-2bのadditive追加。
            # C-01がintegrity検出で返す状態（halt gateとRECORD_INTEGRITY block）を
            # **そのまま読み戻せる**ようにする。読み戻せない状態を書くと、次のresumeが
            # 復元できないcheckpointになる（ADR-0017）。
            #
            # violation集合はC-06のchain検証で毎回再導出できる値だが（ADR-0011 決定8）、
            # readerは純粋関数でありGitHubへ問い合わせられない。ここに保存するのは
            # **状態復元のためのcache**であり、検出の正本は常にchain検証である
            # （保存値が古くても、再検証が違反を再び検出する）
            "procedure": obj(
                {
                    "kind": enum_field(_PROCEDURE_KINDS),
                    "attempt_binding": _optional_opaque(),
                    "violations": array(_violation_field(), required=False),
                },
                required=False,
            ),
            "block": obj(
                {
                    "kind": enum_field(_BLOCK_KINDS),
                    "violations": array(_violation_field(), required=False),
                },
                required=False,
            ),
            "deferred_integrity": array(_violation_field(), required=False),
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
    # artifact_recordsはPhase 7（C-07）のadditive追加: artifactをapproved headと
    # canonical recordへbindする（AC-C07-05）。既存`artifacts`のitem型は変えない
    # （array itemの型変更は非互換でversion bumpを要するため。ADR-0004）
    "artifact_records": array(
        obj(
            {
                "path": text(),
                "kind": text(),
                "content_hash": opaque(),
                "approved_head_sha": sha(),
                "record_binding": opaque(required=False),
                "comment_id": opaque(required=False),
            }
        ),
        required=False,
    ),
    # transactionはPhase 7（C-07）のadditive追加: 投稿前 / 投稿成否不明で中断した
    # recordを**同一key**で再発行するためのcrash window保存値（ADR-0010 決定13）。
    # bodyはredact済みのrender出力（marker付加前）だけを保持する
    "transaction": obj(
        {
            "binding": opaque(),
            "kind": enum_field(_RECORD_KIND_VALUES),
            "seq": integer(),
            "head_sha": sha(),
            "payload_hash": opaque(),
            "body": text(max_len=MAX_PENDING_BODY_CHARS),
            "body_hash": _optional_opaque(),
            # projectionはPhase 7 PR-4のadditive追加: 中断したrecordをmarkerごと
            # 再composeするために要る（同一seqで本文がbyte一致しないとC-06のseq conflictに
            # なる。ADR-0010 決定13）。keyの定義はC-02の`projection`が正本で、ここは
            # 同じ集合を宣言するだけ（objは未知keyを拒否するため列挙が要る）
            "projection": obj(
                {
                    PAYLOAD_HASH_KEY: opaque(),
                    RESULT_KEY: _optional_text(),
                    ROUND_KEY: integer(required=False),
                    TURN_KEY: integer(required=False),
                    FINGERPRINT_KEY: _optional_opaque(),
                    SUBJECT_KEY: _optional_opaque(),
                    TARGET_KEY: _optional_opaque(),
                    DIGEST_KEY: _optional_opaque(),
                    COUNT_KEY: integer(required=False),
                },
                required=False,
            ),
        },
        required=False,
    ),
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

def _envelope_fields(host_action: Field) -> dict[str, Field]:
    return {
        # 必須の外枠: 1. run ID、repository、Issue / PR番号
        "schema_version": schema_version_field(),
        "run_id": opaque(),
        "repository": text(),
        "number": integer(),
        **_SECTIONS,
        "host_action": host_action,
    }


_V1_PENDING_KEYS: tuple[str, ...] = tuple(_pending_action_fields())


def _checkpoint_v1_to_v2(payload: dict[str, object]) -> dict[str, object]:
    """`host_action`のv1形をv2形へ写す（損失なし。ADR-0004 rule 6 / ADR-0015）。

    section直下の未完了actionを`pending`へ移し、単一の`submit`を1要素の`receipts`へ
    入れる。receiptが要る`action_id` / `nonce`は**同じsection内**の値で、捏造しない。
    `host_action`を持たないcheckpointは変換対象が無い（大多数はこちら）。
    """
    section = payload.get("host_action")
    if not isinstance(section, dict):
        return payload
    migrated = dict(payload)
    pending = {key: section[key] for key in _V1_PENDING_KEYS if key in section}
    converted: dict[str, object] = {"pending": pending}
    submit = section.get("submit")
    if isinstance(submit, dict):
        converted["receipts"] = [
            {**submit, "action_id": section["action_id"], "nonce": section["nonce"]}
        ]
    migrated["host_action"] = converted
    return migrated


CHECKPOINT = SchemaDefinition(
    kind=SchemaKind.CHECKPOINT,
    max_input_bytes=CHECKPOINT_MAX_INPUT_BYTES,
    versions={
        1: VersionSpec(fields=_envelope_fields(_host_action_v1())),
        2: VersionSpec(fields=_envelope_fields(_host_action_v2())),
    },
    migrations={1: _checkpoint_v1_to_v2},
)
