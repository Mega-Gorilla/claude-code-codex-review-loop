# SPDX-License-Identifier: Apache-2.0
"""`AWAIT_USER`搬送路のschema: request envelope / user-input submit / permission resume。

正本はimplementation planのSection 2（`advance -> HOST_ACTION | AWAIT_USER | TERMINAL`）と
target experienceの5.5（merge gate interaction）、ADR-0018。

`HOST_ACTION`用の`SUBMIT` v2は**変更しない**。ユーザー入力は同じsubmit操作の別variantとして
`USER_SUBMIT`で戻す（envelopeの判別はC-08が`action_id` / `request_id`の排他で行う）。

**待機の識別子は直和である**（ADR-0023）。`AWAIT_USER`の待機は`awaiting`（C-01が何の入力を
待っているか）で識別するが、`BLOCKED`での介入待ちに`awaiting`は無く、識別子は**block attempt
binding**である。そこで`awaiting`と`block_binding`を排他のdiscriminatorとして持ち、
**どちらか一方だけ**を要求する（両方・どちらも無しはfail closed）。

**intent語彙を新設しない**。merge gateの4 intentは`schema/merge.py`の`MERGE_INTENTS`として、
判断回答は`USER_DECISION`として既に存在する。ここが持つのは搬送路の形だけで、hostが返す
結果payloadは**既存のrecord schemaをそのまま再利用**する（ADR-0014 決定4と同じ規則）。
"""

from __future__ import annotations

from typing import Final

from ..domain.values import USER_INPUT_RECORD_KINDS, Awaiting
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

# ユーザー入力を待つawaiting（C-01の3値。`AWAITING_COMMANDS`が空tupleの行）
USER_INPUT_AWAITINGS: Final = (
    Awaiting.USER_INPUT_DECISION,
    Awaiting.USER_INPUT_GATE,
    Awaiting.USER_INPUT_PERMISSION,
)
_AWAITING_VALUES: Final = tuple(sorted(awaiting.value for awaiting in USER_INPUT_AWAITINGS))

# user-input recordの種別（C-01の`USER_INPUT_RECORD_KINDS`が正本）
_USER_RECORD_KINDS: Final = tuple(sorted(kind.value for kind in USER_INPUT_RECORD_KINDS))

# 入力経路の語彙。`USER_DECISION`等のschemaが`input_route`へ持つ値の値域で、
# 「C-08 / C-06で確定」という留保をここで閉じる（ADR-0018 決定3）。
# - host_transcript: 対話型sessionの入力をC-08がintentへ構造化し、canonical recordへ転記した
# - github_comment: ユーザーがGitHubへ直接投稿し、C-06がexternal evidenceとして受理した
HOST_TRANSCRIPT_ROUTE: Final = "host_transcript"
GITHUB_COMMENT_ROUTE: Final = "github_comment"
INPUT_ROUTES: Final = (GITHUB_COMMENT_ROUTE, HOST_TRANSCRIPT_ROUTE)


def _rule_exactly_one_wait(data: dict[str, object]) -> list[PublicError]:
    """待機の識別子は`awaiting`か`block_binding`の**どちらか一方**だけ（ADR-0023 決定1）。

    `AWAIT_USER`はC-01のawaitingで、`BLOCKED`での介入待ちはblock attempt bindingで識別する。
    両方あるenvelopeはどちらの待機を指すのか決まらず、どちらも無いenvelopeは何の待機かが
    決まらない。どちらも**推測せずに拒否する**。

    `awaiting`をoptionalへ緩めた分は、このruleが構造で締める。
    """
    has_awaiting = "awaiting" in data
    has_block = "block_binding" in data
    if has_awaiting == has_block:
        return [PublicError("cross_field", "awaiting")]
    return []


USER_REQUEST = SchemaDefinition(
    kind=SchemaKind.USER_REQUEST,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "run_id": opaque(),
                # 1 requestを一意に指す識別子（`HOST_ACTION`のaction IDと同じ役割）
                "request_id": opaque(),
                "repository": text(),
                "number": integer(),
                "expected_head_sha": sha(),
                # 待機の識別子（排他。`_rule_exactly_one_wait`が一方だけを要求する）
                "awaiting": enum_field(_AWAITING_VALUES, required=False),
                # `BLOCKED`での介入待ちの識別子: 解除対象のblock attempt binding
                "block_binding": opaque(required=False),
                # submitを一度だけconsumeするone-time nonce
                "nonce": opaque(),
                # Controllerがrun directory内へ払い出す結果path（任意pathを受理しない）
                "result_path": text(),
                # awaiting instanceの識別子: request発行時点のchain最大seq。両経路が
                # checkpointから導出できる値であり、intent keyの構成要素になる
                "since_seq": integer(),
                # 当該awaitingで受理するrecord種別（registryの写し。hostへの提示用）
                "accepted_result_kinds": array(enum_field(_USER_RECORD_KINDS)),
                # 判断の根拠として同梱する検証済みrecord（AC-C08-07と同型）
                "verified_records": array(obj({"comment_id": opaque(), "head_sha": sha()})),
            },
            rules=(_rule_exactly_one_wait,),
        )
    },
)


def _rule_result_kind_or_permission(data: dict[str, object]) -> list[PublicError]:
    """`result_kind`が無いsubmitは`USER_INPUT_PERMISSION`に限る。

    tool permissionの明示resumeだけがrecordを作らない応答である（C-01の
    `PermissionResumeValidated`はpendingを要求しない）。他のawaitingでrecord種別を
    省略できると、何のrecordを作るかを決めずに状態を進める経路ができる。
    """
    if "result_kind" in data:
        return []
    if data.get("awaiting") == Awaiting.USER_INPUT_PERMISSION.value:
        return []
    return [PublicError("cross_field", "result_kind")]


USER_SUBMIT = SchemaDefinition(
    kind=SchemaKind.USER_SUBMIT,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                # `USER_REQUEST`のbinding echo（一致しないsubmitはC-08が拒否する）
                "run_id": opaque(),
                "request_id": opaque(),
                # 待機の識別子（`USER_REQUEST`と同じ排他。echoして一致を照合する）
                "awaiting": enum_field(_AWAITING_VALUES, required=False),
                "block_binding": opaque(required=False),
                "expected_head_sha": sha(),
                "nonce": opaque(),
                # 結果はControllerが払い出したrun directory内のfileで受け渡し、hashで照合する
                "result_hash": opaque(),
                # 作るrecordの種別。不在はpermission resume（recordを作らない）を意味する
                "result_kind": enum_field(_USER_RECORD_KINDS, required=False),
            },
            rules=(_rule_exactly_one_wait, _rule_result_kind_or_permission),
        )
    },
)


PERMISSION_RESUME = SchemaDefinition(
    kind=SchemaKind.PERMISSION_RESUME,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                # `identity.ResumeRequest`と1対1（停止点との完全一致をC-06が判定する）
                "permission_id": opaque(),
                "tool": text(),
                "scope": text(),
                "current_head_sha": sha(),
            },
        )
    },
)
