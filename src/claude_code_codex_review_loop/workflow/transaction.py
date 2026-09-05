# SPDX-License-Identifier: Apache-2.0
"""record transactionの発行（Phase 8。ADR-0010 / ADR-0014 決定21 / ADR-0015）。

投稿の**前に**checkpointへ保存する値を作る。順序を変えるとcrash windowで同一keyを
再現できないため、ADR-0010が定めた順序をそのまま実装する。

```
render済みbody -> build_record_projection -> derive_record_binding
  -> compose_record_marker_payload -> attach_marker -> body_hash_of
```

`body_hash`はschema上optionalだが（既存fieldの制約を強化しないため。ADR-0013 決定9）、
**新しいproducerは常に保存する**（ADR-0014 決定21）。marker付加後の完成本文hashは投稿前に
計算でき、省略する理由が無い。これによりresume側（C-07の`evaluate_pending`）の完成形照合が
常に効く。

保存する`body`は**marker付加前**のredact済みrender出力である。通常は直前recordから、
incidentは保存済みaudit_prevとhashからmarkerを再構成する（ADR-0024）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..domain import events as ev
from ..domain.commands import Command
from ..domain.machine import transition
from ..domain.values import (
    MachineState,
    OpaqueBinding,
    RecordingIncidentProcedure,
    RecordKind,
    TransitionRejected,
)
from ..identity.errors import IdentityError
from ..identity.record_chain import (
    ChainVerification,
    VerifiedRecord,
    compose_record_marker_payload,
)
from ..schema.projection import ProjectionError, build_record_projection, derive_record_binding
from ..transport.conversation import body_hash_of
from ..transport.gh import TransportError
from ..transport.marker import attach_marker


@dataclass(frozen=True)
class IssuedTransaction:
    """発行したtransaction（checkpointへ保存する値と、投稿する完成本文）。"""

    binding: str
    kind: RecordKind
    seq: int
    head_sha: str
    payload_hash: str
    body: str
    projection: dict[str, str | int]
    body_hash: str
    marked_body: str
    audit_prev: int | None = None
    audit_prev_hash: str | None = None


@dataclass(frozen=True)
class TransactionUnavailable:
    """transactionを発行できない（推測して投稿しない）。"""

    detail: str


TransactionOutcome = IssuedTransaction | TransactionUnavailable


def next_sequence(records: Sequence[VerifiedRecord]) -> int:
    """次に採番するsequence（検証済みchainの最大seq + 1。空chainなら1）。"""
    return max((record.seq for record in records), default=0) + 1


def issue_transaction(
    *,
    kind: RecordKind,
    payload: Mapping[str, object],
    run_id: str,
    head_sha: str,
    body: str,
    records: Sequence[VerifiedRecord],
    audit_chain: ChainVerification | None = None,
) -> TransactionOutcome:
    """検証済みpayloadとrender済み本文からtransactionを発行する（pure）。

    通常は検証済み列の末尾へ連結する。incident用audit_chainを渡す場合は、観測最大と
    checkpointのhigh-waterを超えて採番し、検証済み末尾を明示anchorとして固定する。
    """
    seq = next_sequence(records)
    previous: str | None = None
    anchor: int | None = None
    if audit_chain is not None:
        anchor = max((record.seq for record in records), default=0)
        seq = max(anchor, audit_chain.max_seq, audit_chain.assurance_high_water) + 1
        previous = next((record.body_hash for record in records if record.seq == anchor), None)
    elif seq >= 2:
        earlier = {record.seq: record for record in records}.get(seq - 1)
        if earlier is None:  # pragma: no cover - next_sequenceの定義上到達しない
            return TransactionUnavailable(detail=f"直前のseq {seq - 1}がchainに無い")
        previous = earlier.body_hash
    try:
        projection = build_record_projection(kind, payload, head_sha=head_sha, body=body)
        payload_hash = str(projection["pay"])
        binding = derive_record_binding(
            run_id=run_id, seq=seq, kind=kind, head_sha=head_sha, payload_hash=payload_hash
        )
        marker = compose_record_marker_payload(
            key=binding,
            kind=kind,
            run_id=run_id,
            head_sha=head_sha,
            seq=seq,
            prev_body_hash=previous,
            projection=projection,
            audit_prev=anchor,
        )
        marked = attach_marker(body, marker)
    except (ProjectionError, IdentityError, TransportError) as error:
        return TransactionUnavailable(detail=f"transactionを発行できない: {error}")
    return IssuedTransaction(
        binding=binding,
        kind=kind,
        seq=seq,
        head_sha=head_sha,
        payload_hash=payload_hash,
        body=body,
        projection=projection,
        body_hash=body_hash_of(marked),
        marked_body=marked,
        audit_prev=anchor,
        audit_prev_hash=previous if anchor is not None else None,
    )


@dataclass(frozen=True)
class ProducedRecord:
    """transactionを発行し、C-01を`RecordProduced`まで進めた結果。"""

    transaction: IssuedTransaction
    machine_state: MachineState
    commands: tuple[Command, ...]


@dataclass(frozen=True)
class ProduceRejected:
    """recordを作れない（推測して投稿しない）。codeは診断とtestの安定した識別子。"""

    code: str
    detail: str


ProduceOutcome = ProducedRecord | ProduceRejected


def _is_integrity_audit(machine_state: MachineState, kind: RecordKind) -> bool:
    """整合性の監査記録そのものか（incident record かつ incident記録中）。

    C-01の`INCIDENT_PENDING_SCOPE`不変条件が両者を結び付けているが、`produce_record`は
    pendingが**まだ無い**時点で呼ばれるため、ここでは手続きを直接見る。
    """
    return kind is RecordKind.INTEGRITY_INCIDENT and isinstance(
        machine_state.procedure, RecordingIncidentProcedure
    )


def produce_record(
    machine_state: MachineState,
    *,
    kind: RecordKind,
    payload: Mapping[str, object],
    run_id: str,
    head_sha: str,
    body: str,
    chain: ChainVerification,
) -> ProduceOutcome:
    """検証済みpayloadとrender済み本文から、投稿待ちのrecordを1件作る。

    host actionの結果（`engine`）とユーザー入力の転記（`user_input`）は、本文の作り方と
    受理の記録だけが違い、**採番から`RecordProduced`までは同じ**である。分けて書くと
    chain gateやC-01の受理判定が2箇所へ散るため、ここへ集約する。

    **incident recordだけはchainのviolationで拒否しない**（ADR-0024 決定1）。それを記録する
    ためのrecordを「chainが壊れている」という理由で作れないのは循環だからである。取りこぼしが
    起きないことは別の2点が担保する: `persist`が未知のviolationを投稿前にC-01へ渡すことと、
    C-01が「全violationが検証済みrecordへ含まれるまでterminalへ進まない」こと（AC-C01-12）。
    """
    if not chain.is_intact and not _is_integrity_audit(machine_state, kind):
        # 壊れたchainの上でseqとprevを決めない（integrityの解消はC-01のblockが扱う）
        return ProduceRejected("chain_violation", f"chainにviolationがある（{len(chain.violations)}件）")
    issued = issue_transaction(
        kind=kind,
        payload=payload,
        run_id=run_id,
        head_sha=head_sha,
        body=body,
        records=chain.records,
        audit_chain=chain if _is_integrity_audit(machine_state, kind) else None,
    )
    if isinstance(issued, TransactionUnavailable):
        return ProduceRejected("transaction_unavailable", issued.detail)
    try:
        updated, commands = transition(
            machine_state, ev.RecordProduced(kind=kind, binding=OpaqueBinding(issued.binding))
        )
    except (TransitionRejected, ev.IllegalEventError) as error:
        return ProduceRejected("illegal_event", f"C-01が結果を受理しない: {error}")
    return ProducedRecord(transaction=issued, machine_state=updated, commands=commands)


def transaction_section(issued: IssuedTransaction) -> dict[str, object]:
    """checkpointの`transaction` sectionへ保存する形（C-07の`read_transaction`が読む）。"""
    section: dict[str, object] = {
        "binding": issued.binding,
        "kind": issued.kind.value,
        "seq": issued.seq,
        "head_sha": issued.head_sha,
        "payload_hash": issued.payload_hash,
        "body": issued.body,
        "body_hash": issued.body_hash,
        "projection": dict(issued.projection),
    }
    if issued.audit_prev is not None:
        section["audit_prev"] = issued.audit_prev
        if issued.audit_prev_hash is not None:
            section["audit_prev_hash"] = issued.audit_prev_hash
    return section
