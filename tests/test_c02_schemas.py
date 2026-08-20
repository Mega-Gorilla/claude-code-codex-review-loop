# SPDX-License-Identifier: Apache-2.0
"""C-02の全schema（29 kind）の受入test（AC-C02-01）。

kindごとに、representative payloadの受理と、未知version / 必須field欠落 / 型不一致 /
size超過 / cross-field違反が**区別できるerror**になることを検証する。
"""

from __future__ import annotations

import json

import pytest

from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.schema import REGISTRY, SchemaKind, validate

_SHA = "0123abcd"


def _finding(*, blocking: bool = True) -> dict[str, object]:
    return {
        "id": "F-1",
        "fingerprint": "fp-1",
        "problem": "境界条件でoff-by-oneになる",
        "severity": "HIGH",
        "blocking": blocking,
        "file": "src/app.py",
        "line": 42,
    }


REPRESENTATIVE: dict[SchemaKind, dict[str, object]] = {
    SchemaKind.REVIEW_RESULT: {
        "schema_version": 1,
        "target_head_sha": _SHA,
        "round": 1,
        "verdict": "CHANGES_REQUESTED",
        "findings": [_finding()],
        "verification_runs": [{"command": "pytest -q", "result": "pass"}],
    },
    SchemaKind.FIX_RESULT: {
        "schema_version": 1,
        "summary": "findingへ同意し境界条件を修正",
        "pre_head_sha": _SHA,
        "pushed_head_sha": "4567beef",
        "dispositions": [{"finding_id": "F-1", "disposition": "fixed"}],
        "tests": [{"command": "pytest -q", "result": "pass", "duration_ms": 900}],
        "dirty_before": False,
        "dirty_after": False,
    },
    SchemaKind.CLARIFICATION_QUESTION: {
        "schema_version": 1,
        "run_id": "run-1",
        "fingerprint": "fp-1",
        "turn": 1,
        "target_head_sha": _SHA,
        "target_finding": "F-1",
        "question": "この指摘は非同期経路にも適用されますか",
        "grounds": "同期経路のみ再現を確認",
        "expected_confirmation": "適用範囲の確認",
    },
    SchemaKind.CLARIFICATION_ANSWER: {
        "schema_version": 1,
        "run_id": "run-1",
        "fingerprint": "fp-1",
        "turn": 1,
        "target_head_sha": _SHA,
        "result": "CONFIRMED",
        "rationale": "非同期経路でも同じ競合が起こる",
    },
    SchemaKind.DECISION_REQUEST: {
        "schema_version": 1,
        "target_head_sha": _SHA,
        "reason": "外部仕様の選択が残る",
        "constraints": "後方互換を維持する",
        "candidates": [
            {"content": "案A", "pros": "単純", "cons": "拡張性が低い", "impact": "小"}
        ],
        "recommendation": "案A",
    },
    SchemaKind.DECISION_VERDICT: {
        "schema_version": 1,
        "target_head_sha": _SHA,
        "verdict": "ASK_USER",
        "rationale": "ユーザー体験の選択が残る",
    },
    SchemaKind.DECISION_BRIEF: {
        "schema_version": 1,
        "decision_id": "D-run1-001",
        "target_head_sha": _SHA,
        "question": "APIの互換性方針",
        "constraints_and_impact": "既存clientへ影響",
        "candidates": [
            {
                "number": 1,
                "content": "案A",
                "pros": "単純",
                "cons": "拡張性",
                "recommended": True,
            }
        ],
        "claude_position": "案Aを推奨",
        "codex_review": "判断が必要と判定",
        "disagreements": "なし",
        "recommendation": "案A（Recommended）",
        "how_to_answer": "[1]または自由記述",
    },
    SchemaKind.DECISION_RECORD: {
        "schema_version": 1,
        "decision_id": "D-run1-001",
        "target_head_sha": _SHA,
        "considered": "互換性方針",
        "initial_position": "案A",
        "verdict": "PROCEED_WITH_RECORD",
        "verdict_rationale": "既存方針から決定できる",
        "adopted_implementation": "案Aを実装",
    },
    SchemaKind.USER_DECISION: {
        "schema_version": 1,
        "decision_id": "D-run1-001",
        "target_head_sha": _SHA,
        "answer": "[1]で進めてください",
        "input_route": "github_comment",
    },
    SchemaKind.FOLLOWUP_CANDIDATES: {
        "schema_version": 1,
        "repository": "owner/repo",
        "number": 12,
        "target_head_sha": _SHA,
        "candidates": [
            {
                "candidate_id": "FU-1",
                "title": "log整備",
                "background": "調査に時間がかかった",
                "scope": "log追加",
                "out_of_scope": "log基盤の刷新",
                "acceptance_criteria": ["主要経路にlogがある"],
            }
        ],
    },
    SchemaKind.FOLLOWUP_EVALUATION: {
        "schema_version": 1,
        "target_head_sha": _SHA,
        "evaluations": [
            {"candidate_id": "FU-1", "verdict": "CREATE_ISSUE", "reason": "追跡価値がある"}
        ],
    },
    SchemaKind.FOLLOWUP_PERMISSION: {
        "schema_version": 1,
        "candidate_id": "FU-1",
        "candidate_fingerprint": "fp-fu-1",
        "body_hash": "hash-1",
        "status": "APPROVED",
        "input_route": "github_comment",
        "approval_comment_id": "c-100",
        "created_issue_url": "https://example.invalid/issues/99",
    },
    SchemaKind.FINAL_REPORT: {
        "schema_version": 1,
        "language": "ja",
        "summary": "境界条件の修正",
        "why": "off-by-oneの解消",
        "user_visible_changes": ["エラーが出なくなる"],
        "acceptance_criteria": [
            {"criterion": "testが通る", "result": "pass", "evidence": "pytest -q"}
        ],
        "review_history": [
            {
                "round": 1,
                "findings": [{"description": "off-by-one", "resolution": "修正済み"}],
            }
        ],
        "approved_head_sha": _SHA,
        "local_tests": [{"command": "pytest -q", "result": "pass"}],
        "ci_results": [{"check_name": "CI", "result": "pass"}],
        "remaining_risks": ["負荷試験は未実施"],
        "followups": [
            {
                "candidate_id": "FU-1",
                "title": "log整備",
                "codex_evaluation": "CREATE_ISSUE",
                "permission_status": "APPROVED",
            }
        ],
        "pre_merge_checks": ["headが承認対象と一致"],
        "merged": False,
    },
    SchemaKind.MERGE_INTENT: {
        "schema_version": 1,
        "intent": "APPROVE_MERGE",
        "repository": "owner/repo",
        "pr_number": 12,
        "target_head_sha": _SHA,
        "input_route": "powershell",
    },
    SchemaKind.MERGE_APPROVAL: {
        "schema_version": 1,
        "repository": "owner/repo",
        "pr_number": 12,
        "approved_head_sha": _SHA,
        "merge_method": "merge",
        "intent": "APPROVE_MERGE",
        "input_route": "powershell",
        "recorded_at": "2026-08-20T12:00:00Z",
        "comment_id": "c-200",
    },
    SchemaKind.MERGE_OUTCOME: {
        "schema_version": 1,
        "repository": "owner/repo",
        "pr_number": 12,
        "approved_head_sha": _SHA,
        "merged_commit_sha": "89ab0123",
        "merge_method": "merge",
        "approval_record_url": "https://example.invalid/pr/12#c-200",
        "verified_on_github": True,
    },
    SchemaKind.GATE_QUESTION: {
        "schema_version": 1,
        "target_head_sha": _SHA,
        "body": "riskの詳細を教えてください",
        "input_route": "github_comment",
    },
    SchemaKind.GATE_ANSWER: {
        "schema_version": 1,
        "target_head_sha": _SHA,
        "body": "riskは負荷試験の未実施のみです",
    },
    SchemaKind.GATE_CHANGES: {
        "schema_version": 1,
        "target_head_sha": _SHA,
        "body": "docstringを追加してください",
        "input_route": "github_comment",
    },
    SchemaKind.HOST_ACTION: {
        "schema_version": 1,
        "run_id": "run-1",
        "action_id": "act-1",
        "action_kind": "APPLY_FINDINGS",
        "repository": "owner/repo",
        "number": 12,
        "expected_head_sha": _SHA,
        "payload_hash": "ph-1",
        "nonce": "nonce-1",
        "verified_records": [{"comment_id": "c-1", "head_sha": _SHA}],
        "payload": {"findings": ["F-1"]},
    },
    SchemaKind.SUBMIT: {
        "schema_version": 1,
        "run_id": "run-1",
        "action_id": "act-1",
        "action_kind": "APPLY_FINDINGS",
        "expected_head_sha": _SHA,
        "nonce": "nonce-1",
        "result_hash": "rh-1",
        "outcome": "COMPLETED",
    },
    SchemaKind.PERMISSION_BLOCK: {
        "schema_version": 1,
        "permission_id": "perm-1",
        "tool": "Bash(git push)",
        "reason": "初回のpush許可が必要",
        "risk": "remoteへの書込",
        "repository": "owner/repo",
        "number": 12,
        "target_head_sha": _SHA,
        "requested_scope": "push once",
        "resume_command": "同じSkillをresume",
    },
    SchemaKind.CI_TIMEOUT: {
        "schema_version": 1,
        "target_head_sha": _SHA,
        "checks": [{"check_name": "CI", "url": "https://example.invalid/run/1"}],
        "waited_seconds": 1200,
    },
    SchemaKind.CI_CODE_FAILURE: {
        "schema_version": 1,
        "target_head_sha": _SHA,
        "checks": [{"check_name": "CI", "result": "fail"}],
        "summary": "test失敗",
    },
    SchemaKind.EXTERNAL_DEPENDENCY: {
        "schema_version": 1,
        "target_head_sha": _SHA,
        "description": "外部APIのrate limit解除待ち",
    },
    SchemaKind.BLOCK_INTERVENTION: {
        "schema_version": 1,
        "target_block_binding": "blk-1",
        "target_head_sha": _SHA,
        "body": "追加evidenceを提示し膠着を解消",
        "input_route": "github_comment",
    },
    SchemaKind.INTEGRITY_INCIDENT: {
        "schema_version": 1,
        "violation_bindings": ["v-1", "v-2"],
        "summary": "canonical commentの改変を検出",
        "audit_reference": {"kind": "REVIEW_RESULT", "binding": "r-9"},
    },
    SchemaKind.USER_CANCEL: {
        "schema_version": 1,
        "target_head_sha": _SHA,
        "input_route": "powershell",
        "reason": "方針変更のため中断",
    },
    SchemaKind.CHECKPOINT: {
        "schema_version": 1,
        "run_id": "run-1",
        "repository": "owner/repo",
        "number": 12,
        "state": {"state": "RUNNING_REVIEW", "round": 1},
        "heads": {"base_sha": "aaa", "observed_sha": _SHA},
    },
}

# kindごとの「必須fieldの代表」（欠落・型不一致のprobeに使う）
REQUIRED_PROBE: dict[SchemaKind, str] = {
    SchemaKind.REVIEW_RESULT: "verdict",
    SchemaKind.FIX_RESULT: "pushed_head_sha",
    SchemaKind.CLARIFICATION_QUESTION: "question",
    SchemaKind.CLARIFICATION_ANSWER: "result",
    SchemaKind.DECISION_REQUEST: "reason",
    SchemaKind.DECISION_VERDICT: "verdict",
    SchemaKind.DECISION_BRIEF: "decision_id",
    SchemaKind.DECISION_RECORD: "adopted_implementation",
    SchemaKind.USER_DECISION: "answer",
    SchemaKind.FOLLOWUP_CANDIDATES: "candidates",
    SchemaKind.FOLLOWUP_EVALUATION: "evaluations",
    SchemaKind.FOLLOWUP_PERMISSION: "status",
    SchemaKind.FINAL_REPORT: "approved_head_sha",
    SchemaKind.MERGE_INTENT: "intent",
    SchemaKind.MERGE_APPROVAL: "approved_head_sha",
    SchemaKind.MERGE_OUTCOME: "merged_commit_sha",
    SchemaKind.GATE_QUESTION: "body",
    SchemaKind.GATE_ANSWER: "body",
    SchemaKind.GATE_CHANGES: "body",
    SchemaKind.HOST_ACTION: "nonce",
    SchemaKind.SUBMIT: "result_hash",
    SchemaKind.PERMISSION_BLOCK: "permission_id",
    SchemaKind.CI_TIMEOUT: "waited_seconds",
    SchemaKind.CI_CODE_FAILURE: "summary",
    SchemaKind.EXTERNAL_DEPENDENCY: "description",
    SchemaKind.BLOCK_INTERVENTION: "target_block_binding",
    SchemaKind.INTEGRITY_INCIDENT: "violation_bindings",
    SchemaKind.USER_CANCEL: "input_route",
    SchemaKind.CHECKPOINT: "run_id",
}

ALL_KINDS = tuple(SchemaKind)


def _raw(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def test_every_kind_has_representative_and_probe() -> None:
    assert set(REPRESENTATIVE) == set(SchemaKind)
    assert set(REQUIRED_PROBE) == set(SchemaKind)
    assert set(REGISTRY) == set(SchemaKind)


def test_all_c01_record_kinds_have_schemas() -> None:
    """C-01のcanonical record全21種がschemaを持つ（schemaの分散を作らない）。"""
    schema_names = {kind.value for kind in SchemaKind}
    for record_kind in RecordKind:
        assert record_kind.value in schema_names, record_kind


@pytest.mark.parametrize("kind", ALL_KINDS, ids=[k.value for k in ALL_KINDS])
class TestPerKind:
    def test_representative_is_accepted(self, kind: SchemaKind) -> None:
        result = validate(REGISTRY[kind], _raw(REPRESENTATIVE[kind]))
        assert result.ok, (kind, result.stage, result.errors)

    def test_unknown_version_is_a_version_error(self, kind: SchemaKind) -> None:
        payload = dict(REPRESENTATIVE[kind])
        payload["schema_version"] = 999
        result = validate(REGISTRY[kind], _raw(payload))
        assert (result.ok, result.stage) == (False, "version")
        assert {e.code for e in result.errors} == {"unknown_version"}

    def test_missing_required_field_is_distinguishable(self, kind: SchemaKind) -> None:
        payload = dict(REPRESENTATIVE[kind])
        probe = REQUIRED_PROBE[kind]
        del payload[probe]
        result = validate(REGISTRY[kind], _raw(payload))
        assert (result.ok, result.stage) == (False, "schema"), kind
        assert any(e.code == "required_missing" and e.path == probe for e in result.errors), (
            kind,
            result.errors,
        )

    def test_type_mismatch_is_distinguishable(self, kind: SchemaKind) -> None:
        payload = dict(REPRESENTATIVE[kind])
        probe = REQUIRED_PROBE[kind]
        payload[probe] = 12.5  # どのfieldでも型不一致になる値（floatはintでもない）
        result = validate(REGISTRY[kind], _raw(payload))
        assert (result.ok, result.stage) == (False, "schema"), kind
        assert any(e.code == "type_mismatch" and e.path == probe for e in result.errors), (
            kind,
            result.errors,
        )

    def test_size_limit_is_enforced(self, kind: SchemaKind) -> None:
        definition = REGISTRY[kind]
        raw = _raw(REPRESENTATIVE[kind]) + b" " * definition.max_input_bytes
        result = validate(definition, raw)
        assert (result.ok, result.stage) == (False, "size")

    def test_unknown_field_is_rejected_with_ordinal_token(self, kind: SchemaKind) -> None:
        payload = dict(REPRESENTATIVE[kind])
        payload["totally_unknown_field"] = "x"
        result = validate(REGISTRY[kind], _raw(payload))
        assert (result.ok, result.stage) == (False, "schema")
        assert any(e.code == "unknown_field" and "<unknown#" in e.path for e in result.errors)


class TestCrossFieldRules:
    """cross-field違反が「区別できるerror」になる（AC-C02-01）。"""

    def _reject(self, kind: SchemaKind, payload: dict[str, object], path: str) -> None:
        result = validate(REGISTRY[kind], _raw(payload))
        assert (result.ok, result.stage) == (False, "schema"), (kind, result.errors)
        assert any(e.code == "cross_field" and e.path == path for e in result.errors), result.errors

    def test_review_verdict_must_match_blocking_findings(self) -> None:
        approved_with_blocking = dict(REPRESENTATIVE[SchemaKind.REVIEW_RESULT])
        approved_with_blocking["verdict"] = "APPROVED"
        self._reject(SchemaKind.REVIEW_RESULT, approved_with_blocking, "verdict")
        changes_without_blocking = dict(REPRESENTATIVE[SchemaKind.REVIEW_RESULT])
        changes_without_blocking["findings"] = [_finding(blocking=False)]
        self._reject(SchemaKind.REVIEW_RESULT, changes_without_blocking, "verdict")
        approved_clean = dict(REPRESENTATIVE[SchemaKind.REVIEW_RESULT])
        approved_clean["verdict"] = "APPROVED"
        approved_clean["findings"] = []
        assert validate(REGISTRY[SchemaKind.REVIEW_RESULT], _raw(approved_clean)).ok

    def test_clarification_revised_requires_revised_finding(self) -> None:
        payload = dict(REPRESENTATIVE[SchemaKind.CLARIFICATION_ANSWER])
        payload["result"] = "REVISED"
        self._reject(SchemaKind.CLARIFICATION_ANSWER, payload, "revised_finding")
        payload["revised_finding"] = _finding()
        assert validate(REGISTRY[SchemaKind.CLARIFICATION_ANSWER], _raw(payload)).ok

    def test_clarification_more_evidence_requires_detail(self) -> None:
        payload = dict(REPRESENTATIVE[SchemaKind.CLARIFICATION_ANSWER])
        payload["result"] = "MORE_EVIDENCE_REQUIRED"
        self._reject(SchemaKind.CLARIFICATION_ANSWER, payload, "required_evidence")
        payload["required_evidence"] = "負荷試験の再現条件"
        assert validate(REGISTRY[SchemaKind.CLARIFICATION_ANSWER], _raw(payload)).ok

    def test_decision_candidates_must_not_be_empty(self) -> None:
        payload = dict(REPRESENTATIVE[SchemaKind.DECISION_REQUEST])
        payload["candidates"] = []
        self._reject(SchemaKind.DECISION_REQUEST, payload, "candidates")

    def test_brief_requires_a_recommended_candidate(self) -> None:
        payload = json.loads(json.dumps(REPRESENTATIVE[SchemaKind.DECISION_BRIEF]))
        payload["candidates"][0]["recommended"] = False
        self._reject(SchemaKind.DECISION_BRIEF, payload, "candidates")

    def test_link_existing_requires_existing_issue(self) -> None:
        payload = json.loads(json.dumps(REPRESENTATIVE[SchemaKind.FOLLOWUP_EVALUATION]))
        payload["evaluations"][0]["verdict"] = "LINK_EXISTING"
        self._reject(SchemaKind.FOLLOWUP_EVALUATION, payload, "evaluations[0].existing_issue")
        payload["evaluations"][0]["existing_issue"] = "#45"
        assert validate(REGISTRY[SchemaKind.FOLLOWUP_EVALUATION], _raw(payload)).ok

    def test_followup_candidates_are_capped_at_three(self) -> None:
        payload = json.loads(json.dumps(REPRESENTATIVE[SchemaKind.FOLLOWUP_CANDIDATES]))
        payload["candidates"] = payload["candidates"] * 4
        result = validate(REGISTRY[SchemaKind.FOLLOWUP_CANDIDATES], _raw(payload))
        assert any(e.code == "max_items" and e.path == "candidates" for e in result.errors)

    def test_final_report_must_be_pre_merge(self) -> None:
        payload = dict(REPRESENTATIVE[SchemaKind.FINAL_REPORT])
        payload["merged"] = True
        self._reject(SchemaKind.FINAL_REPORT, payload, "merged")

    def test_question_intent_requires_body(self) -> None:
        payload = dict(REPRESENTATIVE[SchemaKind.MERGE_INTENT])
        payload["intent"] = "QUESTION"
        self._reject(SchemaKind.MERGE_INTENT, payload, "body")
        payload["body"] = "riskの詳細は？"
        assert validate(REGISTRY[SchemaKind.MERGE_INTENT], _raw(payload)).ok

    def test_ambiguous_affirmation_cannot_be_an_approval(self) -> None:
        """approval recordのintentはAPPROVE_MERGE以外を受理しない（曖昧な肯定の遮断）。"""
        payload = dict(REPRESENTATIVE[SchemaKind.MERGE_APPROVAL])
        payload["intent"] = "QUESTION"
        result = validate(REGISTRY[SchemaKind.MERGE_APPROVAL], _raw(payload))
        assert any(e.code == "enum_invalid" and e.path == "intent" for e in result.errors)

    def test_failed_submit_requires_error_category(self) -> None:
        payload = dict(REPRESENTATIVE[SchemaKind.SUBMIT])
        payload["outcome"] = "FAILED"
        self._reject(SchemaKind.SUBMIT, payload, "error_category")
        payload["error_category"] = "network"
        assert validate(REGISTRY[SchemaKind.SUBMIT], _raw(payload)).ok

    def test_incident_violations_must_not_be_empty(self) -> None:
        payload = dict(REPRESENTATIVE[SchemaKind.INTEGRITY_INCIDENT])
        payload["violation_bindings"] = []
        self._reject(SchemaKind.INTEGRITY_INCIDENT, payload, "violation_bindings")

    def test_rules_do_not_mask_type_errors_on_their_inputs(self) -> None:
        """cross-field ruleの入力fieldが型不一致の場合、型errorが報告されruleは黙る。"""
        review = dict(REPRESENTATIVE[SchemaKind.REVIEW_RESULT])
        review["findings"] = 123
        result = validate(REGISTRY[SchemaKind.REVIEW_RESULT], _raw(review))
        assert {e.code for e in result.errors} == {"type_mismatch"}
        brief = dict(REPRESENTATIVE[SchemaKind.DECISION_BRIEF])
        brief["candidates"] = 123
        result = validate(REGISTRY[SchemaKind.DECISION_BRIEF], _raw(brief))
        assert {e.code for e in result.errors} == {"type_mismatch"}
