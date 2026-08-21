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


def _validate_header_field(label: str, value: str) -> None:
    if not value or "\n" in value or "\r" in value:
        raise TransportError("render", f"{label}は非空の1行でなければならない", ErrorCategory.PERMANENT)


def prepare_public_body(agent_body: str, *, speaker: str, model: str) -> PreparedBody:
    """agent発言を公開用へ変換する: sanitize -> redact -> 発言者/model headerの前置。"""
    _validate_header_field("speaker", speaker)
    _validate_header_field("model", model)
    sanitized = sanitize_agent_body(agent_body)
    redacted = redact(sanitized.text)
    text = f"**{speaker}**（model: {model}）\n\n{redacted.text}"
    return PreparedBody(
        text=text,
        redaction_hits=redacted.hits,
        escaped_marker_count=sanitized.escaped_count,
    )
