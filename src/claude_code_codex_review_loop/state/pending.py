# SPDX-License-Identifier: Apache-2.0
"""中断したrecordの再発行directive（AC-C07-02。C-07 / ADR-0013）。

投稿前 / 投稿成否不明で中断したturnを、**同一key・同一本文**で再開するための判定。
同一`seq`で再composeした結果がbyte一致しないとC-06のseq conflictで`BLOCKED`になるため
（ADR-0010 決定13）、checkpointの`transaction`に保存した値からmarkerごと再構成する。

- **C-07は投稿しない**。「この本文をこのkeyで投稿する」までをdirectiveとして返し、実行は
  C-01のR-P（pending保持中の明示resume -> `PersistRecord`）を経てC-08が行う。C-05の
  `ensure_comment_posted`が投稿直前にsearch-firstを行うため、重複防止は二重に効く
- **投稿済み判定は検証済みrecordで行う**（marker `key` = `PersistRecord.binding` = ここの
  `binding`。ADR-0010 決定7）。未検証markerを根拠にしない。ただしbindingの一致だけでは
  足りず、**本文が期待する完成形と一致すること**まで確認する（C-06はkeyを本文から
  再導出しないため、「同一key・別本文」のrecordを投稿済みと誤認できてしまう）
- **推測して再投稿しない**: 直前seqの欠落、同一seqの別record、投稿済みrecordの本文不一致、
  再compose結果が記録したbody hashと一致しない場合は、いずれも理由つきで停止する
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..domain.values import RecordKind
from ..identity.errors import IdentityError
from ..identity.record_chain import VerifiedRecord, audit_link_error, compose_record_marker_payload
from ..schema.projection import PROJECTION_KEYS
from ..transport.conversation import body_hash_of
from ..transport.gh import TransportError
from ..transport.marker import attach_marker


@dataclass(frozen=True)
class PendingTransaction:
    """checkpointが保存する中断中recordの値（marker付加**前**の本文を持つ）。"""

    binding: str
    kind: RecordKind
    seq: int
    head_sha: str
    payload_hash: str
    body: str
    projection: dict[str, str | int]
    body_hash: str | None = None
    audit_prev: int | None = None
    audit_prev_hash: str | None = None


@dataclass(frozen=True)
class PendingAbsent:
    """中断中のrecordが無い（checkpointへtransactionが残っていない）。"""


@dataclass(frozen=True)
class PendingAlreadyPosted:
    """同一bindingのrecordがGitHubで確認できた（再投稿しない）。"""

    record: VerifiedRecord


@dataclass(frozen=True)
class PendingReissueRequired:
    """未投稿と確認できた。bodyは**そのまま投稿する完成形**（marker付加済み）。"""

    transaction: PendingTransaction
    body: str
    body_hash: str

    @property
    def idempotency_key(self) -> str:
        """投稿時の検索key（marker `key` = binding。変換規則を持たない）。"""
        return self.transaction.binding


@dataclass(frozen=True)
class PendingUnavailable:
    """再発行の可否を決められない（推測して投稿しない）。"""

    detail: str


PendingOutcome = PendingAbsent | PendingAlreadyPosted | PendingReissueRequired | PendingUnavailable


def _projection_of(section: object) -> dict[str, str | int] | None:
    """transactionのprojectionを読む（許可keyとstr / int値だけを受け取る）。"""
    if not isinstance(section, dict):
        return None
    projection: dict[str, str | int] = {}
    for key, value in section.items():
        if key not in PROJECTION_KEYS or isinstance(value, bool) or not isinstance(value, str | int):
            return None
        projection[key] = value
    return projection


def read_transaction(payload: Mapping[str, object]) -> PendingTransaction | PendingUnavailable | None:
    """checkpoint payloadから`transaction`を読む（無ければNone）。

    schema検証を通ったcheckpointでは解釈できない形にならないが、解釈できない場合に
    「中断中のrecordは無い」へ丸めると重複投稿や取りこぼしの余地を作るため、
    `PendingUnavailable`として提示する（silent repair禁止）。
    """
    section = payload.get("transaction")
    if section is None:
        return None
    if not isinstance(section, dict):
        return PendingUnavailable(detail="transactionがobjectでない")
    values: dict[str, str] = {}
    for name in ("binding", "kind", "head_sha", "payload_hash", "body"):
        value = section.get(name)
        if not isinstance(value, str) or not value:
            return PendingUnavailable(detail=f"transactionの{name}が欠如または不正")
        values[name] = value
    seq = section.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        return PendingUnavailable(detail="transactionのseqが1始まりの整数でない")
    projection = _projection_of(section.get("projection"))
    if projection is None or projection.get("pay") != values["payload_hash"]:
        return PendingUnavailable(detail="transactionのprojectionが欠如または payload hashと不一致")
    body_hash = section.get("body_hash")
    try:
        kind = RecordKind(values["kind"])
    except ValueError:
        return PendingUnavailable(detail="transactionのkindが未知の種別")
    anchor = section.get("audit_prev")
    anchor_hash = section.get("audit_prev_hash")
    if "audit_prev" in section:
        error = audit_link_error(kind, seq, anchor, anchor_hash)
        if error is not None:
            return PendingUnavailable(detail=error)
        if not isinstance(body_hash, str) or not body_hash:
            return PendingUnavailable(detail="incident linkは完成本文hashを必要とする")
    elif "audit_prev_hash" in section:
        return PendingUnavailable(detail="audit_prev_hashにはaudit_prevが必要")
    return PendingTransaction(
        binding=values["binding"],
        kind=kind,
        seq=seq,
        head_sha=values["head_sha"],
        payload_hash=values["payload_hash"],
        body=values["body"],
        projection=projection,
        body_hash=body_hash if isinstance(body_hash, str) else None,
        audit_prev=anchor if isinstance(anchor, int) else None,
        audit_prev_hash=anchor_hash if isinstance(anchor_hash, str) else None,
    )


def evaluate_pending(
    transaction: PendingTransaction, *, run_id: str, records: Sequence[VerifiedRecord]
) -> PendingOutcome:
    """中断中recordの再発行可否を判定する（pure）。

    通常は直前seqの検証済みhash、incidentの明示linkは保存済みanchor/hashから再構成する。
    明示anchorも現在の検証済み先行recordと照合し、変化していれば投稿前に停止する。

    - 同一`seq`を別bindingのrecordが占有していればseq conflictとして停止する
    - 同一bindingのrecordがあっても、**本文が期待する完成形と一致しなければ停止する**。
      C-06はmarkerのkeyを本文から再導出しないため、ここで照合しないと「同一key・別本文」の
      recordを投稿済みとして受理し、中断したturnの内容が永久にGitHubへ載らない
      （AC-C07-02の「同一key ⇒ 同一本文」に反する）。body hashはmarker行（kind・head・
      seq・prev・projection）まで覆うため、この1回の照合で全要素の一致を判定できる
    - 未投稿なら、記録したbody hashとの一致を確認してからdirectiveを返す
    """
    by_seq = {record.seq: record for record in records}
    occupant = by_seq.get(transaction.seq)
    if occupant is not None and occupant.key != transaction.binding:
        return PendingUnavailable(
            detail=f"seq {transaction.seq}を別bindingのrecordが占有している（{occupant.key}）"
        )
    previous: str | None = None
    if transaction.audit_prev is not None:
        error = audit_link_error(transaction.kind, transaction.seq, transaction.audit_prev, transaction.audit_prev_hash)
        if error is not None:
            return PendingUnavailable(detail=error)
        anchor = max((n for n in by_seq if n < transaction.seq), default=0)
        if anchor != transaction.audit_prev:
            return PendingUnavailable(detail="incidentの保存済みanchorが検証済み先行recordと一致しない")
        previous = transaction.audit_prev_hash
        if anchor and by_seq[anchor].body_hash != previous:
            return PendingUnavailable(detail="incidentの保存済みanchor hashが一致しない")
        if transaction.body_hash is None:
            return PendingUnavailable(detail="incident linkは完成本文hashを必要とする")
    elif transaction.seq >= 2:
        earlier = by_seq.get(transaction.seq - 1)
        if earlier is None:
            return PendingUnavailable(detail=f"直前のseq {transaction.seq - 1}がchainに無い")
        previous = earlier.body_hash
    try:
        payload = compose_record_marker_payload(
            key=transaction.binding,
            kind=transaction.kind,
            run_id=run_id,
            head_sha=transaction.head_sha,
            seq=transaction.seq,
            prev_body_hash=previous,
            projection=transaction.projection,
            audit_prev=transaction.audit_prev,
        )
        body = attach_marker(transaction.body, payload)
    except (IdentityError, TransportError) as error:
        return PendingUnavailable(detail=f"markerを再構成できない: {error}")
    digest = body_hash_of(body)
    if transaction.body_hash is not None and transaction.body_hash != digest:
        return PendingUnavailable(detail="再composeした本文が記録したbody hashと一致しない")
    if occupant is not None:
        if occupant.body_hash != digest:
            return PendingUnavailable(
                detail=f"seq {transaction.seq}の投稿済みrecordの本文がtransactionと一致しない"
            )
        return PendingAlreadyPosted(record=occupant)
    return PendingReissueRequired(transaction=transaction, body=body, body_hash=digest)
