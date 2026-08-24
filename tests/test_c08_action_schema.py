# SPDX-License-Identifier: Apache-2.0
"""`HOST_ACTION` / `SUBMIT` envelope（v2）の受入test（ADR-0014 / ADR-0004）。

- binding 8項目 + `result_path`（必須）+ AC-C08-07のverified records
- actionごとのpayload schema（kind取り違え・必須欠落・未知fieldを拒否）
- v1は既知versionだがmigrationが存在しない（`migration_unavailable`）
"""

from __future__ import annotations

import json

import pytest

from claude_code_codex_review_loop.domain.commands import HostAction
from claude_code_codex_review_loop.schema import REGISTRY, SchemaKind, validate
from claude_code_codex_review_loop.schema.action import HOST_ACTION, SUBMIT
from claude_code_codex_review_loop.schema.migrate import load_with_migration

SHA = "0123abcd"


def _raw(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _action(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 2,
        "run_id": "run-1",
        "action_id": "act-1",
        "action_kind": HostAction.APPLY_FINDINGS.value,
        "repository": "owner/repo",
        "number": 12,
        "expected_head_sha": SHA,
        "payload_hash": "ph-1",
        "nonce": "nonce-1",
        "result_path": "actions/act-1.result.json",
        "verified_records": [{"comment_id": "c-1", "head_sha": SHA}],
        "payload": {"round": 1, "finding_ids": ["F-1"]},
    }
    payload.update(overrides)
    return payload


def _submit(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 2,
        "run_id": "run-1",
        "action_id": "act-1",
        "action_kind": HostAction.APPLY_FINDINGS.value,
        "expected_head_sha": SHA,
        "nonce": "nonce-1",
        "result_hash": "rh-1",
        "outcome": "COMPLETED",
    }
    payload.update(overrides)
    return payload


def _codes(payload: dict[str, object], definition: object = None) -> set[str]:
    result = validate(HOST_ACTION if definition is None else definition, _raw(payload))  # type: ignore[arg-type]
    return {error.code for error in result.errors}


class TestHostActionEnvelope:
    def test_current_version_is_two(self) -> None:
        assert HOST_ACTION.current_version == 2 and SUBMIT.current_version == 2

    def test_accepts_the_binding_and_result_path(self) -> None:
        assert validate(HOST_ACTION, _raw(_action())).ok

    def test_result_path_is_required(self) -> None:
        """result pathはControllerが払い出す（呼び出し側の任意pathを受理しない）。"""
        payload = _action()
        del payload["result_path"]
        assert _codes(payload) == {"required_missing"}

    def test_verified_records_are_required(self) -> None:
        """AC-C08-07: 検証済みrecordのcomment IDと対象headを必ず渡す。"""
        payload = _action()
        del payload["verified_records"]
        assert _codes(payload) == {"required_missing"}

    @pytest.mark.parametrize(
        "kind",
        ["IMPLEMENT_ISSUE", "ASK_CLARIFICATION", "RUN_LOCAL_TESTS", "STRUCTURE_USER_INTENT"],
    )
    def test_v1_only_kinds_are_rejected(self, kind: str) -> None:
        """v1の暫定enumにあったC-01が発行しないactionは、v2では値域外。"""
        assert "enum_invalid" in _codes(_action(action_kind=kind, payload={}))


class TestPayloadPerKind:
    def test_accepts_the_declared_payload(self) -> None:
        assert validate(HOST_ACTION, _raw(_action(action_kind="RECORD_DECISION",
                                                  payload={"decision_id": "d-1"}))).ok

    def test_missing_payload_field_is_reported_under_payload(self) -> None:
        result = validate(HOST_ACTION, _raw(_action(action_kind="RECORD_DECISION", payload={})))
        assert not result.ok
        assert [(error.code, error.path) for error in result.errors] == [
            ("required_missing", "payload.decision_id")
        ]

    def test_payload_of_another_kind_is_rejected(self) -> None:
        """kindを取り違えたpayloadは通らない（unknown + missingの両方が出る）。"""
        result = validate(HOST_ACTION, _raw(_action(action_kind="RECORD_DECISION",
                                                    payload={"round": 1, "finding_ids": []})))
        assert {error.code for error in result.errors} == {"required_missing", "unknown_field"}

    def test_action_without_payload_fields_rejects_extras(self) -> None:
        payload = _action(action_kind="DRAFT_DECISION_REQUEST", payload={"decision_id": "d-1"})
        assert _codes(payload) == {"unknown_field"}

    def test_action_without_payload_fields_accepts_empty(self) -> None:
        assert validate(HOST_ACTION, _raw(_action(action_kind="DRAFT_DECISION_REQUEST", payload={}))).ok

    def test_payload_type_mismatch_is_reported_once(self) -> None:
        payload = _action(payload={"round": "1", "finding_ids": ["F-1"]})
        assert _codes(payload) == {"type_mismatch"}

    def test_non_object_payload_is_a_field_error(self) -> None:
        """payload自体の型違反はfield検証が報告し、ruleは二重報告しない。"""
        result = validate(HOST_ACTION, _raw(_action(payload="x")))
        assert [(error.code, error.path) for error in result.errors] == [("type_mismatch", "payload")]


class TestSubmitEnvelope:
    def test_accepts_the_binding_echo(self) -> None:
        assert validate(SUBMIT, _raw(_submit())).ok

    def test_failed_requires_an_error_category(self) -> None:
        assert not validate(SUBMIT, _raw(_submit(outcome="FAILED"))).ok
        assert validate(SUBMIT, _raw(_submit(outcome="FAILED", error_category="TRANSIENT"))).ok

    def test_v1_only_kind_is_rejected(self) -> None:
        assert "enum_invalid" in {
            error.code for error in validate(SUBMIT, _raw(_submit(action_kind="IMPLEMENT_ISSUE"))).errors
        }


class TestVersioning:
    """v1は既知versionだが、v2へ持ち上げる損失のない変換が存在しない（ADR-0004 rule 6 / 8）。"""

    def _v1(self) -> dict[str, object]:
        payload = _action(schema_version=1, action_kind="IMPLEMENT_ISSUE", payload={"issue": 1})
        del payload["result_path"]
        return payload

    def test_v1_is_still_a_known_version(self) -> None:
        assert validate(HOST_ACTION, _raw(self._v1())).ok

    def test_v1_cannot_be_migrated(self) -> None:
        result = load_with_migration(HOST_ACTION, _raw(self._v1()))
        assert (result.ok, result.stage) == (False, "migration")
        assert {error.code for error in result.errors} == {"migration_unavailable"}

    def test_current_version_passes_migration_unchanged(self) -> None:
        result = load_with_migration(HOST_ACTION, _raw(_action()))
        assert result.ok and result.version == 2

    def test_unknown_version_is_a_version_error(self) -> None:
        assert validate(HOST_ACTION, _raw(_action(schema_version=99))).stage == "version"


def test_registry_exposes_both_definitions() -> None:
    assert REGISTRY[SchemaKind.HOST_ACTION] is HOST_ACTION
    assert REGISTRY[SchemaKind.SUBMIT] is SUBMIT
