# SPDX-License-Identifier: Apache-2.0
"""role別providerの解決済みsnapshot契約（Issue #52、ADR-0025）。"""

from .registry import (
    SchemaDefinition,
    SchemaKind,
    enum_field,
    integer,
    obj,
    opaque,
    schema_version_field,
    text,
)
from .validate import Field, PublicError, VersionSpec, is_integer_token

PROVIDERS = ("claude", "codex")
CODER_MODES = ("active", "headless")
REVIEWER_MODES = ("fresh",)
CODER_PROFILE = "coder_workspace"
REVIEWER_PROFILE = "reviewer_isolated"


def _selection_rules(data: dict[str, object]) -> list[PublicError]:
    """推測や補完なしで意味的な下限だけ検証する。入力値は診断へ出さない。"""
    errors: list[PublicError] = []
    number = data.get("number")
    if is_integer_token(number) and isinstance(number, int) and number < 1:
        errors.append(PublicError("out_of_range", "number"))
    for role in ("coder", "reviewer"):
        config = data.get(role)
        if not isinstance(config, dict):
            continue
        version = config.get("adapter_contract_version")
        if is_integer_token(version) and isinstance(version, int) and version < 1:
            errors.append(PublicError("out_of_range", f"{role}.adapter_contract_version"))
        model = config.get("model")
        if isinstance(model, str) and (not model.strip() or not model.isprintable()):
            errors.append(PublicError("invalid_model", f"{role}.model"))
    return errors


def _role_fields(modes: tuple[str, ...], profile: str) -> dict[str, Field]:
    return {
        "provider": enum_field(PROVIDERS),
        "model": text(max_len=200),
        "mode": enum_field(modes),
        # roleの安全要件であり、native CLIのpermission flagではない。
        "safety_profile": enum_field((profile,)),
        "adapter_contract_version": integer(),
    }


AGENT_SELECTION = SchemaDefinition(
    kind=SchemaKind.AGENT_SELECTION,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "run_id": opaque(),
                "repository": text(),
                "number": integer(),
                "coder": obj(_role_fields(CODER_MODES, CODER_PROFILE)),
                "reviewer": obj(_role_fields(REVIEWER_MODES, REVIEWER_PROFILE)),
            },
            rules=(_selection_rules,),
        ),
    },
)
