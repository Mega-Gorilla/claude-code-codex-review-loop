# SPDX-License-Identifier: Apache-2.0
"""C-02 agent protocol schemaとcheckpoint envelope（Phase 2）。

agent入出力とcheckpoint envelopeのschema・validation・versioning・migrationの
単一の所有者。validatorはADR-0003（runtime依存ゼロの専用validator）、versioningと
migration policyはADR-0004に従う。
"""

from .action import HOST_ACTION, HOST_ACTION_KINDS, SUBMIT
from .decision import (
    DECISION_BRIEF,
    DECISION_RECORD,
    DECISION_REQUEST,
    DECISION_VERDICT,
    USER_DECISION,
)
from .envelope import CHECKPOINT
from .followup import FOLLOWUP_CANDIDATES, FOLLOWUP_EVALUATION, FOLLOWUP_PERMISSION
from .merge import (
    GATE_ANSWER,
    GATE_CHANGES,
    GATE_QUESTION,
    MERGE_APPROVAL,
    MERGE_INTENT,
    MERGE_OUTCOME,
)
from .migrate import load_with_migration
from .records import (
    BLOCK_INTERVENTION,
    CI_CODE_FAILURE,
    CI_TIMEOUT,
    EXTERNAL_DEPENDENCY,
    INTEGRITY_INCIDENT,
    PERMISSION_BLOCK,
    USER_CANCEL,
)
from .registry import (
    SchemaDefinition,
    SchemaKind,
    repair_and_validate,
    validate,
    validate_object,
)
from .report import FINAL_REPORT
from .review import CLARIFICATION_ANSWER, CLARIFICATION_QUESTION, FIX_RESULT, REVIEW_RESULT
from .validate import Field, PublicError, ValidationResult, VersionSpec

_DEFINITIONS: tuple[SchemaDefinition, ...] = (
    REVIEW_RESULT,
    FIX_RESULT,
    CLARIFICATION_QUESTION,
    CLARIFICATION_ANSWER,
    DECISION_REQUEST,
    DECISION_VERDICT,
    DECISION_BRIEF,
    DECISION_RECORD,
    USER_DECISION,
    FOLLOWUP_CANDIDATES,
    FOLLOWUP_EVALUATION,
    FOLLOWUP_PERMISSION,
    FINAL_REPORT,
    MERGE_INTENT,
    MERGE_APPROVAL,
    MERGE_OUTCOME,
    GATE_QUESTION,
    GATE_ANSWER,
    GATE_CHANGES,
    HOST_ACTION,
    SUBMIT,
    PERMISSION_BLOCK,
    CI_TIMEOUT,
    CI_CODE_FAILURE,
    EXTERNAL_DEPENDENCY,
    BLOCK_INTERVENTION,
    INTEGRITY_INCIDENT,
    USER_CANCEL,
    CHECKPOINT,
)

def build_registry(
    definitions: tuple[SchemaDefinition, ...],
) -> dict[SchemaKind, SchemaDefinition]:
    """kind -> 定義のregistryを構築する。kindの重複登録は構築時に拒否する。"""
    registry: dict[SchemaKind, SchemaDefinition] = {}
    for definition in definitions:
        if definition.kind in registry:
            raise ValueError(f"SchemaKindが重複して登録されている: {definition.kind.value}")
        registry[definition.kind] = definition
    return registry


REGISTRY: dict[SchemaKind, SchemaDefinition] = build_registry(_DEFINITIONS)

__all__ = [
    "build_registry",
    "HOST_ACTION_KINDS",
    "REGISTRY",
    "Field",
    "PublicError",
    "SchemaDefinition",
    "SchemaKind",
    "ValidationResult",
    "VersionSpec",
    "load_with_migration",
    "repair_and_validate",
    "validate",
    "validate_object",
]
