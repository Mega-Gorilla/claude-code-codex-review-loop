# SPDX-License-Identifier: Apache-2.0
"""中断recordの再発行directiveの受入test（**AC-C07-02**。ADR-0013）。

- 未投稿なら「同一key・同一本文」で再発行できるdirectiveを返す（本文はbyte一致）
- 投稿済みなら再投稿しない
- 直前seqの欠落・seqの占有・byte不一致は、いずれも推測せず停止する
"""

from __future__ import annotations

import pytest
from c06_support.helpers import HEAD, PRODUCER, make_comment, marker_payload
from c07_support.helpers import RUN, chain_comments_of, pending_fixture, verified_chain

from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.identity import ProducerAllowlist, verify_record_chain
from claude_code_codex_review_loop.identity.record_chain import VerifiedRecord
from claude_code_codex_review_loop.state import (
    PendingAlreadyPosted,
    PendingReissueRequired,
    PendingTransaction,
    PendingUnavailable,
    evaluate_pending,
    read_transaction,
)
from claude_code_codex_review_loop.transport.conversation import body_hash_of
from claude_code_codex_review_loop.transport.marker import attach_marker

_K = RecordKind
_QUESTION = _K.CLARIFICATION_QUESTION


def _records(count: int) -> tuple[VerifiedRecord, ...]:
    """seq=1..countの検証済みchain（既に投稿済みのrecord列）。"""
    verification = verified_chain([_K.REVIEW_RESULT] * count)
    assert verification.is_intact
    return verification.records


def _prev_hash(count: int) -> str:
    return body_hash_of(chain_comments_of([_K.REVIEW_RESULT] * count)[-1].body)


def _read(payload: dict[str, object]) -> PendingTransaction:
    transaction = read_transaction({"transaction": payload})
    assert isinstance(transaction, PendingTransaction)
    return transaction


class TestReadTransaction:
    def test_absent_section_is_none(self) -> None:
        assert read_transaction({}) is None

    def test_reads_all_fields(self) -> None:
        fixture = pending_fixture(seq=2, prev=_prev_hash(1))
        transaction = _read(fixture.transaction)
        assert (transaction.binding, transaction.seq, transaction.kind) == (fixture.binding, 2, _QUESTION)
        assert transaction.head_sha == HEAD and transaction.body == "record 2"
        assert transaction.projection["pay"] == transaction.payload_hash

    def test_optional_body_hash_may_be_absent(self) -> None:
        transaction = _read(pending_fixture(seq=1, include_body_hash=False).transaction)
        assert transaction.body_hash is None

    @pytest.mark.parametrize(
        "mutate",
        [
            {"binding": ""},
            {"kind": "NOT_A_KIND"},
            {"seq": 0},
            {"seq": True},
            {"head_sha": 1},
            {"projection": "x"},
            {"projection": {"pay": "0" * 64, "unknown": "x"}},
            {"projection": {"pay": "0" * 64, "round": True}},
            {"payload_hash": "f" * 64},
        ],
        ids=[
            "empty_binding", "unknown_kind", "seq_zero", "seq_bool", "head_not_str",
            "projection_not_object", "projection_unknown_key", "projection_bool", "hash_mismatch",
        ],
    )
    def test_uninterpretable_transaction_is_not_reduced_to_absent(self, mutate: dict[str, object]) -> None:
        """解釈できないtransactionを「中断中のrecordは無い」へ丸めない（silent repair禁止）。"""
        payload = dict(pending_fixture(seq=1).transaction)
        payload.update(mutate)
        assert isinstance(read_transaction({"transaction": payload}), PendingUnavailable)

    def test_non_object_transaction_is_reported(self) -> None:
        assert isinstance(read_transaction({"transaction": "x"}), PendingUnavailable)


class TestEvaluatePending:
    def test_unposted_record_produces_a_byte_identical_directive(self) -> None:
        """**AC-C07-02の核**: 再composeした本文が、中断前の完成形とbyte一致する。"""
        fixture = pending_fixture(seq=2, prev=_prev_hash(1))
        outcome = evaluate_pending(_read(fixture.transaction), run_id=RUN, records=_records(1))
        assert isinstance(outcome, PendingReissueRequired)
        assert outcome.body == fixture.body
        assert outcome.body_hash == body_hash_of(fixture.body)
        assert outcome.idempotency_key == fixture.binding

    def test_genesis_record_has_no_prev(self) -> None:
        fixture = pending_fixture(seq=1)
        outcome = evaluate_pending(_read(fixture.transaction), run_id=RUN, records=())
        assert isinstance(outcome, PendingReissueRequired) and outcome.body == fixture.body

    def test_posted_record_is_not_reissued(self) -> None:
        """同一bindingのrecordがGitHubで確認できれば再投稿しない。"""
        records = _records(2)
        transaction = _read(
            pending_fixture(
                seq=2, prev=_prev_hash(1), kind=_K.REVIEW_RESULT, text="record 2"
            ).transaction
        )
        outcome = evaluate_pending(transaction, run_id=RUN, records=records)
        assert isinstance(outcome, PendingAlreadyPosted)
        assert outcome.record.seq == 2 and outcome.record.key == transaction.binding

    def test_same_binding_with_a_different_body_stops(self) -> None:
        """**同一key・別本文**のrecordを投稿済みとして受理しない（AC-C07-02の契約）。

        C-06はmarkerのkeyを本文から再導出しないため、intactなchainでもこの差異は
        chain検証では検出できない。ここで照合しないと、中断したturnの内容が永久に
        GitHubへ載らないままtransactionが消費される。
        """
        prev = _prev_hash(1)
        fixture = pending_fixture(seq=2, prev=prev)
        forged = attach_marker(
            "DIFFERENT BODY",
            marker_payload(kind=_QUESTION, run_id=RUN, head=HEAD, seq=2, prev=prev, body="record 2"),
        )
        verification = verify_record_chain(
            (*chain_comments_of([_K.REVIEW_RESULT]), make_comment(2002, forged)),
            run_id=RUN,
            detection_head=HEAD,
            producers=ProducerAllowlist(logins=frozenset({PRODUCER})),
            checkpoint=None,
            probes={},
        )
        assert verification.is_intact  # 正規producerの正規markerなのでchain検証は通る
        assert verification.records[1].key == fixture.binding  # 同一key
        outcome = evaluate_pending(_read(fixture.transaction), run_id=RUN, records=verification.records)
        assert isinstance(outcome, PendingUnavailable) and "一致しない" in outcome.detail

    def test_other_record_on_the_same_seq_stops(self) -> None:
        """同一seqを別bindingのrecordが占有していれば停止する（seq conflictを作らない）。"""
        fixture = pending_fixture(seq=2, prev=_prev_hash(1))
        outcome = evaluate_pending(_read(fixture.transaction), run_id=RUN, records=_records(2))
        assert isinstance(outcome, PendingUnavailable) and "占有" in outcome.detail

    def test_missing_previous_record_stops(self) -> None:
        """直前seqがchainに無ければprevを決められない（推測しない）。"""
        fixture = pending_fixture(seq=3, prev=_prev_hash(2))
        outcome = evaluate_pending(_read(fixture.transaction), run_id=RUN, records=_records(1))
        assert isinstance(outcome, PendingUnavailable) and "直前" in outcome.detail

    def test_body_hash_mismatch_stops(self) -> None:
        """記録したbody hashと再compose結果が違えば、同一keyで同一本文にならない。"""
        payload = dict(pending_fixture(seq=2, prev=_prev_hash(1)).transaction)
        payload["body_hash"] = "0" * 64
        outcome = evaluate_pending(_read(payload), run_id=RUN, records=_records(1))
        assert isinstance(outcome, PendingUnavailable) and "一致しない" in outcome.detail

    def test_prev_change_is_detected_through_the_body_hash(self) -> None:
        """chainが進んでprevが変われば、記録した本文と一致しなくなり停止する。"""
        fixture = pending_fixture(seq=2, prev="f" * 64)
        outcome = evaluate_pending(_read(fixture.transaction), run_id=RUN, records=_records(1))
        assert isinstance(outcome, PendingUnavailable)

    def test_oversized_marker_stops(self) -> None:
        """compose時の上限違反（ADR-0007）を成功と推測しない。"""
        payload = dict(pending_fixture(seq=1).transaction)
        projection = dict(payload["projection"])  # type: ignore[arg-type]
        projection["tgt"] = "x" * 4096
        payload["projection"] = projection
        outcome = evaluate_pending(_read(payload), run_id=RUN, records=())
        assert isinstance(outcome, PendingUnavailable) and "再構成できない" in outcome.detail
