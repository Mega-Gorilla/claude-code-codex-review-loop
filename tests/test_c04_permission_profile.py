# SPDX-License-Identifier: Apache-2.0
"""C-04 permission profileと禁止flag runtime検査の受入test（P-006、AC-C06-10の規則部分）。

禁止flagのfixtureは実行時連結で構築する（literalを書くと本repositoryの
禁止flag contract testに自己検出されるため）。
"""

from __future__ import annotations

import pytest

from claude_code_codex_review_loop.policy import (
    ForbiddenFlagError,
    PermissionProfile,
    ProfilePurpose,
    ensure_argv_allowed,
    select_profile,
)
from claude_code_codex_review_loop.policy.permission_profile import _FALLBACK


class TestSelectProfile:
    @pytest.mark.parametrize("purpose", list(ProfilePurpose))
    def test_auto_mode_is_preferred_when_available(self, purpose: ProfilePurpose) -> None:
        assert select_profile(True, purpose) is PermissionProfile.AUTO

    @pytest.mark.parametrize(
        "purpose,expected",
        [
            (ProfilePurpose.AUTOMATION, PermissionProfile.ACCEPT_EDITS),
            (ProfilePurpose.INTERACTIVE, PermissionProfile.DEFAULT),
            (ProfilePurpose.NON_INTERACTIVE, PermissionProfile.DONT_ASK),
        ],
    )
    def test_fallback_by_purpose(self, purpose: ProfilePurpose, expected: PermissionProfile) -> None:
        assert select_profile(False, purpose) is expected

    def test_fallback_mapping_is_exhaustive(self) -> None:
        """match文の代わりのMappingが全purposeを覆う（網羅性の担保）。"""
        assert set(_FALLBACK) == set(ProfilePurpose)


class TestProfileValues:
    def test_enum_values_are_the_claude_code_presets(self) -> None:
        assert {profile.value for profile in PermissionProfile} == {"auto", "acceptEdits", "default", "dontAsk"}

    def test_no_bypass_profile_is_representable(self) -> None:
        """bypass系profileは値域に存在しない（P-006）。"""
        for profile in PermissionProfile:
            assert "bypass" not in profile.value.casefold()
            assert "skip" not in profile.value.casefold()


def _forbidden_variants() -> list[str]:
    """禁止flagの変種を実行時連結で構築する（fileへliteralを書かない）。"""
    bypass = "bypass"
    skip = "dangerously" + "-skip"
    return [
        "--" + bypass + "Permissions",
        bypass + "_permissions",
        bypass + "permissions",
        "--" + skip + "-permissions",
        skip.replace("-", "_") + "_permissions",
        ("Dangerously " + "Skip " + "Permissions"),
        "--flag=" + bypass + "-Permissions",
    ]


class TestEnsureArgvAllowed:
    def test_clean_argv_passes(self) -> None:
        ensure_argv_allowed(["codex", "exec", "--sandbox", "read-only", "--json"])

    def test_empty_argv_passes(self) -> None:
        ensure_argv_allowed([])

    @pytest.mark.parametrize("variant", _forbidden_variants())
    def test_forbidden_variants_are_rejected(self, variant: str) -> None:
        with pytest.raises(ForbiddenFlagError):
            ensure_argv_allowed(["claude", variant])

    def test_error_does_not_leak_the_offending_value(self) -> None:
        """errorへ違反値そのものを含めない（logへの禁止語再導入を防ぐ）。"""
        variant = _forbidden_variants()[0]
        with pytest.raises(ForbiddenFlagError) as excinfo:
            ensure_argv_allowed([variant])
        assert variant not in str(excinfo.value)
        assert excinfo.value.index == 0
        assert excinfo.value.pattern_name == "permission-bypass"
