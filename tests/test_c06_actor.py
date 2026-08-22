# SPDX-License-Identifier: Apache-2.0
"""actor解決とlogin正規化の受入test（C-06。D-031「完全一致」の解釈はADR-0008）。"""

from __future__ import annotations

import pytest

from claude_code_codex_review_loop.identity import ActorClass, normalize_login, resolve_actor


class TestNormalizeLogin:
    def test_ascii_lowercase(self) -> None:
        assert normalize_login("Mega-Gorilla") == "mega-gorilla"

    def test_already_lowercase_is_unchanged(self) -> None:
        assert normalize_login("controller-bot") == "controller-bot"


class TestResolveActor:
    """安全境界はcharset guard + allowlist完全一致。USER以外は全てdeny側。"""

    def test_missing_login(self) -> None:
        resolved = resolve_actor(None)
        assert resolved.klass is ActorClass.MISSING
        assert resolved.login is None and resolved.raw_login is None

    @pytest.mark.parametrize("raw", ["github-actions[bot]", "Dependabot[Bot]"])
    def test_bot_suffix_is_classified(self, raw: str) -> None:
        resolved = resolve_actor(raw)
        assert resolved.klass is ActorClass.BOT
        assert resolved.login is None and resolved.raw_login == raw

    @pytest.mark.parametrize("raw", ["", "user name", "ユーザー", "a_b", "KKlvin"])
    def test_charset_violation_is_invalid(self, raw: str) -> None:
        """Unicode casefold縮退（U+212A等）を含む非ASCIIはguardでdenyへ倒す。"""
        resolved = resolve_actor(raw)
        assert resolved.klass is ActorClass.INVALID
        assert resolved.login is None and resolved.raw_login == raw

    def test_user_is_normalized(self) -> None:
        resolved = resolve_actor("Mega-Gorilla")
        assert resolved.klass is ActorClass.USER
        assert resolved.login == "mega-gorilla"
        assert resolved.raw_login == "Mega-Gorilla"
