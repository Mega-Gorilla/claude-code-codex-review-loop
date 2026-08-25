# SPDX-License-Identifier: Apache-2.0
"""versioningとmigrationの受入test（AC-C02-03、ADR-0004）。

migration機構はregistryのdataに対して汎用であるため、test定義のv1→v2→v3 chainで
機構を検証し、production定義（checkpoint envelope）では現行versionの受理と
未知versionの拒否を検証する。
"""

from __future__ import annotations

import json

from claude_code_codex_review_loop.schema import (
    REGISTRY,
    SchemaKind,
    load_with_migration,
)
from claude_code_codex_review_loop.schema.registry import (
    SchemaDefinition,
    schema_version_field,
    text,
)
from claude_code_codex_review_loop.schema.validate import Field, VersionSpec


def _raw(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _v1_to_v2(payload: dict[str, object]) -> dict[str, object]:
    """損失のない改名: title -> name。"""
    migrated = dict(payload)
    migrated["name"] = migrated.pop("title")
    return migrated


def _v2_to_v3(payload: dict[str, object]) -> dict[str, object]:
    """損失のない構造変更: nameをprofile objectへ移す。"""
    migrated = dict(payload)
    migrated["profile"] = {"name": migrated.pop("name")}
    return migrated


_CHAINED = SchemaDefinition(
    kind=SchemaKind.USER_CANCEL,  # kindは識別にのみ使う（test専用定義）
    versions={
        1: VersionSpec(fields={"schema_version": schema_version_field(), "title": text()}),
        2: VersionSpec(fields={"schema_version": schema_version_field(), "name": text()}),
        3: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "profile": Field(types=(dict,), fields={"name": text()}),
            }
        ),
    },
    migrations={1: _v1_to_v2, 2: _v2_to_v3},
)


class TestMigrationChain:
    def test_v1_payload_is_readable_and_migrated_to_current(self) -> None:
        result = load_with_migration(_CHAINED, _raw({"schema_version": 1, "title": "t"}))
        assert result.ok, result.errors
        assert result.payload == {"schema_version": 3, "profile": {"name": "t"}}
        assert result.version == 3

    def test_intermediate_version_is_migrated(self) -> None:
        result = load_with_migration(_CHAINED, _raw({"schema_version": 2, "name": "t"}))
        assert result.ok and result.payload == {"schema_version": 3, "profile": {"name": "t"}}

    def test_current_version_passes_through(self) -> None:
        payload = {"schema_version": 3, "profile": {"name": "t"}}
        result = load_with_migration(_CHAINED, _raw(payload))
        assert result.ok and result.payload == payload

    def test_old_payload_must_be_valid_for_its_declared_version(self) -> None:
        """migration前に、宣言versionのspecで検証する（不正な旧payloadを黙って変換しない）。"""
        result = load_with_migration(_CHAINED, _raw({"schema_version": 1, "name": "wrong-field"}))
        assert (result.ok, result.stage) == (False, "schema")

    def test_unknown_future_version_is_a_version_error(self) -> None:
        result = load_with_migration(_CHAINED, _raw({"schema_version": 99, "title": "t"}))
        assert (result.ok, result.stage) == (False, "version")

    def test_missing_migration_step_is_a_structured_error(self) -> None:
        """chainが現行へ到達しないversionはsilentに無視せずerrorになる（AC-C02-03）。"""
        broken = SchemaDefinition(
            kind=SchemaKind.USER_CANCEL,
            versions=_CHAINED.versions,
            migrations={1: _v1_to_v2},  # 2 -> 3 が欠落
        )
        result = load_with_migration(broken, _raw({"schema_version": 1, "title": "t"}))
        assert (result.ok, result.stage) == (False, "migration")
        assert {e.code for e in result.errors} == {"migration_unavailable"}

    def test_no_registered_migrations_is_a_structured_error(self) -> None:
        no_migrations = SchemaDefinition(
            kind=SchemaKind.USER_CANCEL, versions=_CHAINED.versions
        )
        result = load_with_migration(no_migrations, _raw({"schema_version": 2, "name": "t"}))
        assert (result.ok, result.stage) == (False, "migration")

    def test_migration_output_is_revalidated_by_the_same_validator(self) -> None:
        """migrationが不正なpayloadを作った場合、現行validatorの再検証で拒否される。"""

        def bad_migration(payload: dict[str, object]) -> dict[str, object]:
            migrated = dict(payload)
            migrated.pop("title")
            migrated["fabricated"] = True
            return migrated

        bad = SchemaDefinition(
            kind=SchemaKind.USER_CANCEL,
            versions={
                1: _CHAINED.versions[1],
                2: _CHAINED.versions[2],
            },
            migrations={1: bad_migration},
        )
        result = load_with_migration(bad, _raw({"schema_version": 1, "title": "t"}))
        assert (result.ok, result.stage) == (False, "migration")
        assert any(e.code == "required_missing" for e in result.errors)

    def test_definition_without_declared_version_cannot_migrate(self) -> None:
        """schema_versionをspecに持たない定義では、versionを特定できずmigrationしない。"""
        versionless = SchemaDefinition(
            kind=SchemaKind.USER_CANCEL,
            versions={1: VersionSpec(fields={"note": text(required=False)})},
        )
        result = load_with_migration(versionless, _raw({"note": "x"}))
        assert (result.ok, result.stage) == (False, "migration")


# migration chainを意図的に持たない定義（理由つきで登録する。ADR-0004 rule 6 / 8）
INCOMPLETE_MIGRATION_CHAINS: dict[SchemaKind, str] = {
    SchemaKind.HOST_ACTION: "v1 -> v2はresult_pathを捏造せずに変換できない（ADR-0014）",
    SchemaKind.SUBMIT: "HOST_ACTIONとaction kindのenumを共有するため同時にbumpした（ADR-0014）",
}


def _checkpoint(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": "run-1",
        "repository": "owner/repo",
        "number": 12,
    }
    payload.update(overrides)
    return payload


_V1_HOST_ACTION: dict[str, object] = {
    "action_id": "act-1",
    "action_kind": "APPLY_FINDINGS",
    "nonce": "nonce-1",
    "expected_head_sha": "a" * 40,
    "result_path": "actions/act-1/result.json",
    "envelope_path": "actions/act-1/action.json",
    "envelope_hash": "e" * 64,
}


class TestProductionDefinitions:
    def test_checkpoint_v1_migrates_to_current(self) -> None:
        """host_actionを持たないcheckpointは、version以外が変わらない（ADR-0015）。"""
        result = load_with_migration(REGISTRY[SchemaKind.CHECKPOINT], _raw(_checkpoint()))
        assert result.ok and result.payload == _checkpoint(schema_version=2)

    def test_checkpoint_v1_pending_action_moves_under_pending(self) -> None:
        payload = _checkpoint(host_action=dict(_V1_HOST_ACTION))
        result = load_with_migration(REGISTRY[SchemaKind.CHECKPOINT], _raw(payload))
        assert result.ok and result.payload is not None
        assert result.payload["host_action"] == {"pending": _V1_HOST_ACTION}

    def test_checkpoint_v1_submit_becomes_a_keyed_receipt(self) -> None:
        """単一submitは、同じsectionのaction ID / nonceを鍵にした1件のreceiptへ写る。"""
        submit = {
            "outcome": "COMPLETED",
            "submit_hash": "s" * 64,
            "result_hash": "r" * 64,
            "result_kind": "FIX_RESULT",
        }
        payload = _checkpoint(host_action={**_V1_HOST_ACTION, "submit": submit})
        result = load_with_migration(REGISTRY[SchemaKind.CHECKPOINT], _raw(payload))
        assert result.ok and result.payload is not None
        section = result.payload["host_action"]
        assert section == {
            "pending": _V1_HOST_ACTION,
            "receipts": [{**submit, "action_id": "act-1", "nonce": "nonce-1"}],
        }

    def test_checkpoint_unknown_version_is_rejected(self) -> None:
        result = load_with_migration(
            REGISTRY[SchemaKind.CHECKPOINT], _raw(_checkpoint(schema_version=3))
        )
        assert (result.ok, result.stage) == (False, "version")

    def test_production_versions_are_contiguous_from_one(self) -> None:
        """versionは1始まり・欠番なし（ADR-0004 rule 1）。"""
        for kind, definition in REGISTRY.items():
            versions = sorted(definition.versions)
            assert versions == list(range(1, versions[-1] + 1)), kind

    def test_migration_chains_are_complete_or_declared(self) -> None:
        """chainが現行versionへ到達しない定義は、理由つきで明示登録する（silentな穴を作らない）。

        migrationは**損失のない**変換に限る（ADR-0004 rule 6）ため、情報が増える方向の
        bumpではchainを張れない。その場合は`migration_unavailable`の構造化errorになる
        （rule 8）ので、意図した穴だけを許す。
        """
        for kind, definition in REGISTRY.items():
            migrations = definition.migrations or {}
            missing = [
                version
                for version in range(1, definition.current_version)
                if version not in migrations
            ]
            assert bool(missing) == (kind in INCOMPLETE_MIGRATION_CHAINS), kind

    def test_integer_versions_start_at_one_and_are_contiguous(self) -> None:
        for definition in (_CHAINED,):
            versions = sorted(definition.versions)
            assert versions[0] == 1
            assert versions == list(range(1, versions[-1] + 1))
