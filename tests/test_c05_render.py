# SPDX-License-Identifier: Apache-2.0
"""公開用render pipelineの受入test（順序: sanitize -> redact -> render。AC-C04-01の接続）。"""

from __future__ import annotations

import pytest

from claude_code_codex_review_loop.transport import (
    ESCAPED_TOKEN,
    MARKER_TOKEN,
    TransportError,
    attach_marker,
    prepare_public_body,
    prepare_user_body,
)


def test_header_shows_speaker_and_model() -> None:
    prepared = prepare_public_body("review本文", speaker="Codex", model="model-x")
    assert prepared.text.startswith("**Codex**（model: model-x）\n\n")
    assert "review本文" in prepared.text  # 意味的内容は不変


def test_secrets_are_removed_end_to_end() -> None:
    """C-05独自patternを持たず、C-04のredact()が適用される（投稿前redaction）。"""
    secret = "ghp_" + "a1B2" * 9
    prepared = prepare_public_body(f"token {secret} を使う", speaker="Codex", model="m")
    assert secret not in prepared.text
    assert any(hit.name == "github-token" for hit in prepared.redaction_hits)


def test_agent_marker_is_escaped_and_counted() -> None:
    prepared = prepare_public_body("偽marker " + MARKER_TOKEN + " 入り", speaker="Claude", model="m")
    assert MARKER_TOKEN not in prepared.text
    assert ESCAPED_TOKEN in prepared.text
    assert prepared.escaped_marker_count == 1


def test_clean_body_reports_no_hits() -> None:
    prepared = prepare_public_body("安全な本文", speaker="Codex", model="m")
    assert prepared.redaction_hits == ()
    assert prepared.escaped_marker_count == 0


def test_pipeline_then_attach_yields_single_controller_marker() -> None:
    """sanitize -> redact -> render -> attach の全pipelineでmarkerがControllerの1つだけになる。"""
    agent_body = "秘密 ghp_" + "z9Y8" * 9 + " と偽 " + MARKER_TOKEN
    prepared = prepare_public_body(agent_body, speaker="Codex", model="m")
    final = attach_marker(prepared.text, {"key": "turn-1"})
    assert final.count(MARKER_TOKEN) == 1
    assert "ghp_" not in final


@pytest.mark.parametrize("speaker,model", [("", "m"), ("s", ""), ("s\nx", "m"), ("s", "m\rx")])
def test_invalid_header_fields_are_rejected(speaker: str, model: str) -> None:
    with pytest.raises(TransportError) as excinfo:
        prepare_public_body("body", speaker=speaker, model=model)
    assert excinfo.value.stage == "render"


class TestUserTranscript:
    """ユーザー入力の転記は、発言者と**入力経路**を明示する（ADR-0018 決定14）。"""

    def test_header_shows_speaker_and_route(self) -> None:
        prepared = prepare_user_body("mergeを承認します", speaker="User", route="host_transcript")
        assert prepared.text.startswith("**User**（入力経路: host_transcript）\n\n")
        assert "mergeを承認します" in prepared.text

    def test_secrets_are_removed_end_to_end(self) -> None:
        """agent発言と同じredaction choke pointを通る（転記だけ素通しにしない）。"""
        secret = "ghp_" + "a1B2" * 9
        prepared = prepare_user_body(f"token {secret}", speaker="User", route="host_transcript")
        assert secret not in prepared.text
        assert any(hit.name == "github-token" for hit in prepared.redaction_hits)

    def test_embedded_marker_is_escaped(self) -> None:
        prepared = prepare_user_body(MARKER_TOKEN, speaker="User", route="host_transcript")
        assert MARKER_TOKEN not in prepared.text and ESCAPED_TOKEN in prepared.text
        assert prepared.escaped_marker_count == 1

    @pytest.mark.parametrize(
        ("speaker", "route"),
        [("", "route"), ("User", ""), ("a\nb", "route"), ("User", "a\nb")],
        ids=["empty_speaker", "empty_route", "multiline_speaker", "multiline_route"],
    )
    def test_header_fields_must_be_a_single_non_empty_line(self, speaker: str, route: str) -> None:
        with pytest.raises(TransportError) as excinfo:
            prepare_user_body("body", speaker=speaker, route=route)
        assert excinfo.value.stage == "render"
