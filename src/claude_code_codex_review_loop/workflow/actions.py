# SPDX-License-Identifier: Apache-2.0
"""host actionのregistry（C-08。ADR-0014）。

active hostへ依頼する作業（`HOST_ACTION`）ごとに、**入力・結果・投稿するrecord・
組み立てるC-01 event**の対応を1箇所で定義する。engine（PR-2）はこの表だけを見て動く。

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

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from ..domain import events as ev
from ..domain.commands import HostAction
from ..domain.events import Event
from ..domain.values import Awaiting, RecordKind
from ..schema.registry import SchemaKind


class ActionRegistryError(Exception):
    """registryに無いactionを引いた（呼び出し側の誤り）。"""


@dataclass(frozen=True)
class ResultVariant:
    """1つのresult variant（hostが返し得るrecordの種別ごとの契約）。"""

    record_kind: RecordKind
    result_schema: SchemaKind
    event: type[Event]
    extra_event_inputs: tuple[str, ...] = ()


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
}


@dataclass(frozen=True)
class ActionSpec:
    """1 actionの契約。engineはこの表以外の知識でactionを扱わない。"""

    action: HostAction
    awaiting: Awaiting
    result_kinds: tuple[RecordKind, ...]

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
    ),
    HostAction.DRAFT_DECISION_REQUEST: ActionSpec(
        action=HostAction.DRAFT_DECISION_REQUEST,
        awaiting=Awaiting.HOST_DRAFT_DECISION_REQUEST,
        result_kinds=(RecordKind.DECISION_REQUEST,),
    ),
    HostAction.REVISE_DECISION_REQUEST: ActionSpec(
        action=HostAction.REVISE_DECISION_REQUEST,
        awaiting=Awaiting.HOST_REVISE_DECISION_REQUEST,
        result_kinds=(RecordKind.DECISION_REQUEST,),
    ),
    HostAction.DRAFT_DECISION_BRIEF: ActionSpec(
        action=HostAction.DRAFT_DECISION_BRIEF,
        awaiting=Awaiting.HOST_DRAFT_DECISION_BRIEF,
        result_kinds=(RecordKind.DECISION_BRIEF,),
    ),
    HostAction.RECORD_DECISION: ActionSpec(
        action=HostAction.RECORD_DECISION,
        awaiting=Awaiting.HOST_RECORD_DECISION,
        result_kinds=(RecordKind.DECISION_RECORD,),
    ),
    HostAction.ANSWER_GATE_QUESTION: ActionSpec(
        action=HostAction.ANSWER_GATE_QUESTION,
        awaiting=Awaiting.HOST_ANSWER_GATE_QUESTION,
        result_kinds=(RecordKind.GATE_ANSWER,),
    ),
}


def spec_for(action: HostAction) -> ActionSpec:
    """actionの契約を引く（registryに無ければ呼び出し側の誤り）。"""
    spec = ACTION_SPECS.get(action)
    if spec is None:  # pragma: no cover - registryは全HostActionを覆う（contract testで固定）
        raise ActionRegistryError(f"registryに無いaction: {action.value}")
    return spec


def spec_for_kind(action_kind: str) -> ActionSpec | None:
    """`HOST_ACTION` envelopeの`action_kind`から契約を引く（未知はNone）。"""
    for spec in ACTION_SPECS.values():
        if spec.kind == action_kind:
            return spec
    return None
