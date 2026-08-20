# SPDX-License-Identifier: Apache-2.0
"""C-02 protocol validatorのengine（ADR-0003で決定した専用validator）。

Phase 0の評価実装（`tests/p001_evaluation/`）を製品codeへ移植したもの。特性:

- 入力境界は `size -> utf8 -> json -> version -> schema` のpipelineで判定し、
  先に失敗したstageで確定する
- 公開errorは`(code, path)`のみで構成し、入力値・入力key名を含めない。
  未知field名とmap keyはattacker-controlledな文字列であり、`<unknown#N>` /
  `<key#N>`の序数tokenへ正規化し、全pathへ長さ上限と制御文字除去を適用する
- integer意味論: JSONのinteger tokenのみを整数として許可する（boolと`1.0`は
  型不一致）。したがって`schema_version: 2.0`はschema stageで拒否され、
  integerの未知versionはversion stageで拒否される
- 診断優先順位: 同一pathの複数違反は `null_not_allowed -> type_mismatch ->
  その他の値制約` の順で1件へ正規化する
- repairは損失のない変換（UTF-8 BOM除去、specへ宣言された既定値の補完）に
  限定し、repair後は必ず同じvalidatorを通す（AC-C02-02）
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

DEFAULT_MAX_INPUT_BYTES = 65_536
MAX_PATH_LENGTH = 120

STAGES = ("size", "utf8", "json", "version", "schema", "migration")

# repair用の「既定値なし」sentinel（Noneは正当な既定値になり得るため区別する）
_NO_DEFAULT = object()


@dataclass(frozen=True)
class PublicError:
    """公開してよいerror。入力値・入力key名を含まない。"""

    code: str
    path: str


@dataclass(frozen=True)
class Field:
    """宣言的なfield spec（ADR-0003の機能集合）。"""

    types: tuple[type, ...]
    required: bool = True
    allow_none: bool = False
    non_empty: bool = False
    max_len: int | None = None
    enum: tuple[str, ...] | None = None
    fields: Mapping[str, Field] | None = None  # nested object
    items: Field | None = None  # list
    values: Field | None = None  # map（任意keyのobject）
    max_items: int | None = None  # listの要素数上限
    default: object = _NO_DEFAULT  # 損失のない補完に使う既定値（repair専用）


CrossFieldRule = Callable[[dict[str, object]], list[PublicError]]


@dataclass(frozen=True)
class VersionSpec:
    """1つのschema versionのstructural spec。"""

    fields: Mapping[str, Field]
    rules: tuple[CrossFieldRule, ...] = ()


@dataclass(frozen=True)
class ValidationResult:
    """検証結果。stageは失敗したstage（成功時はNone）。"""

    ok: bool
    stage: str | None
    errors: tuple[PublicError, ...]
    payload: dict[str, object] | None
    version: int | None = None


# 同一pathの複数違反を1件へ正規化する際の優先順位（小さいほど優先）
_CODE_PRIORITY = {"null_not_allowed": 0, "type_mismatch": 1}


def canonicalize(errors: list[PublicError]) -> list[PublicError]:
    """同一pathのerrorを診断優先順位に従って1件へ正規化する（出現順は維持）。"""
    best: dict[str, PublicError] = {}
    order: list[str] = []
    for e in errors:
        if e.path not in best:
            best[e.path] = e
            order.append(e.path)
        elif _CODE_PRIORITY.get(e.code, 99) < _CODE_PRIORITY.get(best[e.path].code, 99):
            best[e.path] = e
    return [best[path] for path in order]


def _reject_constant(value: str) -> object:
    raise ValueError(f"非標準のJSON token: {value}")


def parse_json(text: str) -> object:
    """protocolのJSON parse意味論（NaN / Infinity拒否）。失敗はValueError / RecursionError。"""
    return json.loads(text, parse_constant=_reject_constant)


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


def _check(value: object, spec: Field, path: str, errors: list[PublicError]) -> None:
    if value is None:
        if not spec.allow_none:
            errors.append(PublicError("null_not_allowed", path))
        return
    if int in spec.types and bool not in spec.types and not isinstance(value, str | list | dict):
        # integerはJSON integer tokenのみ。boolとfloatは型不一致とする。
        if not is_integer_token(value):
            errors.append(PublicError("type_mismatch", path))
            return
    elif not isinstance(value, spec.types):
        errors.append(PublicError("type_mismatch", path))
        return
    if isinstance(value, str):
        if spec.non_empty and value == "":
            errors.append(PublicError("empty_string", path))
        if spec.max_len is not None and len(value) > spec.max_len:
            errors.append(PublicError("max_length", path))
        if spec.enum is not None and value not in spec.enum:
            errors.append(PublicError("enum_invalid", path))
    if isinstance(value, dict):
        if spec.fields is not None:
            _check_object(value, spec.fields, path, errors)
        elif spec.values is not None:
            all_keys = list(value.keys())
            for key, item in value.items():
                _check(item, spec.values, f"{path}.{map_key_token(all_keys, key)}", errors)
    if isinstance(value, list):
        if spec.max_items is not None and len(value) > spec.max_items:
            errors.append(PublicError("max_items", path))
        if spec.items is not None:
            for i, item in enumerate(value):
                _check(item, spec.items, f"{path}[{i}]", errors)


def _check_object(
    obj: dict[str, object], fields: Mapping[str, Field], path: str, errors: list[PublicError]
) -> None:
    prefix = "" if path == "$" else path + "."
    for name, spec in fields.items():
        if name not in obj:
            if spec.required:
                errors.append(PublicError("required_missing", prefix + name))
            continue
        _check(obj[name], spec, prefix + name, errors)
    unknown = [name for name in obj if name not in fields]
    for name in unknown:
        errors.append(PublicError("unknown_field", prefix + unknown_field_token(unknown, name)))


def structural_errors(spec: VersionSpec, data: dict[str, object]) -> tuple[PublicError, ...]:
    """structural + cross-field検証。診断優先順位の正規化とpath sanitizeを適用済みで返す。"""
    errors: list[PublicError] = []
    _check_object(data, spec.fields, "$", errors)
    for rule in spec.rules:
        errors.extend(rule(data))
    return tuple(PublicError(e.code, sanitize_path(e.path)) for e in canonicalize(errors))


def strip_bom(raw: bytes) -> bytes:
    """損失のないrepair(1): UTF-8 BOMの除去。"""
    return raw.removeprefix(b"\xef\xbb\xbf")


def apply_defaults(spec: VersionSpec, data: dict[str, object]) -> dict[str, object]:
    """損失のないrepair(2): specへ宣言された既定値の補完。

    欠落したoptional fieldへ宣言済みの既定値を入れる。存在する値は変更しない
    （意味的fieldの捏造をしない）。nested objectへは再帰する。
    """
    repaired = dict(data)
    for name, field_spec in spec.fields.items():
        if name not in repaired:
            if field_spec.default is not _NO_DEFAULT and not field_spec.required:
                repaired[name] = field_spec.default
            continue
        value = repaired[name]
        if field_spec.fields is not None and isinstance(value, dict):
            repaired[name] = apply_defaults(
                VersionSpec(fields=field_spec.fields), value
            )
    return repaired
