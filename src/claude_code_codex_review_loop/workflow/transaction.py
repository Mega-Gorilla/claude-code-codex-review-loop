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

保存する`body`は**marker付加前**のredact済みrender出力である（C-07の`read_transaction`が
そう読む）。marker行はresume時に`projection`と直前recordのbody hashから再構成する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..domain.values import RecordKind
from ..identity.errors import IdentityError
from ..identity.record_chain import VerifiedRecord, compose_record_marker_payload
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
) -> TransactionOutcome:
    """検証済みpayloadとrender済み本文からtransactionを発行する（pure）。

    `records`は当該runの検証済みrecord列（seq昇順）。直前recordのbody hashがmarkerの
    `prev`になるため、chainに欠けがあるとtransactionを発行しない。
    """
    seq = next_sequence(records)
    previous: str | None = None
    if seq >= 2:
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
    )


def transaction_section(issued: IssuedTransaction) -> dict[str, object]:
    """checkpointの`transaction` sectionへ保存する形（C-07の`read_transaction`が読む）。"""
    return {
        "binding": issued.binding,
        "kind": issued.kind.value,
        "seq": issued.seq,
        "head_sha": issued.head_sha,
        "payload_hash": issued.payload_hash,
        "body": issued.body,
        "body_hash": issued.body_hash,
        "projection": dict(issued.projection),
    }
