# SPDX-License-Identifier: Apache-2.0
"""active host protocolのschema: `HOST_ACTION` envelopeとsubmit envelope（v2）。

正本はimplementation planのSection 2（active host protocol）とC-08節（L295のbinding 8項目、
AC-C08-05 / 07）、およびaction registryのADR-0014。

**v2でaction kindを確定した**（Phase 8）。v1は「C-01のHostAction 6値 + implementation plan
Section 2.3の残り6」を並べた暫定enumだったが、C-01が発行するのは6値だけで、残りは到達
不能だった。v2では:

- `action_kind`の値域を**C-01の`HostAction`から導出**する（二重定義を持たない）
- Controllerが払い出す`result_path`を**必須**にする（呼び出し側から任意pathを受理しない）
- actionごとの**入力payload**をschemaとして宣言し、cross-field ruleで検証する

enumの縮小と必須fieldの追加はいずれも非互換変更のため、ADR-0004 rule 2に従いversionを
bumpした。**v1 -> v2のmigrationは登録しない**: `result_path`は捏造できず（rule 6）、損失の
ない変換が存在しないため、v1入力は`migration_unavailable`の構造化error（rule 8）になる。
v1 envelopeを生成したcodeは存在しないため実害はない。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from ..domain.commands import HostAction
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
from .validate import Field, PublicError, VersionSpec, structural_errors

# 現行（v2）のaction kind。C-01のHostActionが正本で、registryはADR-0014
HOST_ACTION_KINDS: Final = tuple(sorted(action.value for action in HostAction))

# v1の暫定enum（歴史的な値域。migrationは登録しない）
_V1_ACTION_KINDS: Final = (
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

# actionごとの入力payload。**そのactionを実行するために必要な識別子だけ**を持ち、成果物の
# 形はresult schema（既存のrecord schema）が定める。追加が必要になった場合、optional field
# の追加はadditive変更でversionを上げない（ADR-0004 rule 2）
HOST_ACTION_PAYLOADS: Final[Mapping[str, VersionSpec]] = {
    # 対象roundと、disposition対象のblocking finding（結果のFIX_RESULTが参照する）
    HostAction.APPLY_FINDINGS.value: VersionSpec(
        fields={"round": integer(), "finding_ids": array(opaque())}
    ),
    # 判断が必要になった文脈はactive host自身のsessionにあり、追加の入力を要さない
    HostAction.DRAFT_DECISION_REQUEST.value: VersionSpec(fields={}),
    # 以下は対象decisionの識別子が要る（結果のschemaがdecision_idを必須にする）
    HostAction.REVISE_DECISION_REQUEST.value: VersionSpec(fields={"decision_id": opaque()}),
    HostAction.DRAFT_DECISION_BRIEF.value: VersionSpec(fields={"decision_id": opaque()}),
    HostAction.RECORD_DECISION.value: VersionSpec(fields={"decision_id": opaque()}),
    # 回答対象のgate質問（結果のGATE_ANSWERがreply_toとして参照する）
    HostAction.ANSWER_GATE_QUESTION.value: VersionSpec(fields={"question_comment_id": opaque()}),
}


def _rule_payload_matches_kind(data: dict[str, object]) -> list[PublicError]:
    """`payload`を`action_kind`に対応するschemaで検証する（pathは`payload.`配下へ写す）。

    `action_kind` / `payload`自体の型違反はfield検証が報告済みなので、ここでは扱わない。
    """
    kind = data.get("action_kind")
    payload = data.get("payload")
    if not isinstance(kind, str) or not isinstance(payload, dict):
        return []
    spec = HOST_ACTION_PAYLOADS.get(kind)
    if spec is None:
        return []  # 未知kindはenum検証が報告済み
    return [
        PublicError(error.code, f"payload.{error.path}")
        for error in structural_errors(spec, {str(key): value for key, value in payload.items()})
    ]


def _binding_fields() -> dict[str, Field]:
    """binding 8項目（implementation plan L295）+ AC-C08-07のverified records。"""
    return {
        "schema_version": schema_version_field(),
        "run_id": opaque(),
        "action_id": opaque(),
        "repository": text(),
        "number": integer(),
        "expected_head_sha": sha(),
        "payload_hash": opaque(),
        # submitを一度だけconsumeするone-time nonce
        "nonce": opaque(),
        # 検証済みrecordのcomment IDと対象head SHA（AC-C08-07）
        "verified_records": array(obj({"comment_id": opaque(), "head_sha": sha()})),
    }


HOST_ACTION = SchemaDefinition(
    kind=SchemaKind.HOST_ACTION,
    versions={
        1: VersionSpec(
            fields={
                **_binding_fields(),
                "action_kind": enum_field(_V1_ACTION_KINDS),
                # v1ではaction固有payloadの内部形状が未確定だった
                "payload": Field(types=(dict,)),
            },
        ),
        2: VersionSpec(
            fields={
                **_binding_fields(),
                "action_kind": enum_field(HOST_ACTION_KINDS),
                # Controllerがrun directory内へ払い出すresult path（呼び出し側の任意pathを
                # 受理しない）。canonical path・所有者権限・size limitの検証はC-08が行う
                "result_path": text(),
                "payload": Field(types=(dict,)),
            },
            rules=(_rule_payload_matches_kind,),
        ),
    },
)


def _rule_failed_requires_error_category(data: dict[str, object]) -> list[PublicError]:
    if data.get("outcome") == "FAILED" and "error_category" not in data:
        return [PublicError("cross_field", "error_category")]
    return []


def _submit_fields(kinds: tuple[str, ...]) -> dict[str, Field]:
    return {
        "schema_version": schema_version_field(),
        # HOST_ACTION envelopeのbinding echo（一致しないsubmitはC-08が拒否する）
        "run_id": opaque(),
        "action_id": opaque(),
        "action_kind": enum_field(kinds),
        "expected_head_sha": sha(),
        "nonce": opaque(),
        # resultはControllerが払い出したrun directory内のfileで受け渡し、hashで照合する
        "result_hash": opaque(),
        "outcome": enum_field(SUBMIT_OUTCOMES),
        "error_category": text(required=False),
    }


SUBMIT = SchemaDefinition(
    kind=SchemaKind.SUBMIT,
    versions={
        1: VersionSpec(
            fields=_submit_fields(_V1_ACTION_KINDS), rules=(_rule_failed_requires_error_category,)
        ),
        2: VersionSpec(
            fields=_submit_fields(HOST_ACTION_KINDS), rules=(_rule_failed_requires_error_category,)
        ),
    },
)
