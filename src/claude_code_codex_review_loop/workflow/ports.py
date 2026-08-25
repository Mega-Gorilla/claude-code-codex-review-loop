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
from ..domain.values import RecordKind
from ..identity.record_chain import VerifiedRecord


@dataclass(frozen=True)
class ActionContext:
    """actionを組み立てる文脈（portへ渡す入力。engineが持つ値だけで構成する）。"""

    action: HostAction
    run_id: str
    repository: str
    number: int
    head_sha: str


class ActionPayloadPort(Protocol):
    """`HOST_ACTION.payload`を供給する（`HOST_ACTION_PAYLOADS`が検証する形）。"""

    def payload_for(self, context: ActionContext) -> Mapping[str, object]: ...


class EvidencePort(Protocol):
    """actionの根拠として同梱する検証済みrecordを供給する（AC-C08-07）。

    対象headの全recordではなく、**そのactionを実行するための根拠**を選ぶ。engineは
    受け取った列をkind許可（`ActionSpec.evidence_kinds`）とseq昇順で検査する。
    """

    def evidence_for(self, context: ActionContext) -> Sequence[VerifiedRecord]: ...


class RecordSourcePort(Protocol):
    """当該runの検証済みrecord列（seq昇順）。binding採番とprev body hashに要る。"""

    def verified_records(self, run_id: str) -> Sequence[VerifiedRecord]: ...


class RecordBodyPort(Protocol):
    """検証済みpayloadから公開本文（agent発言）を作る。

    schema検証済みJSONを正とし、そこから決定論的にrenderするのがproject全体の方針
    （implementation planのfinal reportと同じ）。kindごとの表現はC-10 / C-11の領域で、
    **C-08はrecordの文面を書かない**。engineはこの結果をC-05の
    `prepare_public_body`（改行正規化 -> sanitize -> redact -> header）へ通す。
    """

    def body_for(self, kind: RecordKind, payload: Mapping[str, object]) -> str: ...
