# SPDX-License-Identifier: Apache-2.0
"""`submit`の受入test（Phase 8。**AC-C08-05** / ADR-0015）。

stale action、異なるhead / run / action kind、path traversal、symlink経由path、size超過
result、hashの異なる重複submitを、いずれも受理せず停止することを固定する。同一内容の
再送は冪等（以前と同じ結果を返す）で、これは停止ではない。
"""

from __future__ import annotations

import json

import pytest
from c08_support.helpers import (
    ACCEPTED_AT,
    HEAD,
    ISSUED_AT,
    MAX_RESULT_BYTES,
    MODEL,
    NEW_HEAD,
    NUMBER,
    REPOSITORY,
    RETRY_BUDGET,
    RUN,
    SPEAKER,
    FakeBodyPort,
    FakeEvidencePort,
    FakeIds,
    FakePayloadPort,
    FakeRecordSource,
    failure_payload,
    fix_result_payload,
    machine_state,
    raw,
    review_records,
    seed,
    submit_payload,
    write_result,
)

from claude_code_codex_review_loop.domain.commands import PersistRecord
from claude_code_codex_review_loop.domain.values import MachineState, RecordKind, State
from claude_code_codex_review_loop.errors import ErrorCategory
from claude_code_codex_review_loop.state import CheckpointLoaded, load_checkpoint
from claude_code_codex_review_loop.workflow import (
    EngineStopped,
    HostActionIssued,
    SubmitAccepted,
    SubmitReplayed,
    advance,
    read_pending_action,
    read_receipts,
    submit,
)


def _issue(env, *, ids=None, state=None):
    outcome = advance(
        paths=env.paths,
        run_id=RUN,
        repository=REPOSITORY,
        number=NUMBER,
        head_sha=HEAD,
        payload_port=FakePayloadPort(),
        evidence_port=FakeEvidencePort(review_records()),
        id_source=ids if ids is not None else FakeIds(),
        issued_at=ISSUED_AT,
    )
    assert isinstance(outcome, HostActionIssued)
    return outcome


def _submit(env, payload, *, records=None, budget=RETRY_BUDGET):
    return submit(
        raw(payload),
        paths=env.paths,
        run_id=RUN,
        repository=REPOSITORY,
        number=NUMBER,
        records_port=FakeRecordSource(review_records() if records is None else records),
        body_port=FakeBodyPort(),
        max_result_bytes=MAX_RESULT_BYTES,
        retry_budget=budget,
        accepted_at=ACCEPTED_AT,
        speaker=SPEAKER,
        model=MODEL,
    )


def _completed(env, issued, *, payload=None, **overrides):
    digest = write_result(
        env.run_dir, issued.action.result_path, fix_result_payload() if payload is None else payload
    )
    return submit_payload(
        action_id=issued.action.action_id,
        nonce=issued.action.nonce,
        result_hash=digest,
        **overrides,
    )


def _failure(env, issued, *, category="TRANSIENT"):
    digest = write_result(env.run_dir, issued.action.result_path, failure_payload(category=category))
    return submit_payload(
        action_id=issued.action.action_id,
        nonce=issued.action.nonce,
        result_hash=digest,
        outcome="FAILED",
        result_kind=None,
        error_category=category,
    )


def _payload_of(env) -> dict[str, object]:
    loaded = load_checkpoint(env.checkpoint)
    assert isinstance(loaded, CheckpointLoaded)
    return loaded.payload


class TestCompleted:
    def test_accepts_and_advances_to_persist(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        outcome = _submit(env, _completed(env, issued))
        assert isinstance(outcome, SubmitAccepted)
        assert outcome.machine_state.pending_record is not None
        assert outcome.commands == (
            PersistRecord(
                kind=RecordKind.FIX_RESULT, binding=outcome.machine_state.pending_record.binding
            ),
        )

    def test_receipt_and_transaction_are_saved_together(self, tmp_path) -> None:
        """receiptとtransactionを別々に書くと、その間のcrashで投稿対象が失われる。"""
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        outcome = _submit(env, _completed(env, issued))
        assert isinstance(outcome, SubmitAccepted) and outcome.transaction is not None
        payload = _payload_of(env)
        assert payload["transaction"]["binding"] == outcome.transaction.binding
        assert read_receipts(payload) == (outcome.receipt,)

    def test_transaction_records_the_completed_body_hash(self, tmp_path) -> None:
        """`body_hash`は必須（ADR-0014 決定21）。resume側の完成形照合が常に効く。"""
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        outcome = _submit(env, _completed(env, issued))
        assert isinstance(outcome, SubmitAccepted) and outcome.transaction is not None
        assert _payload_of(env)["transaction"]["body_hash"] == outcome.transaction.body_hash

    def test_pending_action_is_cleared(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        _submit(env, _completed(env, issued))
        assert read_pending_action(_payload_of(env)) is None

    def test_record_head_comes_from_the_pushed_head(self, tmp_path) -> None:
        """`FIX_RESULT`は新しいheadを対象にする（actionのheadに縛らない）。"""
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        outcome = _submit(env, _completed(env, issued))
        assert isinstance(outcome, SubmitAccepted) and outcome.transaction is not None
        assert outcome.transaction.head_sha == NEW_HEAD

    def test_body_passes_through_the_public_render(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        outcome = _submit(env, _completed(env, issued))
        assert isinstance(outcome, SubmitAccepted) and outcome.transaction is not None
        assert outcome.transaction.body.startswith(f"**{SPEAKER}**（model: {MODEL}）")


class TestIdempotency:
    def test_identical_resend_returns_the_same_result(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        first = _submit(env, payload)
        assert isinstance(first, SubmitAccepted)
        again = _submit(env, payload)
        assert again == SubmitReplayed(receipt=first.receipt)

    def test_replay_does_not_change_the_checkpoint(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        _submit(env, payload)
        before = json.dumps(_payload_of(env), sort_keys=True)
        _submit(env, payload)
        assert json.dumps(_payload_of(env), sort_keys=True) == before

    def test_different_content_for_the_same_attempt_stops(self, tmp_path) -> None:
        """AC-C08-05: hashの異なる重複submitは受理しない。"""
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        _submit(env, payload)
        changed = dict(payload)
        changed["outcome"] = "FAILED"
        changed.pop("result_kind")
        changed["error_category"] = "PERMANENT"
        outcome = _submit(env, changed)
        assert isinstance(outcome, EngineStopped) and outcome.code == "duplicate_mismatch"


class TestBindingRefusals:
    """AC-C08-05のbinding部分（stale / head / run / kind）。"""

    def test_submit_without_a_pending_action_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        outcome = _submit(env, submit_payload(action_id="act-x", nonce="n-x", result_hash="h"))
        assert isinstance(outcome, EngineStopped) and outcome.code == "stale_action"

    def test_stale_action_id_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        payload["action_id"] = "act-old"
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "stale_action"

    def test_stale_nonce_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        payload["nonce"] = "nonce-old"
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "stale_action"

    def test_different_head_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued, head=NEW_HEAD)
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "head_mismatch"

    def test_different_action_kind_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued, action_kind="RECORD_DECISION")
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "kind_mismatch"

    def test_different_run_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued, run_id="run-other")
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "run_mismatch"

    def test_tampered_envelope_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        envelope = dict(issued.envelope)
        envelope["payload"] = {"round": 9, "finding_ids": ["F-9"]}
        issued.envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "envelope_mismatch"


class TestSubmitEnvelope:
    def test_v1_submit_cannot_be_migrated(self, tmp_path) -> None:
        """v1は`result_path`を持たないHOST_ACTION世代の形で、損失なく持ち上げられない。"""
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        payload["schema_version"] = 1
        payload.pop("result_kind")
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "submit_invalid"
        assert "migration_unavailable" in outcome.detail

    def test_completed_without_result_kind_is_rejected(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        payload.pop("result_kind")
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "submit_invalid"


class TestResultFile:
    """AC-C08-05のresult部分（traversal / symlink / size / hash）。"""

    def _retarget(self, env, issued, relative: str) -> None:
        """checkpointのresult pathを差し替える（払い出し以外のpathを拒否することの検証）。"""
        from dataclasses import replace

        from claude_code_codex_review_loop.state import save_checkpoint
        from claude_code_codex_review_loop.workflow import with_retry_attempt

        payload = _payload_of(env)
        save_checkpoint(
            env.checkpoint, with_retry_attempt(payload, replace(issued.action, result_path=relative))
        )

    def test_missing_result_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = submit_payload(
            action_id=issued.action.action_id, nonce=issued.action.nonce, result_hash="h" * 64
        )
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "missing"

    def test_hash_mismatch_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        payload["result_hash"] = "0" * 64
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "result_hash_mismatch"

    def test_size_over_the_limit_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        outcome = submit(
            raw(payload),
            paths=env.paths,
            run_id=RUN,
            repository=REPOSITORY,
            number=NUMBER,
            records_port=FakeRecordSource(review_records()),
            body_port=FakeBodyPort(),
            max_result_bytes=8,
            retry_budget=RETRY_BUDGET,
            accepted_at=ACCEPTED_AT,
            speaker=SPEAKER,
            model=MODEL,
        )
        assert isinstance(outcome, EngineStopped) and outcome.code == "too_large"

    def test_path_traversal_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        self._retarget(env, issued, "../../escape.json")
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "outside_run_directory"

    def test_non_canonical_path_inside_the_run_directory_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        self._retarget(env, issued, f"actions/x/../{issued.action.action_id}/result.json")
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "non_canonical_path"

    def test_absolute_result_path_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        self._retarget(env, issued, str((env.run_dir / issued.action.result_path).resolve()))
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "absolute_path"

    def test_symlinked_result_path_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        link = env.run_dir / "actions" / "link.json"
        try:
            link.symlink_to(env.run_dir / issued.action.result_path)
        except OSError:  # pragma: no cover - symlink作成権限が無い環境
            pytest.skip("symlinkを作成できない環境")
        self._retarget(env, issued, "actions/link.json")
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "non_canonical_path"

    def test_shared_result_entity_stops(self, tmp_path) -> None:
        """hard linkは権限検証を通るが、file実体を外部と共有する（AC-C06-05の維持）。"""
        import os

        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        outside = tmp_path / "outside.json"
        try:
            os.link(env.run_dir / issued.action.result_path, outside)
        except OSError:  # pragma: no cover - hard linkを作成できない環境
            pytest.skip("hard linkを作成できない環境")
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "not_private"

    def test_result_that_fails_schema_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        broken = dict(fix_result_payload())
        broken.pop("pushed_head_sha")
        payload = _completed(env, issued, payload=broken)
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "schema_invalid"


class TestCheckpointRefusals:
    def test_unreadable_pending_action_stops(self, tmp_path) -> None:
        """schemaは通るが解釈できないpending actionを「無い」へ丸めない。"""
        from claude_code_codex_review_loop.state import save_checkpoint

        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        checkpoint = _payload_of(env)
        section = dict(checkpoint["host_action"])
        section["pending"] = {**section["pending"], "attempt": 0}
        checkpoint["host_action"] = section
        save_checkpoint(env.checkpoint, checkpoint)
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "host_action_unavailable"

    def test_checkpoint_without_state_stops(self, tmp_path) -> None:
        from claude_code_codex_review_loop.state import save_checkpoint

        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued)
        checkpoint = _payload_of(env)
        checkpoint.pop("state")
        save_checkpoint(env.checkpoint, checkpoint)
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "state_unavailable"


class TestVariantRules:
    def test_result_kind_outside_the_action_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _completed(env, issued, result_kind="GATE_ANSWER")
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "result_kind_not_allowed"

    def test_record_targeting_another_head_stops(self, tmp_path) -> None:
        """`target_head_sha`を持つrecordは、actionのheadを対象にしなければならない。"""
        from c02_support.helpers import REPRESENTATIVE

        from claude_code_codex_review_loop.schema import SchemaKind

        env = seed(tmp_path, state=machine_state(State.CHANGES_REQUESTED))
        issued = _issue(env)
        request = dict(REPRESENTATIVE[SchemaKind.DECISION_REQUEST])
        request["target_head_sha"] = NEW_HEAD
        payload = _completed(env, issued, payload=request, result_kind="DECISION_REQUEST")
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "record_head_mismatch"

    def test_state_that_does_not_accept_the_kind_stops(self, tmp_path) -> None:
        """registryが許す種別でも、その時のstateがC-01で受理されなければ進めない。"""
        env = seed(tmp_path, state=machine_state(State.CHANGES_REQUESTED))
        issued = _issue(env)
        payload = _completed(env, issued)
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "illegal_event"


class TestTransactionRefusals:
    def test_marker_over_the_limit_stops_before_posting(self, tmp_path) -> None:
        """projectionがmarker上限を超える結果は、投稿前（transaction発行時）に止める。"""
        from c02_support.helpers import REPRESENTATIVE

        from claude_code_codex_review_loop.schema import SchemaKind

        env = seed(tmp_path, state=machine_state(State.CHANGES_REQUESTED))
        issued = _issue(env)
        question = dict(REPRESENTATIVE[SchemaKind.CLARIFICATION_QUESTION])
        question["target_head_sha"] = HEAD
        question["fingerprint"] = "f" * 1000
        question["target_finding"] = "t" * 1000
        payload = _completed(env, issued, payload=question, result_kind="CLARIFICATION_QUESTION")
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "transaction_unavailable"


class TestFailed:
    def test_transient_failure_keeps_the_action_for_retry(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        outcome = _submit(env, _failure(env, issued))
        assert isinstance(outcome, SubmitAccepted)
        assert outcome.retry_available is True
        assert outcome.machine_state == machine_state()
        assert read_pending_action(_payload_of(env)) == issued.action

    def test_retry_issues_a_new_attempt_of_the_same_logical_action(self, tmp_path) -> None:
        """attemptごとに新しいaction IDとnonceを発行し、logical actionはcorrelation IDで結ぶ。"""
        env = seed(tmp_path, state=machine_state())
        ids = FakeIds()
        issued = _issue(env, ids=ids)
        _submit(env, _failure(env, issued))
        retried = _issue(env, ids=ids)
        assert retried.action.correlation_id == issued.action.correlation_id
        assert retried.action.attempt == 2
        assert retried.action.action_id != issued.action.action_id
        assert retried.action.nonce != issued.action.nonce

    def test_old_attempt_can_still_be_replayed_after_a_retry(self, tmp_path) -> None:
        """ledgerは過去attemptを保つ（遅れて届いた同一submitを冪等に扱う）。"""
        env = seed(tmp_path, state=machine_state())
        ids = FakeIds()
        issued = _issue(env, ids=ids)
        failure = _failure(env, issued)
        first = _submit(env, failure)
        assert isinstance(first, SubmitAccepted)
        _issue(env, ids=ids)
        assert _submit(env, failure) == SubmitReplayed(receipt=first.receipt)

    def test_permanent_failure_fails_the_run(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        outcome = _submit(env, _failure(env, issued, category="PERMANENT"))
        assert isinstance(outcome, SubmitAccepted)
        assert outcome.retry_available is False
        assert outcome.machine_state.state is State.FAILED
        assert read_pending_action(_payload_of(env)) is None

    def test_exhausted_budget_fails_the_run(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        outcome = _submit(env, _failure(env, issued), budget=1)
        assert isinstance(outcome, SubmitAccepted)
        assert outcome.retry_available is False and outcome.machine_state.state is State.FAILED

    def test_full_ledger_stops_retrying(self, tmp_path) -> None:
        """budgetが大きくても、ledgerがcheckpointへ収まらない大きさにはしない。"""
        import dataclasses

        from claude_code_codex_review_loop.schema.envelope import MAX_SUBMIT_RECEIPTS
        from claude_code_codex_review_loop.state import save_checkpoint
        from claude_code_codex_review_loop.workflow import SubmitReceipt, with_receipt

        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        filler = SubmitReceipt(
            action_id="old",
            nonce="old",
            outcome="FAILED",
            submit_hash="s" * 64,
            result_hash="r" * 64,
            error_category=ErrorCategory.TRANSIENT,
        )
        payload = _payload_of(env)
        for index in range(MAX_SUBMIT_RECEIPTS - 1):
            payload = with_receipt(
                payload, dataclasses.replace(filler, action_id=f"old-{index}", nonce=f"n-{index}")
            )
        save_checkpoint(env.checkpoint, payload)
        outcome = _submit(env, _failure(env, issued), budget=MAX_SUBMIT_RECEIPTS * 10)
        assert isinstance(outcome, SubmitAccepted)
        assert outcome.retry_available is False and outcome.machine_state.state is State.FAILED

    def test_failure_records_the_category(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        outcome = _submit(env, _failure(env, issued))
        assert isinstance(outcome, SubmitAccepted)
        assert outcome.receipt.error_category is ErrorCategory.TRANSIENT
        assert outcome.receipt.result_kind is None

    def test_failure_summary_is_redacted(self, tmp_path) -> None:
        """失敗詳細はrun directory内のartifactで、redaction対象（AC-C04-01）。"""
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        detail = failure_payload()
        detail["summary"] = "token ghp_0123456789abcdefghijklmnopqrstuvwxyz で失敗した"
        digest = write_result(env.run_dir, issued.action.result_path, detail)
        payload = submit_payload(
            action_id=issued.action.action_id,
            nonce=issued.action.nonce,
            result_hash=digest,
            outcome="FAILED",
            result_kind=None,
            error_category="TRANSIENT",
        )
        outcome = _submit(env, payload)
        assert isinstance(outcome, SubmitAccepted) and outcome.failure_summary is not None
        assert "ghp_" not in outcome.failure_summary and "REDACTED" in outcome.failure_summary

    def test_missing_failure_detail_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = submit_payload(
            action_id=issued.action.action_id,
            nonce=issued.action.nonce,
            result_hash="h" * 64,
            outcome="FAILED",
            result_kind=None,
            error_category="TRANSIENT",
        )
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "missing"

    def test_failure_detail_hash_mismatch_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        payload = _failure(env, issued)
        payload["result_hash"] = "0" * 64
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "result_hash_mismatch"

    def test_category_mismatch_between_submit_and_detail_stops(self, tmp_path) -> None:
        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        digest = write_result(env.run_dir, issued.action.result_path, failure_payload(category="AUTH"))
        payload = submit_payload(
            action_id=issued.action.action_id,
            nonce=issued.action.nonce,
            result_hash=digest,
            outcome="FAILED",
            result_kind=None,
            error_category="TRANSIENT",
        )
        outcome = _submit(env, payload)
        assert isinstance(outcome, EngineStopped) and outcome.code == "failure_mismatch"

    def test_failure_after_the_run_ended_stops(self, tmp_path) -> None:
        """terminal stateへ到達した後に届いた失敗submitでstateを動かさない。"""
        from claude_code_codex_review_loop.state import save_checkpoint
        from claude_code_codex_review_loop.workflow import with_machine_state

        env = seed(tmp_path, state=machine_state())
        issued = _issue(env)
        failure = _failure(env, issued, category="PERMANENT")
        save_checkpoint(env.checkpoint, with_machine_state(_payload_of(env), MachineState(state=State.MERGED)))
        outcome = _submit(env, failure)
        assert isinstance(outcome, EngineStopped) and outcome.code == "illegal_event"
