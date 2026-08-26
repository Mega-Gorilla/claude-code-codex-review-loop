# SPDX-License-Identifier: Apache-2.0
"""step engineへ値を供給するport（Phase 8。ADR-0015）。

C-01の`RequestHostAction`は**action種別しか持たない**（`domain/commands.py`）。一方で
`HOST_ACTION`のpayloadは`round` / `finding_ids` / `decision_id`等を要し、投稿するrecordの
本文はrecord kindごとの表現を要する。これらの供給元はengineの外（finding ledgerはC-10、
decisionはC-11）にあるため、**typed portとして境界を切る**。

Phase 8はfake実装で満たし、C-10 / C-11で本実装へ差し替える。portの戻り値は既存の型に
限り、後続componentのdomain形状を先取りしない。

ID（action ID / nonce / correlation ID）と時刻はportではなく**引数**で受け取る
（`id_source` / `issued_at`）。既定値を解決するのはC-12であり、engineは持たない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from ..domain.commands import HostAction
from ..domain.events import Event
from ..domain.values import Awaiting, RecordEvidence, RecordKind
from ..identity.record_chain import ChainVerification, VerifiedRecord
from ..process import StopResult, TreeRef


@dataclass(frozen=True)
class ActionContext:
    """actionを組み立てる文脈（portへ渡す入力。engineが持つ値だけで構成する）。"""

    action: HostAction
    run_id: str
    repository: str
    number: int
    head_sha: str


@dataclass(frozen=True)
class UserRequestContext:
    """ユーザー入力待ちを組み立てる文脈（`ActionContext`のuser版。ADR-0018）。

    host actionと違い、実行者はagentではなくユーザーである。したがって「どのactionか」
    ではなく「C-01がどの入力を待っているか」（awaiting）が識別子になる。
    """

    awaiting: Awaiting
    run_id: str
    repository: str
    number: int
    head_sha: str


# actionとuser requestは同じevidence選択規則（許可kind・対象head・seq昇順）を使うため、
# portは両方の文脈を受け取る。片方だけのportを増やして規則を二重化しない
RequestContext = ActionContext | UserRequestContext


class ActionPayloadPort(Protocol):
    """`HOST_ACTION.payload`を供給する（`HOST_ACTION_PAYLOADS`が検証する形）。"""

    def payload_for(self, context: ActionContext) -> Mapping[str, object]: ...


class EvidencePort(Protocol):
    """actionの根拠として同梱する検証済みrecordを供給する（AC-C08-07）。

    対象headの全recordではなく、**そのactionを実行するための根拠**を選ぶ。engineは
    受け取った列をkind許可（`ActionSpec.evidence_kinds`）とseq昇順で検査する。
    """

    def evidence_for(self, context: RequestContext) -> Sequence[VerifiedRecord]: ...


class RecordSourcePort(Protocol):
    """当該runのchain検証結果（C-06の`verify_record_chain`の出力そのもの）。

    **violationsを捨てない**。recordだけを返すと、壊れたchainの上でtransactionを発行したり
    recordを投稿したりできてしまう。engineは`ChainVerification.is_intact`でgateし、violationが
    あればC-01のintegrity経路へ渡す（ADR-0017）。
    """

    def chain(self, run_id: str) -> ChainVerification: ...


class RecordEventPort(Protocol):
    """検証済みrecordから、C-01へ入力するeventを作る。

    `PersistRecord`は**任意のrecord**を扱う汎用境界であり、扱うkindはhost actionの結果
    （registryの8 variant）に限らない。C-09以降の`REVIEW_RESULT` / `FINAL_REPORT`等も同じ
    経路を通り、これらは**値によるdiscrimination**（`REVIEW_RESULT`の2値、
    `CLARIFICATION_ANSWER`の5値等）を要するためC-10 / C-11の領域である（ADR-0014 決定8）。
    さらに`extra_event_inputs`（`ProgressReport` / `head`）もC-08が作らない値である。

    そこでrecord -> eventの写像は**portが担う**。host actionの結果については、registryの
    `build_event`をそのまま使えば1対1の対応が得られる（Phase 8のfakeがその形を示す）。
    """

    def event_for(self, evidence: RecordEvidence, record: VerifiedRecord) -> Event: ...


class ProcessStopPort(Protocol):
    """走っているprocess treeを停止する（C-03の`stop_tree_by_ref`）。

    `TreeRef`は元のhandleを持たない**別process**からtreeへ到達するためのidentifierで、
    停止は冪等である（既に終了しているtreeも正常結果を返す）。engineは既定値を持たないため
    grace periodは引数で受け取る（解決はC-12）。
    """

    def stop(self, ref: TreeRef, grace_seconds: float) -> StopResult: ...


class StopEscalation(Protocol):
    """2回目のCtrl+Cを受けたかどうか（AC-C03-02の昇格判定）。

    C-03は「1回目 = graceful -> grace -> force、2回目 = 即時force」のうち**停止primitive
    だけ**を提供し、両者の競合の最終確定はC-08が行う（ADR-0005 決定6）。engineはsignalの
    受け取り方を知らないので、必要な事実（force要求の有無）だけをこのportで受け取る。
    """

    @property
    def force_requested(self) -> bool: ...


class RecordBodyPort(Protocol):
    """検証済みpayloadから公開本文（agent発言）を作る。

    schema検証済みJSONを正とし、そこから決定論的にrenderするのがproject全体の方針
    （implementation planのfinal reportと同じ）。kindごとの表現はC-10 / C-11の領域で、
    **C-08はrecordの文面を書かない**。engineはこの結果をC-05の
    `prepare_public_body`（改行正規化 -> sanitize -> redact -> header）へ通す。
    """

    def body_for(self, kind: RecordKind, payload: Mapping[str, object]) -> str: ...
