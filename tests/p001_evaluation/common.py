# SPDX-License-Identifier: Apache-2.0
"""両候補が共有する入力境界（size / UTF-8 / JSON / version gate）と公開error形式。

schema library選択と独立した処理はここへ置き、候補差をstructural / cross-field
validationだけに限定する。公開errorは`code`と`path`のみで構成し、入力値を含めない。

protocolのinteger意味論:
    integerはJSONのinteger tokenのみを許可する。Draft 2020-12は小数部0のnumber
    （`1.0`）もintegerとして扱うが、本protocolでは型不一致とする。agent出力は
    JSON直列化された整数であり、`1.0`の混入はprotocol errorとして扱う方が安全で、
    validator実装間の意味論差も排除できる。両候補はこの意味論を同一に実装する。

stage優先順位:
    size -> utf8 -> json -> version -> schema の順に判定し、先に失敗したstageで
    確定する。version gateはschema_versionが**integer token**の場合だけ評価する。
    したがって`schema_version: 2.0`はversionではなくschema（型不一致）で拒否され、
    `schema_version: 2`（未知のinteger）は他のfieldに違反があってもversionで拒否される。

dynamic keyのpath正規化:
    未知field名とmapのkeyはattacker-controlledな文字列であり、公開pathへraw値を
    含めない。未知fieldは`<unknown#N>`（同一階層の未知key名を昇順に並べた1始まりの
    序数）、map keyは`<key#N>`（そのmapの全key名を昇順に並べた序数）へ正規化する。
    さらに全pathへ長さ上限と制御文字の除去を適用する。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

MAX_INPUT_BYTES = 65_536
KNOWN_SCHEMA_VERSIONS = frozenset({1})
MAX_PATH_LENGTH = 120

STAGES = ("size", "utf8", "json", "version", "schema")


@dataclass(frozen=True)
class PublicError:
    """公開してよいerror。入力値・入力key名を含まない。"""

    code: str
    path: str


StructuralValidator = Callable[[dict[str, object]], list[PublicError]]


def is_integer_token(value: object) -> bool:
    """protocolのinteger意味論: JSON integer tokenのみ（boolとfloatは含めない）。"""

    return isinstance(value, int) and not isinstance(value, bool)


def unknown_field_token(unknown_names: list[str], name: str) -> str:
    """未知field名を序数tokenへ正規化する。"""

    return f"<unknown#{sorted(unknown_names).index(name) + 1}>"


def map_key_token(all_keys: list[str], key: str) -> str:
    """map keyを序数tokenへ正規化する。"""

    return f"<key#{sorted(all_keys).index(key) + 1}>"


def sanitize_path(path: str) -> str:
    """公開path全体への防御: 制御文字を除去し、長さ上限を適用する。"""

    cleaned = "".join(ch for ch in path if ch.isprintable())
    if len(cleaned) > MAX_PATH_LENGTH:
        cleaned = cleaned[: MAX_PATH_LENGTH - 1] + "…"
    return cleaned


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
    if is_integer_token(version) and version not in KNOWN_SCHEMA_VERSIONS:
        return "reject:version", [PublicError("unknown_version", "schema_version")]
    errors = [PublicError(e.code, sanitize_path(e.path)) for e in structural(data)]
    if errors:
        return "reject:schema", errors
    return "accept", []
