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
            "records": [
                {
                    "comment_id": "c-1",
                    "review_id": "rv-1",
                    "thread_id": "th-1",
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


def test_unknown_section_is_rejected() -> None:
    payload = dict(_BASE)
    payload["future_section"] = {}
    result = validate(CHECKPOINT, _raw(payload))
    assert not result.ok
    assert any(e.code == "unknown_field" for e in result.errors)
