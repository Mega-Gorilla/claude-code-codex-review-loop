# SPDX-License-Identifier: Apache-2.0
"""C-02 schemaのrepresentative payload corpus（kindごとの妥当な最小例）。

`test_c02_schemas.py`のAC-C02-01検証と、C-07 projection codecのtestが同じcorpusを
共有する（payload例を二重管理しない）。
"""

from __future__ import annotations

from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.schema import SchemaKind
from claude_code_codex_review_loop.schema.projection import PROJECTION_SPECS, schema_kind_of

SHA = "0123abcd"


def finding(*, blocking: bool = True) -> dict[str, object]:
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
        "target_head_sha": SHA,
        "round": 1,
        "verdict": "CHANGES_REQUESTED",
        "findings": [finding()],
        "verification_runs": [{"command": "pytest -q", "result": "pass"}],
    },
    SchemaKind.FIX_RESULT: {
        "schema_version": 1,
        "summary": "findingへ同意し境界条件を修正",
        "pre_head_sha": SHA,
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
        "target_head_sha": SHA,
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
        "target_head_sha": SHA,
        "result": "CONFIRMED",
        "rationale": "非同期経路でも同じ競合が起こる",
    },
    SchemaKind.DECISION_REQUEST: {
        "schema_version": 1,
        "target_head_sha": SHA,
        "reason": "外部仕様の選択が残る",
        "constraints": "後方互換を維持する",
        "candidates": [
            {"content": "案A", "pros": "単純", "cons": "拡張性が低い", "impact": "小"}
        ],
        "recommendation": "案A",
    },
    SchemaKind.DECISION_VERDICT: {
        "schema_version": 1,
        "target_head_sha": SHA,
        "verdict": "ASK_USER",
        "rationale": "ユーザー体験の選択が残る",
    },
    SchemaKind.DECISION_BRIEF: {
        "schema_version": 1,
        "decision_id": "D-run1-001",
        "target_head_sha": SHA,
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
        "target_head_sha": SHA,
        "considered": "互換性方針",
        "initial_position": "案A",
        "verdict": "PROCEED_WITH_RECORD",
        "verdict_rationale": "既存方針から決定できる",
        "adopted_implementation": "案Aを実装",
    },
    SchemaKind.USER_DECISION: {
        "schema_version": 1,
        "decision_id": "D-run1-001",
        "target_head_sha": SHA,
        "answer": "[1]で進めてください",
        "input_route": "github_comment",
    },
    SchemaKind.FOLLOWUP_CANDIDATES: {
        "schema_version": 1,
        "repository": "owner/repo",
        "number": 12,
        "target_head_sha": SHA,
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
        "target_head_sha": SHA,
        "evaluations": [
            {"candidate_id": "FU-1", "verdict": "CREATE_ISSUE", "reason": "追跡価値がある"}
        ],
    },
    SchemaKind.FOLLOWUP_PERMISSION: {
        "schema_version": 1,
        "repository": "owner/repo",
        "number": 12,
        "target_head_sha": SHA,
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
        "approved_head_sha": SHA,
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
        "target_head_sha": SHA,
        "input_route": "powershell",
    },
    SchemaKind.MERGE_APPROVAL: {
        "schema_version": 1,
        "repository": "owner/repo",
        "pr_number": 12,
        "approved_head_sha": SHA,
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
        "approved_head_sha": SHA,
        "merged_commit_sha": "89ab0123",
        "merge_method": "merge",
        "approval_record_url": "https://example.invalid/pr/12#c-200",
        "verified_on_github": True,
    },
    SchemaKind.GATE_QUESTION: {
        "schema_version": 1,
        "target_head_sha": SHA,
        "body": "riskの詳細を教えてください",
        "input_route": "github_comment",
    },
    SchemaKind.GATE_ANSWER: {
        "schema_version": 1,
        "target_head_sha": SHA,
        "body": "riskは負荷試験の未実施のみです",
    },
    SchemaKind.GATE_CHANGES: {
        "schema_version": 1,
        "target_head_sha": SHA,
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
        "expected_head_sha": SHA,
        "payload_hash": "ph-1",
        "nonce": "nonce-1",
        "verified_records": [{"comment_id": "c-1", "head_sha": SHA}],
        "payload": {"findings": ["F-1"]},
    },
    SchemaKind.SUBMIT: {
        "schema_version": 1,
        "run_id": "run-1",
        "action_id": "act-1",
        "action_kind": "APPLY_FINDINGS",
        "expected_head_sha": SHA,
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
        "target_head_sha": SHA,
        "requested_scope": "push once",
        "resume_command": "同じSkillをresume",
    },
    SchemaKind.CI_TIMEOUT: {
        "schema_version": 1,
        "target_head_sha": SHA,
        "checks": [{"check_name": "CI", "url": "https://example.invalid/run/1"}],
        "waited_seconds": 1200,
    },
    SchemaKind.CI_CODE_FAILURE: {
        "schema_version": 1,
        "target_head_sha": SHA,
        "checks": [{"check_name": "CI", "result": "fail"}],
        "summary": "test失敗",
    },
    SchemaKind.EXTERNAL_DEPENDENCY: {
        "schema_version": 1,
        "target_head_sha": SHA,
        "description": "外部APIのrate limit解除待ち",
    },
    SchemaKind.BLOCK_INTERVENTION: {
        "schema_version": 1,
        "target_block_binding": "blk-1",
        "target_head_sha": SHA,
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
        "target_head_sha": SHA,
        "input_route": "powershell",
        "reason": "方針変更のため中断",
    },
    SchemaKind.CHECKPOINT: {
        "schema_version": 1,
        "run_id": "run-1",
        "repository": "owner/repo",
        "number": 12,
        "state": {"state": "RUNNING_REVIEW", "round": 1},
        "heads": {"base_sha": "aaa", "observed_sha": SHA},
    },
}


REPRESENTATIVE[SchemaKind.RUN_LOCK] = {
    "schema_version": 1,
    "run_id": "run-1",
    "repository": "owner/repo",
    "number": 12,
    "pid": 4242,
    "host": "build-host",
    "acquired_at": "2026-08-23T10:00:00Z",
    "head_sha": SHA,
}


def record_payload(kind: RecordKind, *, head_sha: str) -> dict[str, object]:
    """canonical record kindのrepresentative payload（対象head fieldを差し替えた複製）。"""
    payload = dict(REPRESENTATIVE[schema_kind_of(kind)])
    head_source = PROJECTION_SPECS[kind].head_source
    if head_source is not None:
        payload[head_source] = head_sha
    return payload
