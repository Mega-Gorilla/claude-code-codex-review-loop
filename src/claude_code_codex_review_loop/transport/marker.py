# SPDX-License-Identifier: Apache-2.0
"""予約marker（CC_REVIEW_META）の付加・抽出・agent本文のescape。

markerはControllerだけが付加する機械metadataで、HTML commentとして本文末尾へ
1行だけ置く（HTML commentも公開情報として扱い、冪等性とresumeに必要な最小項目のみを
載せる）。形式・許可key・escape規約はADR-0007が正本で、C-06は同じ規約で
「Controller以外が付加したmarker」を判定する（共有仕様点）。

- 形式: `<!-- CC_REVIEW_META:v1 {compact JSON} -->`（sorted keys、payload<=2048 bytes）
- payloadは構造key（`key` / `kind` / `run` / `head` / `seq` / `prev` / `audit_prev`）と、C-02が定義する
  projection key（検証済みpayloadからのscalar射影。ADR-0010）だけを持つ
- agent生成本文中の予約token（大小無視）は`CC~REVIEW~META`へ置換してescapeする
  （AC-C05-04）。redactionとは別の変換であり、順序はsanitize -> redact -> render ->
  attach（markerをredactへ通さない）
- 置換の不動点性: replacement `CC~REVIEW~META` のどの接頭辞もtokenの接尾辞と一致せず、
  どの接尾辞もtokenの接頭辞と一致しないため、置換結果に新たなtokenが（境界を跨いでも）
  生成されない。単一passで冪等（property testで常設検証）
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from ..errors import ErrorCategory
from ..schema.projection import PROJECTION_KEYS
from .gh import TransportError

MARKER_TOKEN: Final = "CC_REVIEW_META"
MARKER_VERSION: Final = "v1"
ESCAPED_TOKEN: Final = "CC~REVIEW~META"
MAX_PAYLOAD_BYTES: Final = 2048

# chainの構造key（識別・順序・連結）。意味情報は持たない
STRUCTURAL_PAYLOAD_KEYS: Final = frozenset({"key", "kind", "run", "head", "seq", "prev", "audit_prev"})
# markerへ載せてよいkeyの許可集合。credentialを想起させる語や本文を持ち込まない。
# projection keyの定義と語彙はC-02（schema.projection）が所有する（ADR-0010）
ALLOWED_PAYLOAD_KEYS: Final = STRUCTURAL_PAYLOAD_KEYS | PROJECTION_KEYS

_TOKEN_PATTERN = re.compile(re.escape(MARKER_TOKEN), re.IGNORECASE)
_MARKER_PATTERN = re.compile(
    r"<!-- " + re.escape(MARKER_TOKEN) + ":" + re.escape(MARKER_VERSION) + r" (\{.*\}) -->\s*\Z",
    re.DOTALL,
)


@dataclass(frozen=True)
class SanitizedBody:
    """agent本文のescape結果。escaped_countは置換した予約tokenの数。"""

    text: str
    escaped_count: int


@dataclass(frozen=True)
class ExtractedMarker:
    """本文末尾のmarker。payloadはJSONとして解釈できた場合のみ（不能ならNone）。"""

    raw_json: str
    payload: Mapping[str, object] | None


def sanitize_agent_body(body: str) -> SanitizedBody:
    """agent生成本文中の予約tokenをescapeする（Controller markerの偽造防止。AC-C05-04）。"""
    text, count = _TOKEN_PATTERN.subn(ESCAPED_TOKEN, body)
    return SanitizedBody(text=text, escaped_count=count)


def attach_marker(body: str, payload: Mapping[str, object]) -> str:
    """Controller markerを本文末尾へ1行で付加する。payloadは許可keyとstr / int値のみ。"""
    for key, value in payload.items():
        if key not in ALLOWED_PAYLOAD_KEYS:
            raise TransportError("marker", f"markerの許可されないkey: {key}", ErrorCategory.PERMANENT)
        if not isinstance(value, str | int) or isinstance(value, bool):
            raise TransportError("marker", f"markerの値はstrまたはint（key: {key}）", ErrorCategory.PERMANENT)
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise TransportError("marker", "marker payloadが上限を超えた", ErrorCategory.PERMANENT)
    marker_line = f"<!-- {MARKER_TOKEN}:{MARKER_VERSION} {encoded} -->"
    return f"{body.rstrip(chr(10))}\n\n{marker_line}"


def extract_marker(body: str) -> ExtractedMarker | None:
    """本文末尾のmarkerを取り出す。markerが無ければNone。JSON不正はpayload=None。"""
    match = _MARKER_PATTERN.search(body)
    if match is None:
        return None
    raw_json = match.group(1)
    try:
        # patternが`{...}`を要求するため、成功時は必ずJSON object（dict）になる
        parsed: dict[str, object] = json.loads(raw_json)
    except ValueError:
        return ExtractedMarker(raw_json=raw_json, payload=None)
    return ExtractedMarker(raw_json=raw_json, payload=parsed)
