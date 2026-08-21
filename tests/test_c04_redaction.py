# SPDX-License-Identifier: Apache-2.0
"""C-04 redactionの受入test（AC-C04-01）。

疑似tokenは実行時連結で生成し、fileへtoken形式のliteralを書かない（P-015。
secret scannerの誤検知も避ける）。冪等性・markerの非再match・hitsへの秘密値
非保持を固定する。
"""

from __future__ import annotations

import time

import pytest

from claude_code_codex_review_loop.policy import REDACTION_PATTERNS, redact

# pattern名 -> 実行時連結で生成した代表値（この値が出力から消えることを検証する）
_SECRET_SAMPLES: dict[str, str] = {
    "github-token": "ghp" + "_" + "a1B2" * 9,  # 36文字の本体
    "github-fine-grained": "github" + "_pat_" + "x" * 82,
    "anthropic-key": "sk-" + "ant-" + "k" * 24,
    "openai-key": "sk-" + "o" * 40,
    "aws-access-key": "AKIA" + "ABCDEFGHIJKLMNOP",
    "google-api-key": "AIza" + "B" * 35,
    "slack-token": "xoxb-" + "1234567890-abcdef",
    "jwt": "eyJ" + "h" * 10 + "." + "p" * 12 + "." + "s" * 16,
    "private-key-block": (
        "-----BEGIN PRIVATE KEY-----\n" + "MIIEvQIBADANBg\n" * 3 + "-----END PRIVATE KEY-----"
    ),
    "authorization-header": "Authorization: Bearer " + "t0ken" * 4,
    "url-userinfo": "deploy:" + "p4ss" * 3,  # https://<これ>@host の形で使う
    "env-assignment": "GH_" + "TOKEN=" + "v" * 12,
}


class TestRemoval:
    @pytest.mark.parametrize("name", sorted(_SECRET_SAMPLES))
    def test_each_pattern_is_removed(self, name: str) -> None:
        sample = _SECRET_SAMPLES[name]
        text = f"before {sample} after" if name != "url-userinfo" else f"see https://{sample}@example.invalid/repo"
        result = redact(text)
        secret_core = sample.split(":", 1)[-1] if name == "authorization-header" else sample
        assert secret_core not in result.text, name
        assert any(hit.name == name for hit in result.hits), (name, result.hits)

    @pytest.mark.parametrize("layout", ["前接{}", "{}後続", "鍵{}を保存した"])
    def test_japanese_adjacency_is_not_a_word_boundary_escape(self, layout: str) -> None:
        """日本語隣接（\\bでは境界にならない）でも取り逃さない。"""
        sample = _SECRET_SAMPLES["github-token"]
        result = redact(layout.format(sample))
        assert sample not in result.text
        assert result.hits

    def test_multiple_occurrences_are_counted(self) -> None:
        sample = _SECRET_SAMPLES["aws-access-key"]
        result = redact(f"{sample} と {sample}")
        assert [(h.name, h.count) for h in result.hits] == [("aws-access-key", 2)]

    def test_empty_text(self) -> None:
        result = redact("")
        assert result == redact("")
        assert result.text == "" and result.hits == ()


class TestFalsePositiveGuard:
    _CLEAN_TEXTS = (
        "commit 36841d2057307a2f587f839d5c7b6629d9fda46c を確認",  # SHA40
        "id: 550e8400-e29b-41d4-a716-446655440000",  # UUID
        "base64: eyJhbGciOiJIUzI1NiJ9dGVzdA",  # dotなし
        "通常の日本語の説明文です。taskとriskの話をします。",
        "see https://example.invalid/path and ssh://git@example.invalid/repo.git",
        "C:\\Users\\dev\\.claude\\settings.json を確認",
        "2026-08-21T06:00:00Z に完了",
        "github_pat_helper_function() を呼ぶ",  # 60文字未満の識別子
        "Authorization is required for this endpoint",
        "| --- | --- |",
    )

    @pytest.mark.parametrize("text", _CLEAN_TEXTS)
    def test_clean_text_is_unchanged(self, text: str) -> None:
        result = redact(text)
        assert result.text == text
        assert result.hits == ()


class TestIdempotence:
    def test_redact_is_idempotent_on_all_samples(self) -> None:
        """redact(redact(x)) == redact(x)（marker再matchなし）をpropertyとして常設。"""
        violations: list[str] = []
        combined = "\n".join(
            f"https://{v}@example.invalid" if k == "url-userinfo" else v for k, v in _SECRET_SAMPLES.items()
        )
        for text in (*TestFalsePositiveGuard._CLEAN_TEXTS, combined, "混在: " + combined + " 終わり"):
            once = redact(text)
            twice = redact(once.text)
            if twice.text != once.text:
                violations.append(text[:50])
        assert not violations, violations

    def test_markers_do_not_match_any_pattern(self) -> None:
        for entry in REDACTION_PATTERNS:
            marker = f"[REDACTED:{entry.name}]"
            result = redact(f"log line with {marker} inside")
            assert result.hits == (), entry.name


class TestPrivateKeyEdgeCases:
    def test_unterminated_pem_is_redacted_to_the_end(self) -> None:
        """ENDが欠けた（log切詰め）PEMを素通りさせず末尾までredactする。"""
        body = "-----BEGIN RSA PRIVATE KEY-----\n" + "MIIEvQ\n" * 5 + "（切詰め）"
        result = redact("先頭のlog\n" + body)
        assert "MIIEvQ" not in result.text
        assert any(hit.name == "private-key-unterminated" for hit in result.hits)
        assert result.text.startswith("先頭のlog")

    def test_oversized_pem_falls_back_to_unterminated(self) -> None:
        """10KB超の異常なPEM本体はpairに落とさず、安全側のunterminatedでredactする。"""
        huge = "-----BEGIN PRIVATE KEY-----\n" + "A" * 20_000 + "\n-----END PRIVATE KEY-----"
        result = redact(huge)
        assert "A" * 100 not in result.text
        assert any(hit.name == "private-key-unterminated" for hit in result.hits)

    def test_adversarial_repeated_begins_complete_quickly(self) -> None:
        """END欠落×多数BEGINの敵対的入力でも超線形時間にならない（長さcapの検証）。"""
        text = ("-----BEGIN PRIVATE KEY----- " + "x" * 20 + "\n") * 3000
        started = time.monotonic()
        result = redact(text)
        assert time.monotonic() - started < 5.0
        assert "x" * 20 not in result.text

    def test_terminated_and_unterminated_mixed(self) -> None:
        pair = _SECRET_SAMPLES["private-key-block"]
        tail = "-----BEGIN EC PRIVATE KEY-----\nQQQQ"
        result = redact(f"{pair}\nmiddle\n{tail}")
        names = {hit.name for hit in result.hits}
        assert {"private-key-block", "private-key-unterminated"} <= names
        assert "QQQQ" not in result.text and "MIIEvQIBADANBg" not in result.text


class TestWrapperValueForms:
    """wrapperは名前で識別できた時点で、値の長さ・scheme・quote形式に依存せず値全体をredactする。"""

    @pytest.mark.parametrize(
        "text,secret",
        [
            ("Authorization: ApiKey supersecret123", "supersecret123"),  # 未知scheme
            ("Authorization: Bearer x1", "x1"),  # 短い値
            ('"Authorization": "Bearer abc"', "abc"),  # JSON形式
            ("ANTHROPIC_" + "AUTH_TOKEN=short", "short"),  # 短い非空値
            ('"ANTHROPIC_' + 'AUTH_TOKEN": "opaque-secret-value"', "opaque-secret-value"),  # JSON key/value
            ("ANTHROPIC_" + 'AUTH_TOKEN="opaque secret value"', "opaque secret value"),  # 空白を含むshell quote
            ("ANTHROPIC_" + "AUTH_TOKEN='single quoted secret'", "single quoted secret"),
        ],
    )
    def test_value_is_redacted_regardless_of_form(self, text: str, secret: str) -> None:
        result = redact(text)
        assert secret not in result.text, text
        assert result.hits, text
        # 修正後も冪等
        assert redact(result.text).text == result.text

    def test_quoted_value_does_not_swallow_following_fields(self) -> None:
        """JSONの同一行にある後続の非秘密fieldを飲み込まない。"""
        text = '{"Authorization": "Bearer abc", "next": "keep", "ANTHROPIC_' + 'AUTH_TOKEN": "v1", "tail": "stay"}'
        result = redact(text)
        assert '"next": "keep"' in result.text
        assert '"tail": "stay"' in result.text
        assert "abc" not in result.text and '"v1"' not in result.text

    def test_workflow_secret_reference_is_not_a_secret(self) -> None:
        """`${{ secrets.X }}`は参照であり秘密値でない（既知のFNとして正しい挙動）。"""
        text = "GITHUB_" + "TOKEN: ${{ secrets.GITHUB_TOKEN }}"
        result = redact(text)
        assert result.text == text
        assert result.hits == ()


class TestNonRetention:
    def test_hits_do_not_retain_secret_values(self) -> None:
        """hitsはpattern名と件数のみ（P-015）。"""
        sample = _SECRET_SAMPLES["github-token"]
        result = redact(f"token: {sample}")
        for hit in result.hits:
            assert sample not in hit.name
            assert isinstance(hit.count, int)

    def test_wrapper_swallows_earlier_marker(self) -> None:
        """env代入の値が先行patternでmarker化されても、wrapperが変数名ごと飲み込み安定する。"""
        text = "GH_" + "TOKEN=" + _SECRET_SAMPLES["github-token"]
        once = redact(text)
        assert redact(once.text).text == once.text
        assert "GH_TOKEN" not in once.text or "[REDACTED:" in once.text
