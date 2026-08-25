# SPDX-License-Identifier: Apache-2.0
"""C-02の全schema（31 kind）の受入test（AC-C02-01）。

kindごとに、representative payloadの受理と、未知version / 必須field欠落 / 型不一致 /
size超過 / cross-field違反が**区別できるerror**になることを検証する。
"""

from __future__ import annotations

import json

import pytest
from c02_support.helpers import REPRESENTATIVE
from c02_support.helpers import finding as _finding

from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.schema import REGISTRY, SchemaKind, validate

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
    SchemaKind.FOLLOWUP_PERMISSION: "target_head_sha",
    SchemaKind.FINAL_REPORT: "approved_head_sha",
    SchemaKind.MERGE_INTENT: "intent",
    SchemaKind.MERGE_APPROVAL: "approved_head_sha",
    SchemaKind.MERGE_OUTCOME: "merged_commit_sha",
    SchemaKind.GATE_QUESTION: "body",
    SchemaKind.GATE_ANSWER: "body",
    SchemaKind.GATE_CHANGES: "body",
    SchemaKind.HOST_ACTION: "nonce",
    SchemaKind.SUBMIT: "result_hash",
    SchemaKind.HOST_FAILURE: "summary",
    SchemaKind.PERMISSION_BLOCK: "permission_id",
    SchemaKind.CI_TIMEOUT: "waited_seconds",
    SchemaKind.CI_CODE_FAILURE: "summary",
    SchemaKind.EXTERNAL_DEPENDENCY: "description",
    SchemaKind.BLOCK_INTERVENTION: "target_block_binding",
    SchemaKind.INTEGRITY_INCIDENT: "violation_bindings",
    SchemaKind.USER_CANCEL: "input_route",
    SchemaKind.RUN_LOCK: "run_id",
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

    def test_submit_outcome_fields_are_exclusive(self) -> None:
        """v2: COMPLETEDは結果種別、FAILEDは失敗分類を持つ（ADR-0014）。"""
        failed = dict(REPRESENTATIVE[SchemaKind.SUBMIT])
        failed["outcome"] = "FAILED"
        self._reject(SchemaKind.SUBMIT, failed, "result_kind")
        del failed["result_kind"]
        self._reject(SchemaKind.SUBMIT, failed, "error_category")
        failed["error_category"] = "TRANSIENT"
        assert validate(REGISTRY[SchemaKind.SUBMIT], _raw(failed)).ok
        completed = dict(REPRESENTATIVE[SchemaKind.SUBMIT])
        completed["error_category"] = "TRANSIENT"
        self._reject(SchemaKind.SUBMIT, completed, "error_category")

    def test_approved_followup_permission_requires_authority(self) -> None:
        """APPROVEDは入力経路と承認comment IDなしでは受理されない（TE L510のbind）。"""
        base = dict(REPRESENTATIVE[SchemaKind.FOLLOWUP_PERMISSION])
        missing_route = dict(base)
        del missing_route["input_route"]
        self._reject(SchemaKind.FOLLOWUP_PERMISSION, missing_route, "input_route")
        missing_comment = dict(base)
        del missing_comment["approval_comment_id"]
        self._reject(SchemaKind.FOLLOWUP_PERMISSION, missing_comment, "approval_comment_id")
        # 未回答は承認根拠を要求しない（未回答のIssue作成許可はmergeを止めない）
        unanswered = dict(base)
        del unanswered["input_route"]
        del unanswered["approval_comment_id"]
        del unanswered["created_issue_url"]
        unanswered["status"] = "UNANSWERED"
        assert validate(REGISTRY[SchemaKind.FOLLOWUP_PERMISSION], _raw(unanswered)).ok
        # binding（repository / number / head）の欠落は必須field違反として拒否される
        unbound = dict(base)
        del unbound["repository"]
        result = validate(REGISTRY[SchemaKind.FOLLOWUP_PERMISSION], _raw(unbound))
        assert any(e.code == "required_missing" and e.path == "repository" for e in result.errors)

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
