# SPDX-License-Identifier: Apache-2.0
"""host actionと`AWAIT_USER`のregistry（C-08。ADR-0014 / ADR-0018）。

active hostへ依頼する作業（`HOST_ACTION`）と、ユーザー入力待ち（`AWAIT_USER`）ごとに、
**入力・結果・投稿するrecord・組み立てるC-01 event**の対応を1箇所で定義する。engineは
この表だけを見て動く。`AWAIT_USER`側の表（`USER_REQUEST_SPECS`）と重複防止key
（`intent_key`）はfileの末尾に置く。

- **registryはC-01の`HostAction`（6値）に限る**。C-01は`AWAITING_COMMANDS`で
  `Awaiting.HOST_*`と`RequestHostAction`を1対1に対応させており、engineが受け取り得る
  actionはこの6つだけである。implementation plan Section 2.3の残り（`ASK_CLARIFICATION`等）は
  C-01が発行しないため到達不能で、**C-01へ追加された時点でここへ入る**（Section 2.3は
  「初期案。Phase 8で確定する」と明記しており、本registryはその確定にあたる）
- **結果payloadは既存のrecord schemaを再利用する**。host actionの成果物は、そのまま
  GitHubへ投稿するrecordのpayloadだからである（新しいresult schemaを作らない）
- **1つのactionは複数の正規な結果（result variant）を持ち得る**。C-01は同じawaitingに対して
  複数のrecord kindの`RecordProduced`を受理する（例: `HOST_APPLY_FINDINGS`はfix完了だけでなく、
  質問・判断依頼・外部依存・tool permission停止へも進める）。registryの結果集合は
  **C-01が当該awaitingで許可するkind集合と完全一致**させ、contract testでdriftを止める
- **record kindとC-01 eventは1対1**。値による多値discrimination（`REVIEW_RESULT`の2値、
  `CLARIFICATION_ANSWER`の5値等）はCodex由来recordの話で、record -> eventの対応表を
  持つC-10 / C-11の領域を侵さない
- eventが`evidence`以外の入力を要する場合は`extra_event_inputs`で明示する。
  `ProgressReport`と`head`はC-10 / C-11由来の値で、**C-08は自分で作らない**
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, cast

from ..domain import events as ev
from ..domain._ruledefs import BlockKind
from ..domain.commands import HostAction
from ..domain.events import Event
from ..domain.values import (
    Awaiting,
    BlockContext,
    ExternalDependencyBlock,
    Progress,
    ProgressBlock,
    RecordEvidence,
    RecordKind,
)
from ..schema import REGISTRY
from ..schema.registry import SchemaDefinition, SchemaKind
from ..transport.render import normalize_newlines


class ActionRegistryError(Exception):
    """registryに無いactionを引いた（呼び出し側の誤り）。"""


@dataclass(frozen=True)
class ResultVariant:
    """1つのresult variant（hostが返し得るrecordの種別ごとの契約）。"""

    record_kind: RecordKind
    result_schema: SchemaKind
    event: type[Event]
    extra_event_inputs: tuple[str, ...] = ()
    # **eventを`RecordEvidence`から組み立てられない**種別（写像はportが担う）。
    # `extra_event_inputs`は「C-08が作らない値」を宣言するが、こちらはevidenceの
    # **形そのもの**が違う場合である（例: `BlockResolvedIntervention`は
    # `BlockResolutionEvidence`を受け取る）。ADR-0017 決定2 / ADR-0023 決定7
    port_mapped: bool = False

    @property
    def result_definition(self) -> SchemaDefinition:
        """結果payloadの検証に使う既存のrecord schema（新しいresult schemaを作らない）。"""
        return REGISTRY[self.result_schema]


# record kindごとの結果契約。kindとeventは1対1で、`extra_event_inputs`はC-08が作らない値
RESULT_VARIANTS: Final[Mapping[RecordKind, ResultVariant]] = {
    RecordKind.FIX_RESULT: ResultVariant(
        record_kind=RecordKind.FIX_RESULT,
        result_schema=SchemaKind.FIX_RESULT,
        event=ev.FixResultVerified,
        # progress判定・counter snapshot・fingerprintはC-10 / C-11が決める
        extra_event_inputs=("report",),
    ),
    RecordKind.CLARIFICATION_QUESTION: ResultVariant(
        record_kind=RecordKind.CLARIFICATION_QUESTION,
        result_schema=SchemaKind.CLARIFICATION_QUESTION,
        event=ev.ClarificationQuestionVerified,
        extra_event_inputs=("report",),
    ),
    RecordKind.DECISION_REQUEST: ResultVariant(
        record_kind=RecordKind.DECISION_REQUEST,
        result_schema=SchemaKind.DECISION_REQUEST,
        event=ev.DecisionRequestVerified,
    ),
    RecordKind.EXTERNAL_DEPENDENCY: ResultVariant(
        record_kind=RecordKind.EXTERNAL_DEPENDENCY,
        result_schema=SchemaKind.EXTERNAL_DEPENDENCY,
        event=ev.ExternalDependencyVerified,
        extra_event_inputs=("head",),
    ),
    RecordKind.PERMISSION_BLOCK: ResultVariant(
        record_kind=RecordKind.PERMISSION_BLOCK,
        result_schema=SchemaKind.PERMISSION_BLOCK,
        event=ev.ToolPermissionBlocked,
    ),
    RecordKind.DECISION_BRIEF: ResultVariant(
        record_kind=RecordKind.DECISION_BRIEF,
        result_schema=SchemaKind.DECISION_BRIEF,
        event=ev.DecisionBriefVerified,
    ),
    RecordKind.DECISION_RECORD: ResultVariant(
        record_kind=RecordKind.DECISION_RECORD,
        result_schema=SchemaKind.DECISION_RECORD,
        event=ev.DecisionRecordVerified,
    ),
    RecordKind.GATE_ANSWER: ResultVariant(
        record_kind=RecordKind.GATE_ANSWER,
        result_schema=SchemaKind.GATE_ANSWER,
        event=ev.GateAnswerVerified,
    ),
    # user-input record（`AWAIT_USER`の結果。ADR-0018）。hostがユーザー入力を構造化して
    # 返し、C-08が内部record規約でGitHubへ転記する。eventの追加入力は持たない
    RecordKind.USER_DECISION: ResultVariant(
        record_kind=RecordKind.USER_DECISION,
        result_schema=SchemaKind.USER_DECISION,
        event=ev.UserDecisionVerified,
    ),
    RecordKind.GATE_QUESTION: ResultVariant(
        record_kind=RecordKind.GATE_QUESTION,
        result_schema=SchemaKind.GATE_QUESTION,
        event=ev.GateQuestionVerified,
    ),
    RecordKind.GATE_CHANGES: ResultVariant(
        record_kind=RecordKind.GATE_CHANGES,
        result_schema=SchemaKind.GATE_CHANGES,
        event=ev.GateChangesVerified,
    ),
    RecordKind.MERGE_APPROVAL: ResultVariant(
        record_kind=RecordKind.MERGE_APPROVAL,
        result_schema=SchemaKind.MERGE_APPROVAL,
        event=ev.MergeApprovalVerified,
    ),
    # 膠着 / 外部依存blockの解消（`BLOCKED`での介入）。eventは`BlockResolutionEvidence`を
    # 受け取るためevidenceだけでは組み立てられず、写像はportが担う（決定7）
    RecordKind.BLOCK_INTERVENTION: ResultVariant(
        record_kind=RecordKind.BLOCK_INTERVENTION,
        result_schema=SchemaKind.BLOCK_INTERVENTION,
        event=ev.BlockResolvedIntervention,
        port_mapped=True,
    ),
    RecordKind.USER_CANCEL: ResultVariant(
        record_kind=RecordKind.USER_CANCEL,
        result_schema=SchemaKind.USER_CANCEL,
        event=ev.UserCancelVerified,
    ),
}


@dataclass(frozen=True)
class ActionSpec:
    """1 actionの契約。engineはこの表以外の知識でactionを扱わない。"""

    action: HostAction
    awaiting: Awaiting
    result_kinds: tuple[RecordKind, ...]
    evidence_kinds: tuple[RecordKind, ...] = ()

    @property
    def kind(self) -> str:
        """`HOST_ACTION` envelopeの`action_kind`（enum値そのもの）。"""
        return self.action.value

    @property
    def variants(self) -> tuple[ResultVariant, ...]:
        return tuple(RESULT_VARIANTS[kind] for kind in self.result_kinds)

    def variant_for(self, record_kind: RecordKind) -> ResultVariant | None:
        """hostが選んだresult variantの契約（当該actionで許可されない種別はNone）。"""
        if record_kind not in self.result_kinds:
            return None
        return RESULT_VARIANTS[record_kind]


ACTION_SPECS: Final[Mapping[HostAction, ActionSpec]] = {
    # 修正の完了だけでなく、質問・判断依頼・外部依存・tool permission停止も正規の結果
    HostAction.APPLY_FINDINGS: ActionSpec(
        action=HostAction.APPLY_FINDINGS,
        awaiting=Awaiting.HOST_APPLY_FINDINGS,
        result_kinds=(
            RecordKind.FIX_RESULT,
            RecordKind.CLARIFICATION_QUESTION,
            RecordKind.DECISION_REQUEST,
            RecordKind.EXTERNAL_DEPENDENCY,
            RecordKind.PERMISSION_BLOCK,
        ),
        # 根拠: 対象のreview結果と、修正方針を確定させたclarificationの回答
        evidence_kinds=(RecordKind.REVIEW_RESULT, RecordKind.CLARIFICATION_ANSWER),
    ),
    HostAction.DRAFT_DECISION_REQUEST: ActionSpec(
        action=HostAction.DRAFT_DECISION_REQUEST,
        awaiting=Awaiting.HOST_DRAFT_DECISION_REQUEST,
        result_kinds=(RecordKind.DECISION_REQUEST,),
        # 根拠: 判断が必要になった契機のreview結果
        evidence_kinds=(RecordKind.REVIEW_RESULT,),
    ),
    HostAction.REVISE_DECISION_REQUEST: ActionSpec(
        action=HostAction.REVISE_DECISION_REQUEST,
        awaiting=Awaiting.HOST_REVISE_DECISION_REQUEST,
        result_kinds=(RecordKind.DECISION_REQUEST,),
        # 根拠: 再提出を求めたverdictと、その対象の判断依頼
        evidence_kinds=(RecordKind.DECISION_REQUEST, RecordKind.DECISION_VERDICT),
    ),
    HostAction.DRAFT_DECISION_BRIEF: ActionSpec(
        action=HostAction.DRAFT_DECISION_BRIEF,
        awaiting=Awaiting.HOST_DRAFT_DECISION_BRIEF,
        result_kinds=(RecordKind.DECISION_BRIEF,),
        # 根拠: 判断依頼とverdict（briefは両者を反映する）
        evidence_kinds=(RecordKind.DECISION_REQUEST, RecordKind.DECISION_VERDICT),
    ),
    HostAction.RECORD_DECISION: ActionSpec(
        action=HostAction.RECORD_DECISION,
        awaiting=Awaiting.HOST_RECORD_DECISION,
        result_kinds=(RecordKind.DECISION_RECORD,),
        # 根拠: 提示したbriefと、それに対するユーザーの判断
        evidence_kinds=(RecordKind.DECISION_BRIEF, RecordKind.USER_DECISION),
    ),
    HostAction.ANSWER_GATE_QUESTION: ActionSpec(
        action=HostAction.ANSWER_GATE_QUESTION,
        awaiting=Awaiting.HOST_ANSWER_GATE_QUESTION,
        result_kinds=(RecordKind.GATE_ANSWER,),
        # 根拠: 回答対象のgate質問
        evidence_kinds=(RecordKind.GATE_QUESTION,),
    ),
}


def spec_for(action: HostAction) -> ActionSpec:
    """actionの契約を引く（registryに無ければ呼び出し側の誤り）。"""
    spec = ACTION_SPECS.get(action)
    if spec is None:  # pragma: no cover - registryは全HostActionを覆う（contract testで固定）
        raise ActionRegistryError(f"registryに無いaction: {action.value}")
    return spec


def build_event(
    variant: ResultVariant, evidence: RecordEvidence, inputs: Mapping[str, object]
) -> Event:
    """検証済みrecordのevidenceからC-01 eventを組み立てる（registryが持つ知識）。

    `evidence`以外に要る値は`extra_event_inputs`が宣言したものだけで、**過不足があれば
    構築しない**（C-08が作らない値をNoneで埋める経路を作らない。ADR-0014 決定9）。
    """
    if variant.port_mapped:
        raise ActionRegistryError(
            f"{variant.record_kind.value}のeventはRecordEvidenceだけでは構築できない"
            "（写像はRecordEventPortが担う）"
        )
    if set(inputs) != set(variant.extra_event_inputs):
        raise ActionRegistryError(
            f"{variant.record_kind.value}のevent入力が宣言と一致しない: "
            f"{sorted(inputs)} != {sorted(variant.extra_event_inputs)}"
        )
    # eventの型は`type[Event]`（直和全体）なので、registryが保証する形をcastで示す。
    # 実際の一致は`test_c08_actions.py`のcontract test（EXPECTED_KINDとfield名）が固定する
    factory = cast(Callable[..., Event], variant.event)
    return factory(evidence=evidence, **inputs)


def spec_for_awaiting(awaiting: Awaiting) -> ActionSpec | None:
    """C-01のawaitingから契約を引く（host action以外はNone）。"""
    for spec in ACTION_SPECS.values():
        if spec.awaiting is awaiting:
            return spec
    return None


def spec_for_kind(action_kind: str) -> ActionSpec | None:
    """`HOST_ACTION` envelopeの`action_kind`から契約を引く（未知はNone）。"""
    for spec in ACTION_SPECS.values():
        if spec.kind == action_kind:
            return spec
    return None


# ---------------------------------------------------------------------------
# `AWAIT_USER` registry（ADR-0018）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserRequestSpec:
    """1つのユーザー入力待ちの契約。engineはこの表以外の知識でuser inputを扱わない。

    `resume_event`は**recordを作らない応答**のためにある。tool permissionの明示resumeは
    C-01の`PermissionResumeValidated`（pendingを要求しない）で表され、GitHubへ投稿する
    recordを持たない。他の2 awaitingはrecordを作るのでNoneである。
    """

    awaiting: Awaiting
    result_kinds: tuple[RecordKind, ...]
    evidence_kinds: tuple[RecordKind, ...] = ()
    resume_event: type[Event] | None = None

    @property
    def kind(self) -> str:
        """`USER_REQUEST` envelopeの`awaiting`（enum値そのもの）。"""
        return self.awaiting.value

    def variant_for(self, record_kind: RecordKind) -> ResultVariant | None:
        """hostが返したrecord種別の契約（当該awaitingで許可されない種別はNone）。"""
        if record_kind not in self.result_kinds:
            return None
        return RESULT_VARIANTS[record_kind]


# `USER_CANCEL`が全awaitingに入るのはC-01のP-21（awaiting不問・非terminal全state）に
# 対応するため。集合はC-01の`PRODUCED_RULES`が当該awaitingで許可するkindと完全一致させ、
# contract testでdriftを止める（`ACTION_SPECS`と同じ規則）
USER_REQUEST_SPECS: Final[Mapping[Awaiting, UserRequestSpec]] = {
    Awaiting.USER_INPUT_DECISION: UserRequestSpec(
        awaiting=Awaiting.USER_INPUT_DECISION,
        result_kinds=(RecordKind.USER_DECISION, RecordKind.USER_CANCEL),
        # 根拠: 判断を求めたbrief（ユーザーはこれを読んで答える）
        evidence_kinds=(RecordKind.DECISION_BRIEF,),
    ),
    Awaiting.USER_INPUT_GATE: UserRequestSpec(
        awaiting=Awaiting.USER_INPUT_GATE,
        result_kinds=(
            RecordKind.GATE_QUESTION,
            RecordKind.GATE_CHANGES,
            RecordKind.MERGE_APPROVAL,
            RecordKind.USER_CANCEL,
        ),
        # 根拠: merge gateで提示したfinal report
        evidence_kinds=(RecordKind.FINAL_REPORT,),
    ),
    Awaiting.USER_INPUT_PERMISSION: UserRequestSpec(
        awaiting=Awaiting.USER_INPUT_PERMISSION,
        result_kinds=(RecordKind.USER_CANCEL,),
        # 根拠: 停止の内容を説明したpermission block record
        evidence_kinds=(RecordKind.PERMISSION_BLOCK,),
        resume_event=ev.PermissionResumeValidated,
    ),
}


@dataclass(frozen=True)
class BlockRequestSpec:
    """1つのblock介入待ちの契約（`UserRequestSpec`のblock版。ADR-0023）。

    `BLOCKED`の待機に`awaiting`は無く、識別子は解除対象の**block attempt binding**である。
    どのblockが介入を受け付けるかを決めるのは**C-01**で、この表はその写しである
    （P-22 / P-23。contract testが`PRODUCED_RULES`との一致を固定する）。
    """

    block_kind: BlockKind
    result_kinds: tuple[RecordKind, ...]
    # `PROGRESS` blockはreasonまで一致しないと`BLOCK_INTERVENTION`を受理しない（P-22）。
    # `EXTERNAL_DEPENDENCY`はreasonを持たないのでNone
    reasons: frozenset[Progress] | None = None
    evidence_kinds: tuple[RecordKind, ...] = ()

    @property
    def kind(self) -> str:
        """envelopeとerror messageで使う待機種別の表示（block種別そのもの）。"""
        return str(self.block_kind.value)

    def variant_for(self, record_kind: RecordKind) -> ResultVariant | None:
        """ユーザーが返したrecord種別の契約（当該blockで許可されない種別はNone）。"""
        if record_kind not in self.result_kinds:
            return None
        return RESULT_VARIANTS[record_kind]


# `USER_CANCEL`が入るのはP-21（awaiting不問・非terminal全state）が`BLOCKED`を覆うためで、
# ADR-0018 決定2の導出規則をそのまま適用した結果である。
#
# **`LIMIT_REACHED`の膠着blockと`RECORD_INTEGRITY` blockはここに無い**。前者の出口は
# limit引き上げ（B-LR）、後者は復元 / salvage専用evidence（fail closed）であり、C-01は
# どちらでも`BLOCK_INTERVENTION`を受理しない。受理されない応答を提示しないため、
# これらのblockは`Blocked`のまま返す（ADR-0019 決定19）。
BLOCK_REQUEST_SPECS: Final[tuple[BlockRequestSpec, ...]] = (
    BlockRequestSpec(
        block_kind=BlockKind.PROGRESS,
        result_kinds=(RecordKind.BLOCK_INTERVENTION, RecordKind.USER_CANCEL),
        reasons=frozenset({Progress.NO_PROGRESS}),
    ),
    BlockRequestSpec(
        block_kind=BlockKind.EXTERNAL_DEPENDENCY,
        result_kinds=(RecordKind.BLOCK_INTERVENTION, RecordKind.USER_CANCEL),
        # 根拠: 何を待っているかを書いた外部依存record
        evidence_kinds=(RecordKind.EXTERNAL_DEPENDENCY,),
    ),
)

RequestSpec = UserRequestSpec | BlockRequestSpec


def block_binding_of(block: BlockContext) -> str | None:
    """blockのattempt binding（`RecordIntegrityBlock`は持たないのでNone）。

    `RECORD_INTEGRITY`が持つのはviolation集合であり、単一のattempt bindingではない。
    介入requestを出さないblockなので、ここでNoneになるのは正しい。
    """
    if isinstance(block, (ProgressBlock, ExternalDependencyBlock)):
        return str(block.binding.value)
    return None


def block_spec_for(block: BlockContext) -> BlockRequestSpec | None:
    """介入を受け付けるblockの契約を引く（受け付けないblockはNone）。

    Noneは「runが壊れている」ではなく「このblockの出口は介入ではない」である。
    """
    for spec in BLOCK_REQUEST_SPECS:
        if isinstance(block, ProgressBlock) and spec.block_kind is BlockKind.PROGRESS:
            if spec.reasons is not None and block.reason in spec.reasons:
                return spec
        if isinstance(block, ExternalDependencyBlock) and spec.block_kind is BlockKind.EXTERNAL_DEPENDENCY:
            return spec
    return None


def user_spec_for(awaiting: Awaiting) -> UserRequestSpec | None:
    """ユーザー入力待ちの契約を引く（host actionや他のawaitingはNone）。"""
    return USER_REQUEST_SPECS.get(awaiting)


INTENT_KEY_PREFIX: Final = "ui:"

# **record kindだけでは正規化intentが決まらない種別**と、その値を持つrecord schemaのfield。
#
# merge gateの4 intentはkindと1対1だが（`QUESTION`->`GATE_QUESTION`/
# `REQUEST_CHANGES`->`GATE_CHANGES` / `APPROVE_MERGE`->`MERGE_APPROVAL` /
# `CANCEL`->`USER_CANCEL`）、`USER_DECISION`は**同じkindの中に回答値を持つ**。同じdecisionへの
# 「[1]で進める」と「[2]で進める」をkindだけで区別できないと、2経路が別々の回答を主張しても
# 同一intentへ潰れてしまう（ADR-0018 決定7）。
#
# 宣言するfieldはrecord schemaの**必須text field**でなければならない（contract testで固定）。
INTENT_VALUE_FIELDS: Final[Mapping[RecordKind, str]] = {
    RecordKind.USER_DECISION: "answer",
}


# **転記recordの公開本文が入っているpayload field**。本文はユーザーが書いた文そのもので、
# C-08は選ぶだけで文面を作らない（ADR-0020）。
#
# 宣言が無いkindは本文を**構成**する必要がある: agent recordはkindごとの表現（C-10 / C-11）、
# `MERGE_APPROVAL`は自由記述を持たずgate semanticsの表現になる（C-13）。
BODY_VALUE_FIELDS: Final[Mapping[RecordKind, str]] = {
    RecordKind.USER_DECISION: "answer",
    RecordKind.GATE_QUESTION: "body",
    RecordKind.GATE_CHANGES: "body",
    RecordKind.USER_CANCEL: "reason",
    RecordKind.BLOCK_INTERVENTION: "body",
}


def intent_digest(value: str) -> str:
    """正規化intent値のcanonical hash（両経路が同じ値から同じdigestを導く）。

    揃えるのは**改行と前後の空白だけ**である。表記の揺れで別intentにしないための正規化で、
    **意味の解釈はしない**: 言い換えが同じ回答かどうかの判定はC-11 / C-13の領域であり、
    C-08は「同じ値か」しか見ない。
    """
    return hashlib.sha256(normalize_newlines(value).strip().encode("utf-8")).hexdigest()


def intent_value_of(kind: RecordKind, payload: Mapping[str, object]) -> str | None:
    """検証済みrecord payloadから正規化intent値を取り出す（宣言の無いkindはNone）。

    `payload`は当該kindのrecord schemaを通ったものに限る。宣言したfieldは必須textなので、
    ここに無い場合は検証前のpayloadを渡した呼び出し側の誤りである。
    """
    field = INTENT_VALUE_FIELDS.get(kind)
    return None if field is None else str(payload[field])


@dataclass(frozen=True)
class AwaitingInstance:
    """`AWAIT_USER`の待機instance。

    `since_seq`（request発行時点のchain最大seq）がinstanceを表す。同じstateとheadへ
    再び戻ってきた次のinstanceとは、この値で区別される。
    """

    awaiting: Awaiting
    since_seq: int


@dataclass(frozen=True)
class BlockInstance:
    """`BLOCKED`での介入待ちinstance。

    block attempt binding**そのもの**がinstanceである。blockへ入り直せば新しいbindingに
    なるため、`since_seq`のような別の識別子を要しない。
    """

    block_binding: str


# 待機instanceの直和。intent keyはこの値から作る（2経路が同じkeyを導けることが要件）
WaitInstance = AwaitingInstance | BlockInstance


def _instance_fields(instance: WaitInstance) -> dict[str, object]:
    """intent keyへ入れるinstance固有のfield（種別ごとにfield集合が異なる）。"""
    if isinstance(instance, BlockInstance):
        return {"block": instance.block_binding}
    return {"awaiting": instance.awaiting.value, "since": instance.since_seq}


def intent_key(
    *,
    run_id: str,
    instance: WaitInstance,
    head_sha: str,
    kind: RecordKind,
    intent_value: str | None = None,
) -> str:
    """ユーザー入力の**正規化intent key**（2経路の重複防止key。ADR-0018 決定7）。

    `request_id`を唯一の相関keyにはできない: GitHub直接comment（経路2）は`AWAIT_USER`の
    request IDを持たないためである。両経路がcheckpointから導出できる値だけで構成する。

    - **待機instance**は直和である（`WaitInstance`）。`AWAIT_USER`は`awaiting` + `since_seq`、
      `BLOCKED`での介入待ちはblock attempt bindingで、field集合が異なるので衝突しない。
      台帳は**共通**にする（C-13が経路2で同じkeyを導けることが要件）
    - 正規化intentは**record kind**と、kindだけで決まらない種別では`intent_value`の
      digestである（`INTENT_VALUE_FIELDS`）。値の**要否はkindが決める**ため、宣言と実引数が
      食い違う呼び出しは受理しない（片方だけの経路が別のkeyを作るのを防ぐ）

    区切り文字を含むopaque値でも衝突しないよう、sorted keysのcompact JSONで導出する
    （`identity.allowlist`の受理binding導出と同じ方式）。
    """
    field = INTENT_VALUE_FIELDS.get(kind)
    if (field is None) != (intent_value is None):
        raise ActionRegistryError(
            f"{kind.value}のintent keyと値の宣言が一致しない（宣言: {field}、実引数: "
            f"{'あり' if intent_value is not None else 'なし'}）"
        )
    payload: dict[str, object] = {
        "head": head_sha,
        "intent": None if intent_value is None else intent_digest(intent_value),
        "kind": kind.value,
        "run": run_id,
        **_instance_fields(instance),
    }
    return INTENT_KEY_PREFIX + json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
