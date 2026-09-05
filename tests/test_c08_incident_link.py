# SPDX-License-Identifier: Apache-2.0
"""incident専用linkのproducer/verifier/resume契約と拒否条件（Issue #50）。"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from c06_support.helpers import HEAD, RUN, make_comment
from c07_support.helpers import chain_comments_of, verified_chain
from test_c06_record_chain import _verify
from test_c08_incident import _incident_env, violation

from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.identity import IdentityError
from claude_code_codex_review_loop.identity.record_chain import (
    ChainPayload,
    compose_record_marker_payload,
    parse_record_marker,
)
from claude_code_codex_review_loop.state import (
    PendingReissueRequired,
    PendingTransaction,
    PendingUnavailable,
    evaluate_pending,
    read_transaction,
)
from claude_code_codex_review_loop.transport.marker import attach_marker
from claude_code_codex_review_loop.workflow import (
    EngineStopped,
    IssuedTransaction,
    issue_transaction,
    persist,
    transaction_section,
)


def _issue(count=1):
    chain = replace(verified_chain([RecordKind.REVIEW_RESULT] * count), max_seq=3, assurance_high_water=5)
    issued = issue_transaction(
        kind=RecordKind.INTEGRITY_INCIDENT,
        payload={"schema_version": 1, "violation_bindings": ["iv:gap:run-1:s00000005"], "summary": "監査"},
        run_id=RUN, head_sha=HEAD, body="監査", records=chain.records, audit_chain=chain,
    )
    assert isinstance(issued, IssuedTransaction), issued
    return issued, chain


def _transaction(count=1):
    issued, chain = _issue(count)
    pending = read_transaction({"transaction": transaction_section(issued)})
    assert isinstance(pending, PendingTransaction)
    return issued, pending, chain


@pytest.mark.parametrize("count", [0, 1])
def test_saved_anchor_reproduces_exact_body(count):
    issued, pending, chain = _transaction(count)
    assert issued.seq == 6
    assert issued.audit_prev == count
    parsed = parse_record_marker(make_comment(3000, issued.marked_body))
    assert isinstance(parsed, ChainPayload) and parsed.audit_prev == count
    resumed = evaluate_pending(pending, run_id=RUN, records=chain.records)
    assert isinstance(resumed, PendingReissueRequired)
    assert resumed.body == issued.marked_body
    assert resumed.body_hash == issued.body_hash


@pytest.mark.parametrize("anchor,previous", [(True, None), (-1, None), (2, "a" * 64), (0, "a" * 64), (1, None)])
def test_producer_rejects_invalid_audit_links(anchor, previous):
    with pytest.raises(IdentityError):
        compose_record_marker_payload(
            key="k", kind=RecordKind.INTEGRITY_INCIDENT, run_id=RUN, head_sha=HEAD,
            seq=2, prev_body_hash=previous, audit_prev=anchor,
        )


def test_normal_record_cannot_use_audit_link():
    with pytest.raises(IdentityError, match="INTEGRITY_INCIDENT"):
        compose_record_marker_payload(
            key="k", kind=RecordKind.REVIEW_RESULT, run_id=RUN, head_sha=HEAD,
            seq=2, prev_body_hash=None, audit_prev=0,
        )


@pytest.mark.parametrize("changes", [
    {"audit_prev": True}, {"audit_prev": None}, {"audit_prev": -1}, {"audit_prev": 6},
    {"audit_prev": 0, "prev": None}, {"kind": "REVIEW_RESULT"}, {"prev": "bad"},
])
def test_parser_rejects_malformed_or_wrong_kind_link(changes):
    issued, _ = _issue()
    parsed = make_comment(3000, issued.marked_body).marker.payload
    payload = dict(parsed)
    payload.update(changes)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    body = "監査\n<!-- CC_REVIEW_META:v1 " + raw + " -->"
    assert isinstance(parse_record_marker(make_comment(3000, body)), str)


@pytest.mark.parametrize("changes", [
    {"audit_prev": True}, {"audit_prev": -1}, {"audit_prev": 0},
    {"audit_prev_hash": "bad"}, {"kind": "REVIEW_RESULT"}, {"body_hash": None},
])
def test_reader_rejects_invalid_link(changes):
    issued, _ = _issue()
    section = transaction_section(issued)
    section.update(changes)
    assert isinstance(read_transaction({"transaction": section}), PendingUnavailable)


def test_hash_without_anchor_is_not_silently_ignored():
    issued, _ = _issue()
    section = transaction_section(issued)
    section.pop("audit_prev")
    assert isinstance(read_transaction({"transaction": section}), PendingUnavailable)


@pytest.mark.parametrize("changes", [
    {"kind": RecordKind.REVIEW_RESULT}, {"body_hash": None},
    {"audit_prev_hash": "b" * 64}, {"audit_prev": 0, "audit_prev_hash": None},
])
def test_resume_rejects_changed_anchor_and_unsigned_links(changes):
    _, pending, chain = _transaction()
    result = evaluate_pending(replace(pending, **changes), run_id=RUN, records=chain.records)
    assert isinstance(result, PendingUnavailable)


def test_verifier_rejects_skipping_an_intact_record():
    issued, _ = _issue(0)
    # audit_prev=0はseq=1が検証済みなら許されない。
    chain = _verify((*chain_comments_of([RecordKind.REVIEW_RESULT]), make_comment(3000, issued.marked_body)))
    assert any(ref.binding.value == "iv:chain:run-1:s00000006" for ref in chain.violations)
    assert all(record.kind is not RecordKind.INTEGRITY_INCIDENT for record in chain.records)


def test_verifier_checks_the_anchor_hash():
    issued, _ = _issue()
    payload = dict(make_comment(3000, issued.marked_body).marker.payload)
    payload["prev"] = "b" * 64
    comment = make_comment(3000, attach_marker("監査", payload))
    chain = _verify((*chain_comments_of([RecordKind.REVIEW_RESULT]), comment))
    assert any(ref.binding.value == "iv:chain:run-1:s00000006" for ref in chain.violations)


def test_link_fields_are_covered_by_the_saved_body_hash():
    issued, pending, _ = _transaction()
    # 別の有効な形に改変しても、保存済み完成本文hashとの照合で止める。
    changed = replace(pending, audit_prev=0, audit_prev_hash=None)
    result = evaluate_pending(changed, run_id=RUN, records=())
    assert isinstance(result, PendingUnavailable) and "body hash" in result.detail


def test_an_old_incident_transaction_cannot_reuse_an_observed_number(tmp_path):
    known = violation()
    env = _incident_env(tmp_path, deferred=(known,))
    chain = replace(
        verified_chain([RecordKind.REVIEW_RESULT]),
        max_seq=env.issued.seq, violations=(known,),
    )
    before = env.comment_count()
    result = persist(**env.kwargs(records_port=SimpleNamespace(chain=lambda run: chain)))
    assert isinstance(result, EngineStopped) and result.code == "incident_sequence_occupied"
    assert env.comment_count() == before
