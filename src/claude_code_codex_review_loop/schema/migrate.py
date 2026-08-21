# SPDX-License-Identifier: Apache-2.0
"""旧version payloadの段階的migrationと再検証（AC-C02-03、ADR-0004）。

migrationは損失のないpure関数 `payload(v_n) -> payload(v_n+1)` をkindごとに登録し、
現行versionへ到達するまで段階的にchainする。chainが現行へ到達しないversionは
構造化error（silent無視しない）。migration後は必ず現行versionの同じvalidatorを通す。
"""

from __future__ import annotations

from .registry import SchemaDefinition, validate, validate_object
from .validate import PublicError, ValidationResult


def _migration_error(code: str) -> ValidationResult:
    return ValidationResult(False, "migration", (PublicError(code, "schema_version"),), None)


def load_with_migration(definition: SchemaDefinition, raw: bytes) -> ValidationResult:
    """既知の旧versionを含む入力を読み、現行versionのpayloadとして返す。

    - まず入力を宣言versionのspecで検証する（旧versionとして不正なpayloadは拒否）
    - 現行versionへ到達するまで登録済みmigrationを段階適用する。登録が欠けた
      versionは`migration_unavailable`の構造化errorになる
    - migration後のpayloadを**現行versionの同じvalidator**で再検証してから返す
    """
    result = validate(definition, raw)
    if not result.ok:
        return result
    payload = result.payload
    version = result.version
    if payload is None or version is None:
        # schema_versionはspec上必須のintであり、検証成功時は必ず存在する
        return _migration_error("migration_unavailable")
    migrations = definition.migrations or {}
    while version < definition.current_version:
        step = migrations.get(version)
        if step is None:
            return _migration_error("migration_unavailable")
        migrated = step(payload)
        version += 1
        payload = dict(migrated)
        payload["schema_version"] = version
    # migration後は同じvalidatorで再検証する（migrationが不正なpayloadを作れば失敗する）
    final = validate_object(definition, payload)
    if not final.ok:
        return ValidationResult(False, "migration", final.errors, None)
    return final
