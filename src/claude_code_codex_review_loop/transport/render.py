# SPDX-License-Identifier: Apache-2.0
"""公開用render（発言者とmodelの明示。意味内容は変更しない）。

投稿pipelineの順序（TE / ADR-0006 / ADR-0007）:
sanitize（予約markerのescape）-> redact（C-04の単一choke point）-> render（本関数。
発言者とmodelを明示するheaderの前置のみ）。Controller markerのattachは投稿直前に
最後に行い、markerをredactへ通さない。

C-05は独自のredaction patternを持たず、`policy.redact()`を呼ぶだけである。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ErrorCategory
from ..policy import RedactionHit, redact
from .gh import TransportError
from .marker import sanitize_agent_body


@dataclass(frozen=True)
class PreparedBody:
    """投稿できる状態の公開本文。hitsとescape数は監査用（秘密値を含まない）。"""

    text: str
    redaction_hits: tuple[RedactionHit, ...]
    escaped_marker_count: int


def normalize_newlines(text: str) -> str:
    """改行を`\\n`へ正規化する（ADR-0007の単一choke point。冪等）。

    本文hashと実際に投稿されるbytesを同じ正規化済み文字列から生成するため、
    marker付加・hash計算・file書込より前に必ず本関数を通す（prepare_public_bodyと
    post / ensure系の入口が呼ぶ）。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _validate_header_field(label: str, value: str) -> None:
    if not value or "\n" in value or "\r" in value:
        raise TransportError("render", f"{label}は非空の1行でなければならない", ErrorCategory.PERMANENT)


def prepare_user_body(user_body: str, *, speaker: str, route: str) -> PreparedBody:
    """ユーザー入力の転記を公開用へ変換する: 改行正規化 -> sanitize -> redact -> header。

    `prepare_public_body`はagent発言用でmodelの明示を要求するが、転記recordの内容を書いた
    のはmodelではなくユーザーである。ここではmodelの代わりに**入力経路**を明示する
    （TE「Controllerが入力経路を明記して転記したPR comment」）。sanitize / redactは
    agent発言と同じ単一choke pointを通す。
    """
    _validate_header_field("speaker", speaker)
    _validate_header_field("route", route)
    sanitized = sanitize_agent_body(normalize_newlines(user_body))
    redacted = redact(sanitized.text)
    text = f"**{speaker}**（入力経路: {route}）\n\n{redacted.text}"
    return PreparedBody(
        text=text,
        redaction_hits=redacted.hits,
        escaped_marker_count=sanitized.escaped_count,
    )


def prepare_public_body(agent_body: str, *, speaker: str, model: str) -> PreparedBody:
    """agent発言を公開用へ変換する: 改行正規化 -> sanitize -> redact -> headerの前置。"""
    _validate_header_field("speaker", speaker)
    _validate_header_field("model", model)
    sanitized = sanitize_agent_body(normalize_newlines(agent_body))
    redacted = redact(sanitized.text)
    text = f"**{speaker}**（model: {model}）\n\n{redacted.text}"
    return PreparedBody(
        text=text,
        redaction_hits=redacted.hits,
        escaped_marker_count=sanitized.escaped_count,
    )
