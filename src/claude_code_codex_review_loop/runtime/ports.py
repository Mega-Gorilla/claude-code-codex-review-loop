# SPDX-License-Identifier: Apache-2.0
"""engineへ値を供給するportの実装（Phase 8 PR-3b1。ADR-0020）。

`workflow/ports.py`が定めた6 portのうち、**今日導出できるものをここで実装する**。基準は
「C-10 / C-11のdomain形状を先取りしない」ことで（`workflow/ports.py`のdocstring）、
既存componentの出力と既存registryの宣言だけから決まるものに限る。

| port | 導出元 |
| --- | --- |
| `ChainRecords` | C-05の取得 + C-06の`verify_record_chain` |
| `TreeStopper` | C-03の`stop_tree_by_ref` |
| `ChainEvidence` | registryの`evidence_kinds` × 検証済みchain（DOD-02の選択規則そのもの） |
| `RegistryRecordEvents` | registryの`build_event`（record kindとeventの1対1対応） |
| `UserInputBody` | 転記recordの本文選択（**ユーザーが書いた文そのもの**） |

`ActionPayloadPort`（`round` / `finding_ids`）と、agent recordの本文はfinding ledgerと
kindごとの表現を要するためC-10 / C-11の領域である。ここでは**名指しでfail closed**にし、
「無いものを既定値で埋める」経路を作らない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..domain.events import Event
from ..domain.values import RecordEvidence, RecordKind
from ..identity.record_chain import ChainVerification, VerifiedRecord, verify_record_chain
from ..process import StopResult, TreeRef, stop_tree_by_ref
from ..transport.conversation import fetch_comments_since
from ..workflow import (
    BODY_VALUE_FIELDS,
    RESULT_VARIANTS,
    ActionPayloadPort,
    ActionRegistryError,
    EvidencePort,
    ProcessStopPort,
    RecordBodyPort,
    RecordEventPort,
    RecordSourcePort,
    RequestContext,
    build_event,
    spec_for,
    user_spec_for,
)
from ..workflow.ports import ActionContext
from .config import SessionConfig


class PortUnavailableError(Exception):
    """まだ実装が無いportを引いた。detailは担当componentを名指しする。"""


@dataclass(frozen=True)
class ChainRecords:
    """当該runの検証済みchain（C-05の取得 -> C-06の7条件検証）。

    fixtureへ検証結果を直書きせず、**製品関数だけ**でchainを作る。checkpointを渡さないため
    high-water markに基づくprobeは行わない（AC-C06-09の残存riskはC-06のdocstringが正本）。
    """

    config: SessionConfig

    def chain(self, run_id: str) -> ChainVerification:
        fetched = fetch_comments_since(
            self.config.context(),
            self.config.repo,
            self.config.number,
            self.config.search_since,
            policy=self.config.policy(),
            max_pages=self.config.search_max_pages,
        )
        return verify_record_chain(
            fetched.comments,
            run_id=run_id,
            detection_head=self.config.detection_head,
            producers=self.config.producers,
            checkpoint=None,
            probes={},
        )


@dataclass(frozen=True)
class TreeStopper:
    """process treeの停止（C-03）。`stop_tree_by_ref`は冪等である。"""

    def stop(self, ref: TreeRef, grace_seconds: float) -> StopResult:
        return stop_tree_by_ref(ref, grace_seconds)


def _evidence_kinds(context: RequestContext) -> tuple[RecordKind, ...]:
    """当該actionまたはuser requestが根拠として許可するrecord種別（registryが正本）。"""
    if isinstance(context, ActionContext):
        return spec_for(context.action).evidence_kinds
    spec = user_spec_for(context.awaiting)
    if spec is None:  # pragma: no cover - `UserRequestContext`は3値のawaitingでのみ作られる
        raise PortUnavailableError(f"{context.awaiting.value}はユーザー入力待ちではない")
    return spec.evidence_kinds


@dataclass(frozen=True)
class ChainEvidence:
    """根拠として同梱する検証済みrecordを選ぶ（DOD-02）。

    対象headの全recordではなく、**registryが当該actionへ宣言したkind**かつ**対象headの**
    recordだけをseq昇順で返す。engineの`_evidence_of`が同じ条件を検査するため、この選択が
    そのまま契約になる。
    """

    records: RecordSourcePort

    def evidence_for(self, context: RequestContext) -> Sequence[VerifiedRecord]:
        allowed = _evidence_kinds(context)
        chain = self.records.chain(context.run_id)
        return tuple(
            record
            for record in sorted(chain.records, key=lambda item: item.seq)
            if record.kind in allowed and record.head_sha == context.head_sha
        )


@dataclass(frozen=True)
class RegistryRecordEvents:
    """検証済みrecordからC-01 eventを作る（registryの1対1対応）。

    `extra_event_inputs`が空のkindだけを扱う。`ProgressReport`（progress判定）や`head`は
    C-10 / C-11が決める値で、ここで作ると判定を偽装することになる。
    """

    def event_for(self, evidence: RecordEvidence, record: VerifiedRecord) -> Event:
        variant = RESULT_VARIANTS.get(record.kind)
        if variant is None:
            raise PortUnavailableError(
                f"{record.kind.value}のevent対応表が無い（C-10 / C-11が持つ）"
            )
        if variant.extra_event_inputs:
            raise PortUnavailableError(
                f"{record.kind.value}のeventは{list(variant.extra_event_inputs)}を要する"
                "（C-10 / C-11が供給する）"
            )
        try:
            return build_event(variant, evidence, {})
        except ActionRegistryError as error:  # pragma: no cover - 直上で入力の過不足を除いている
            raise PortUnavailableError(str(error)) from error


@dataclass(frozen=True)
class UserInputBody:
    """転記recordの公開本文を選ぶ。

    本文は**ユーザーが書いた文そのもの**で、C-08は選ぶだけで文面を作らない
    （`BODY_VALUE_FIELDS`がkindごとのfieldを宣言する）。宣言の無いkind——agent recordと、
    自由記述を持たない`MERGE_APPROVAL`——は表現を作る必要があり、C-10 / C-11 / C-13の領域である。
    """

    def body_for(self, kind: RecordKind, payload: Mapping[str, object]) -> str:
        field = BODY_VALUE_FIELDS.get(kind)
        if field is None:
            raise PortUnavailableError(
                f"{kind.value}の本文表現が無い（C-10 / C-11 / C-13が持つ）"
            )
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise PortUnavailableError(f"{kind.value}の{field}が空である（本文を作らない）")
        return value


@dataclass(frozen=True)
class UnavailableActionPayload:
    """`HOST_ACTION.payload`はfinding ledger（C-10）とdecision（C-11）由来である。"""

    def payload_for(self, context: ActionContext) -> Mapping[str, object]:
        raise PortUnavailableError(
            f"{context.action.value}のpayloadを供給する実装が無い（C-10 / C-11が持つ）"
        )


@dataclass(frozen=True)
class PortSet:
    """engineへ渡すportの束。呼び出し側が差し替えられるよう1つにまとめる。"""

    payload: ActionPayloadPort
    evidence: EvidencePort
    records: RecordSourcePort
    body: RecordBodyPort
    events: RecordEventPort
    stop: ProcessStopPort


def default_ports(config: SessionConfig) -> PortSet:
    """今日導出できるportで束を作る（未実装のものはfail closedの実装が入る）。"""
    records = ChainRecords(config=config)
    return PortSet(
        payload=UnavailableActionPayload(),
        evidence=ChainEvidence(records=records),
        records=records,
        body=UserInputBody(),
        events=RegistryRecordEvents(),
        stop=TreeStopper(),
    )


__all__ = [
    "ChainEvidence",
    "ChainRecords",
    "PortSet",
    "PortUnavailableError",
    "RegistryRecordEvents",
    "TreeStopper",
    "UnavailableActionPayload",
    "UserInputBody",
    "default_ports",
]
