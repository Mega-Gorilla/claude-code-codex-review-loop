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


class TestProductionDefinitions:
    def test_checkpoint_v1_loads_without_migration(self) -> None:
        payload = {
            "schema_version": 1,
            "run_id": "run-1",
            "repository": "owner/repo",
            "number": 12,
        }
        result = load_with_migration(REGISTRY[SchemaKind.CHECKPOINT], _raw(payload))
        assert result.ok and result.payload == payload

    def test_checkpoint_unknown_version_is_rejected(self) -> None:
        payload = {
            "schema_version": 2,
            "run_id": "run-1",
            "repository": "owner/repo",
            "number": 12,
        }
        result = load_with_migration(REGISTRY[SchemaKind.CHECKPOINT], _raw(payload))
        assert (result.ok, result.stage) == (False, "version")

    def test_all_production_definitions_start_at_version_one(self) -> None:
        """現行の全定義はv1のみを持ち、migrationは未登録（最初のbumpでchainを登録する）。"""
        for kind, definition in REGISTRY.items():
            assert set(definition.versions) == {1}, kind
            assert not definition.migrations, kind

    def test_integer_versions_start_at_one_and_are_contiguous(self) -> None:
        for definition in (_CHAINED,):
            versions = sorted(definition.versions)
            assert versions[0] == 1
            assert versions == list(range(1, versions[-1] + 1))
