# SPDX-License-Identifier: Apache-2.0
"""両候補が共有する入力境界（size / UTF-8 / JSON / version gate）と公開error形式。

schema library選択と独立した処理はここへ置き、候補差をstructural / cross-field
validationだけに限定する。公開errorは`code`と`path`のみで構成し、入力値を含めない。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

MAX_INPUT_BYTES = 65_536
KNOWN_SCHEMA_VERSIONS = frozenset({1})

# rejectのstage分類。1 caseにつき期待stageは一意とする。
STAGES = ("size", "utf8", "json", "version", "schema")


@dataclass(frozen=True)
class PublicError:
    """公開してよいerror。入力値を含むfree-textを持たない。"""

    code: str
    path: str


StructuralValidator = Callable[[dict[str, object]], list[PublicError]]


def run(structural: StructuralValidator, raw: bytes) -> tuple[str, list[PublicError]]:
    """共通pipelineで検証する。結果は 'accept' または 'reject:<stage>'。"""

    if len(raw) > MAX_INPUT_BYTES:
        return "reject:size", [PublicError("input_too_large", "$")]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "reject:utf8", [PublicError("invalid_utf8", "$")]
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        return "reject:json", [PublicError("invalid_json", "$")]
    if not isinstance(data, dict):
        return "reject:schema", [PublicError("root_not_object", "$")]
    version = data.get("schema_version")
    if isinstance(version, int) and not isinstance(version, bool) and version not in KNOWN_SCHEMA_VERSIONS:
        return "reject:version", [PublicError("unknown_version", "schema_version")]
    errors = structural(data)
    if errors:
        return "reject:schema", errors
    return "accept", []
