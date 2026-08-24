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
- **actionとC-01 eventは1対1**。値による多値discrimination（`REVIEW_RESULT`の2値、
  `CLARIFICATION_ANSWER`の5値等）はCodex由来recordの話で、record -> eventの対応表を
  持つC-10 / C-11の領域を侵さない
- eventが`evidence`以外の入力を要する場合は`extra_event_inputs`で明示する。
  `ProgressReport`はC-10 / C-11由来の値で、**C-08は自分で作らない**
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
class ActionSpec:
    """1 actionの契約。engineはこの表以外の知識でactionを扱わない。"""

    action: HostAction
    awaiting: Awaiting
    result_schema: SchemaKind
    record_kind: RecordKind
    event: type[Event]
    extra_event_inputs: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        """`HOST_ACTION` envelopeの`action_kind`（enum値そのもの）。"""
        return self.action.value


ACTION_SPECS: Final[Mapping[HostAction, ActionSpec]] = {
    HostAction.APPLY_FINDINGS: ActionSpec(
        action=HostAction.APPLY_FINDINGS,
        awaiting=Awaiting.HOST_APPLY_FINDINGS,
        result_schema=SchemaKind.FIX_RESULT,
        record_kind=RecordKind.FIX_RESULT,
        event=ev.FixResultVerified,
        # progress判定・counter snapshot・fingerprintはC-10 / C-11が決める（C-08は作らない）
        extra_event_inputs=("report",),
    ),
    HostAction.DRAFT_DECISION_REQUEST: ActionSpec(
        action=HostAction.DRAFT_DECISION_REQUEST,
        awaiting=Awaiting.HOST_DRAFT_DECISION_REQUEST,
        result_schema=SchemaKind.DECISION_REQUEST,
        record_kind=RecordKind.DECISION_REQUEST,
        event=ev.DecisionRequestVerified,
    ),
    HostAction.REVISE_DECISION_REQUEST: ActionSpec(
        action=HostAction.REVISE_DECISION_REQUEST,
        awaiting=Awaiting.HOST_REVISE_DECISION_REQUEST,
        result_schema=SchemaKind.DECISION_REQUEST,
        record_kind=RecordKind.DECISION_REQUEST,
        event=ev.DecisionRequestVerified,
    ),
    HostAction.DRAFT_DECISION_BRIEF: ActionSpec(
        action=HostAction.DRAFT_DECISION_BRIEF,
        awaiting=Awaiting.HOST_DRAFT_DECISION_BRIEF,
        result_schema=SchemaKind.DECISION_BRIEF,
        record_kind=RecordKind.DECISION_BRIEF,
        event=ev.DecisionBriefVerified,
    ),
    HostAction.RECORD_DECISION: ActionSpec(
        action=HostAction.RECORD_DECISION,
        awaiting=Awaiting.HOST_RECORD_DECISION,
        result_schema=SchemaKind.DECISION_RECORD,
        record_kind=RecordKind.DECISION_RECORD,
        event=ev.DecisionRecordVerified,
    ),
    HostAction.ANSWER_GATE_QUESTION: ActionSpec(
        action=HostAction.ANSWER_GATE_QUESTION,
        awaiting=Awaiting.HOST_ANSWER_GATE_QUESTION,
        result_schema=SchemaKind.GATE_ANSWER,
        record_kind=RecordKind.GATE_ANSWER,
        event=ev.GateAnswerVerified,
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
