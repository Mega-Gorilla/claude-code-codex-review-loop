# SPDX-License-Identifier: Apache-2.0
"""checkpoint envelopeの受入test。

target experience 10.1の18項目のsection写像と、C-01のMachineState（可視state）の
表現可能性を検証する。fieldの追加は利用するPhaseが行う（envelope構造はそれを
additiveに受ける）。
"""

from __future__ import annotations

import json

from test_c01_registry import enumerate_machine_states

from claude_code_codex_review_loop.domain.states import State
from claude_code_codex_review_loop.schema import REGISTRY, SchemaKind, validate
from claude_code_codex_review_loop.schema.projection import PROJECTION_KEYS

CHECKPOINT = REGISTRY[SchemaKind.CHECKPOINT]

_BASE: dict[str, object] = {
    "schema_version": 1,
    "run_id": "run-1",
    "repository": "owner/repo",
    "number": 12,
}


def _raw(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def test_minimal_envelope_is_only_the_required_frame() -> None:
    assert validate(CHECKPOINT, _raw(dict(_BASE))).ok


def test_sections_cover_target_experience_10_1() -> None:
    """TE 10.1の18項目（識別の外枠1 + 16 section）がv1 specへ写像されている。"""
    fields = CHECKPOINT.versions[1].fields
    expected_sections = {
        "heads",
        "state",
        "ledger",
        "conversation",
        "clarification",
        "coder",
        "tests",
        "ci",
        "artifacts",
        "mutation",
        "error",
        "decision",
        "followup",
        "permission",
        "reviewer",
        "merge",
    }
    assert expected_sections <= set(fields)
    for name in expected_sections:
        assert not fields[name].required, name  # sectionはすべてoptional（Phase別に追加）


def test_every_c01_state_is_representable() -> None:
    """C-01の到達可能な全MachineStateの可視stateをstate sectionへ格納できる。"""
    states_seen: set[State] = set()
    for ms in enumerate_machine_states():
        payload = dict(_BASE)
        payload["state"] = {"state": ms.state.value}
        result = validate(CHECKPOINT, _raw(payload))
        assert result.ok, (ms.state, result.errors)
        states_seen.add(ms.state)
    assert states_seen == set(State)


def test_full_envelope_with_all_sections_is_accepted() -> None:
    payload: dict[str, object] = {
        **_BASE,
        "heads": {"base_sha": "aaa", "observed_sha": "bbb", "approved_sha": "ccc"},
        "state": {"state": "MERGING", "round": 2, "agent_role": "controller", "session_id": "s-1"},
        "ledger": {"findings": [{"id": "F-1", "fingerprint": "fp-1", "resolution": "fixed"}]},
        "conversation": {
            "cursor": "c-99",
            "high_water_mark": 7,
            "records": [
                {
                    "comment_id": "c-1",
                    "review_id": "rv-1",
                    "thread_id": "th-1",
                    "seq": 7,
                    "kind": "REVIEW_RESULT",
                    "url": "https://example.invalid/c-1",
                    "body_hash": "h-1",
                    "author_role": "codex",
                    "head_sha": "bbb",
                }
            ],
        },
        "clarification": {"counter": 2, "fingerprint": "fp-1"},
        "coder": {
            "pre_head_sha": "aaa",
            "post_head_sha": "bbb",
            "pushed_head_sha": "bbb",
            "dirty_before": False,
            "dirty_after": False,
        },
        "tests": [{"command": "pytest -q", "cwd": ".", "result": "pass", "duration_ms": 1200}],
        "ci": [{"check_name": "CI", "result": "pass", "url": "https://example.invalid/run"}],
        "artifacts": ["runs/run-1/report.json"],
        "mutation": {
            "last_success": "post-comment",
            "idempotency_marker": "m-1",
            "read_after_write_verified": True,
        },
        "error": {"category": "transient", "resume_point": "WAITING_CI", "resume_command": "cc-review pr 12"},
        "decision": {
            "decision_id": "D-1",
            "claude_position": "案A",
            "codex_position": "判断必要",
            "user_answer": "[1]",
            "answer_head_sha": "bbb",
            "answer_comment_id": "c-77",
            "answer_body_hash": "h-77",
        },
        "followup": [
            {
                "candidate_id": "FU-1",
                "fingerprint": "fp-fu",
                "body_hash": "h-fu",
                "dedupe_result": "unique",
                "verdict": "CREATE_ISSUE",
                "permission_record": "c-51",
                "created_issue_url": "https://example.invalid/issues/99",
            }
        ],
        "permission": {
            "mode": "auto",
            "profile": "default",
            "permission_id": "perm-1",
            "blocked_tool": "Bash(git push)",
            "risk": "remote write",
            "requested_scope": "push once",
            "user_changes": "allow push",
            "resume_checkpoint": "ck-9",
        },
        "reviewer": {
            "checkout_path": "runs/run-1/checkout",
            "sandbox_profile": "readonly",
            "network_profile": "web-search",
            "executed": "pytest -q",
            "dirty_before": False,
            "dirty_after": False,
            "discard_result": "clean",
        },
        "merge": {
            "intent": "APPROVE_MERGE",
            "pr_number": 12,
            "approved_head_sha": "ccc",
            "input_route": "powershell",
            "approval_comment_id": "c-200",
            "approval_body_hash": "h-200",
            "candidate_fingerprint": "fp-cand",
            "approval_binding": "ud:MERGE_APPROVAL:o/r#12:ccc:merge:fp-cand:c200",
            "merge_method": "merge",
            "api_result": "success",
            "merged_commit_sha": "ddd",
            "verified_result": "merged",
        },
    }
    result = validate(CHECKPOINT, _raw(payload))
    assert result.ok, result.errors


def test_checkpoint_accepts_large_payloads_up_to_its_limit() -> None:
    """checkpointの入力上限は既定（64 KiB）より広い（ADR-0004）。"""
    payload = dict(_BASE)
    payload["artifacts"] = ["a" * 1_000] * 100  # 約100 KB
    raw = _raw(payload)
    assert len(raw) > 65_536
    assert validate(CHECKPOINT, raw).ok


def test_chain_checkpoint_fields_reject_wrong_types() -> None:
    """Phase 6 additive field（high_water_mark / records[].seq）の型違反は拒否される。"""
    payload = dict(_BASE)
    payload["conversation"] = {"high_water_mark": "seven"}
    result = validate(CHECKPOINT, _raw(payload))
    assert not result.ok

    payload["conversation"] = {"records": [{"comment_id": "c-1", "seq": "seven"}]}
    result = validate(CHECKPOINT, _raw(payload))
    assert not result.ok


def test_unknown_section_is_rejected() -> None:
    payload = dict(_BASE)
    payload["future_section"] = {}
    result = validate(CHECKPOINT, _raw(payload))
    assert not result.ok
    assert any(e.code == "unknown_field" for e in result.errors)


def test_phase7_state_fields_are_accepted() -> None:
    """中断点の再開に要るfield（Phase 7のadditive追加）が受理される。"""
    payload = dict(_BASE)
    payload["state"] = {
        "state": "AWAITING_USER_DECISION",
        "awaiting": "USER_INPUT_DECISION",
        "return_to": "RUNNING_REVIEW",
        "recovery_to": "APPLYING_FIXES",
        "pending_record": {
            "kind": "DECISION_BRIEF",
            "binding": "cr:run-1:00000004:" + "a" * 64,
            "source_state": "REVIEWING_DECISION_REQUEST",
        },
    }
    assert validate(CHECKPOINT, _raw(payload)).ok


def test_phase7_transaction_section_is_accepted() -> None:
    """crash windowの再発行に要る値（ADR-0010 決定13）を保持できる。"""
    payload = dict(_BASE)
    payload["transaction"] = {
        "binding": "cr:run-1:00000004:" + "a" * 64,
        "kind": "CLARIFICATION_QUESTION",
        "seq": 4,
        "head_sha": "b" * 40,
        "payload_hash": "c" * 64,
        "body": "公開本文（redact済みのrender出力）",
        "body_hash": "d" * 64,
    }
    assert validate(CHECKPOINT, _raw(payload)).ok


def test_phase7_artifact_records_bind_to_approved_head() -> None:
    """artifactがapproved headとrecordへbindされる（AC-C07-05の器）。"""
    payload = dict(_BASE)
    payload["artifacts"] = ["logs/reviewer.log"]
    payload["artifact_records"] = [
        {
            "path": "artifacts/review.json",
            "kind": "REVIEW_RESULT",
            "content_hash": "e" * 64,
            "approved_head_sha": "b" * 40,
            "record_binding": "cr:run-1:00000004:" + "a" * 64,
        }
    ]
    assert validate(CHECKPOINT, _raw(payload)).ok


def test_artifacts_item_type_is_unchanged() -> None:
    """既存`artifacts`はtext arrayのまま（item型変更は非互換になるため）。"""
    payload = dict(_BASE)
    payload["artifacts"] = [{"path": "artifacts/review.json"}]
    assert not validate(CHECKPOINT, _raw(payload)).ok


def test_unknown_state_field_is_still_rejected() -> None:
    payload = dict(_BASE)
    payload["state"] = {"state": "MERGED", "unknown_field": "x"}
    assert not validate(CHECKPOINT, _raw(payload)).ok


def test_phase7_transaction_carries_the_projection() -> None:
    """再composeにprojectionが要る（ADR-0010 決定13）。keyはC-02のprojectionと同じ集合。"""
    payload = dict(_BASE)
    payload["transaction"] = {
        "binding": "cr:run-1:00000004:" + "a" * 64,
        "kind": "CLARIFICATION_QUESTION",
        "seq": 4,
        "head_sha": "b" * 40,
        "payload_hash": "c" * 64,
        "body": "公開本文",
        "body_hash": "d" * 64,
        "projection": {"pay": "c" * 64, "turn": 2, "fp": "fp-1", "tgt": "f-1"},
    }
    assert validate(CHECKPOINT, _raw(payload)).ok


def test_transaction_projection_declares_every_projection_key() -> None:
    """宣言したfield集合がC-02のPROJECTION_KEYSと一致する（drift防止）。"""
    fields = CHECKPOINT.versions[1].fields["transaction"].fields
    assert fields is not None
    assert set(fields["projection"].fields or {}) == set(PROJECTION_KEYS)


def test_transaction_projection_rejects_unknown_keys() -> None:
    payload = dict(_BASE)
    payload["transaction"] = {
        "binding": "cr:run-1:00000004:" + "a" * 64,
        "kind": "CLARIFICATION_QUESTION",
        "seq": 4,
        "head_sha": "b" * 40,
        "payload_hash": "c" * 64,
        "body": "公開本文",
        "projection": {"pay": "c" * 64, "unknown": "x"},
    }
    assert not validate(CHECKPOINT, _raw(payload)).ok


def test_transaction_projection_requires_the_payload_hash() -> None:
    payload = dict(_BASE)
    payload["transaction"] = {
        "binding": "cr:run-1:00000004:" + "a" * 64,
        "kind": "CLARIFICATION_QUESTION",
        "seq": 4,
        "head_sha": "b" * 40,
        "payload_hash": "c" * 64,
        "body": "公開本文",
        "projection": {"turn": 2},
    }
    assert not validate(CHECKPOINT, _raw(payload)).ok


def _host_action_section(**overrides: object) -> dict[str, object]:
    section: dict[str, object] = {
        "action_id": "act-1",
        "action_kind": "APPLY_FINDINGS",
        "nonce": "nonce-1",
        "expected_head_sha": "b" * 40,
        "result_path": "actions/act-1.result.json",
        "envelope_path": "actions/act-1.action.json",
        "envelope_hash": "e" * 64,
    }
    section.update(overrides)
    return section


def test_phase8_host_action_section_holds_the_pending_action() -> None:
    """未完了actionの完全なfingerprint（AC-C08-06のresumeに要る）。"""
    payload = dict(_BASE)
    payload["host_action"] = _host_action_section(
        issued_at="2026-08-24T12:00:00Z",
        submit={
            "outcome": "COMPLETED",
            "submit_hash": "s" * 64,
            "result_hash": "rh-1",
            "result_kind": "FIX_RESULT",
        },
    )
    assert validate(CHECKPOINT, _raw(payload)).ok


def test_host_action_section_records_a_failed_submit() -> None:
    payload = dict(_BASE)
    payload["host_action"] = _host_action_section(
        submit={"outcome": "FAILED", "submit_hash": "s" * 64, "result_hash": "rh-1",
                "error_category": "TRANSIENT"}
    )
    assert validate(CHECKPOINT, _raw(payload)).ok


def test_host_action_section_requires_the_envelope_fingerprint() -> None:
    """payload / verified recordsの違いを潰さないため、envelope全体のhashを必須にする。"""
    for missing in ("envelope_path", "envelope_hash", "result_path", "nonce"):
        payload = dict(_BASE)
        section = _host_action_section()
        del section[missing]
        payload["host_action"] = section
        assert not validate(CHECKPOINT, _raw(payload)).ok, missing


def test_host_action_submit_requires_its_own_fingerprint() -> None:
    payload = dict(_BASE)
    payload["host_action"] = _host_action_section(
        submit={"outcome": "COMPLETED", "result_hash": "rh-1"}
    )
    assert not validate(CHECKPOINT, _raw(payload)).ok


def test_host_action_section_rejects_unknown_kind() -> None:
    """checkpointのaction kindもC-01のHostActionから導出した値域に従う。"""
    payload = dict(_BASE)
    payload["host_action"] = _host_action_section(action_kind="IMPLEMENT_ISSUE")
    assert not validate(CHECKPOINT, _raw(payload)).ok


def test_host_action_section_rejects_free_form_error_category() -> None:
    payload = dict(_BASE)
    payload["host_action"] = _host_action_section(
        submit={"outcome": "FAILED", "submit_hash": "s" * 64, "result_hash": "rh-1",
                "error_category": "TRANSIET"}
    )
    assert not validate(CHECKPOINT, _raw(payload)).ok
