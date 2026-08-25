# SPDX-License-Identifier: Apache-2.0
"""record transaction発行の受入test（Phase 8。ADR-0010 / ADR-0014 決定21 / ADR-0015）。

C-08が発行したtransactionを、**C-07の`read_transaction` -> `evaluate_pending`がそのまま
読める**ことを固定する。producer（C-08）とresume（C-07）の契約をcodeで結び、片側だけの
変更をfailにする。
"""

from __future__ import annotations

from c06_support.helpers import HEAD
from c07_support.helpers import RUN, verified_chain
from c08_support.helpers import NEW_HEAD, fix_result_payload

from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.state import (
    PendingReissueRequired,
    PendingTransaction,
    evaluate_pending,
    read_transaction,
)
from claude_code_codex_review_loop.transport.conversation import body_hash_of
from claude_code_codex_review_loop.workflow import (
    IssuedTransaction,
    TransactionUnavailable,
    issue_transaction,
    next_sequence,
    transaction_section,
)

_BODY = "**Claude Code**（model: claude-opus-5）\n\n修正を適用しました。"


def _issue(records=(), *, payload=None, head=NEW_HEAD):
    return issue_transaction(
        kind=RecordKind.FIX_RESULT,
        payload=fix_result_payload() if payload is None else payload,
        run_id=RUN,
        head_sha=head,
        body=_BODY,
        records=records,
    )


class TestSequence:
    def test_empty_chain_starts_at_one(self) -> None:
        assert next_sequence(()) == 1

    def test_continues_after_the_highest_sequence(self) -> None:
        assert next_sequence(verified_chain([RecordKind.REVIEW_RESULT]).records) == 2


class TestIssue:
    def test_binding_and_body_hash_are_derived(self) -> None:
        issued = _issue()
        assert isinstance(issued, IssuedTransaction)
        assert issued.binding.startswith("cr:")
        assert issued.body_hash == body_hash_of(issued.marked_body)

    def test_marked_body_keeps_the_rendered_text(self) -> None:
        issued = _issue()
        assert isinstance(issued, IssuedTransaction)
        assert issued.body == _BODY and _BODY in issued.marked_body

    def test_projection_carries_the_payload_hash(self) -> None:
        issued = _issue()
        assert isinstance(issued, IssuedTransaction)
        assert issued.projection["pay"] == issued.payload_hash

    def test_head_mismatch_is_reported(self) -> None:
        """payloadの対象headとmarkerのheadが食い違うrecordを作らせない。"""
        issued = _issue(head=HEAD)
        assert isinstance(issued, TransactionUnavailable)
        assert "transactionを発行できない" in issued.detail


class TestSectionRoundTrip:
    def test_section_is_readable_by_resume(self) -> None:
        records = verified_chain([RecordKind.REVIEW_RESULT]).records
        issued = _issue(records)
        assert isinstance(issued, IssuedTransaction)
        transaction = read_transaction({"transaction": transaction_section(issued)})
        assert isinstance(transaction, PendingTransaction)
        assert transaction.binding == issued.binding
        assert transaction.body_hash == issued.body_hash

    def test_resume_reproduces_the_same_completed_body(self) -> None:
        """同一seqで再composeした本文がbyte一致する（C-06のseq conflictを避ける）。"""
        records = verified_chain([RecordKind.REVIEW_RESULT]).records
        issued = _issue(records)
        assert isinstance(issued, IssuedTransaction)
        transaction = read_transaction({"transaction": transaction_section(issued)})
        assert isinstance(transaction, PendingTransaction)
        outcome = evaluate_pending(transaction, run_id=RUN, records=records)
        assert isinstance(outcome, PendingReissueRequired)
        assert outcome.body == issued.marked_body
        assert outcome.idempotency_key == issued.binding

    def test_body_hash_is_always_saved(self) -> None:
        """schema上はoptionalだが、新しいproducerは常に保存する（ADR-0014 決定21）。"""
        issued = _issue()
        assert isinstance(issued, IssuedTransaction)
        assert transaction_section(issued)["body_hash"] == issued.body_hash
