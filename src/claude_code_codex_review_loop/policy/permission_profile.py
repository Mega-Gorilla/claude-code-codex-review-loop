# SPDX-License-Identifier: Apache-2.0
"""C-04のpermission profile（純粋な選択規則）とargvの禁止flag検査。

profileの値域はAuto / acceptEdits / default / dontAskのみで、permission bypass系は
enumに存在せず構築経路を持たない（P-006）。Auto modeの利用可否の検出は環境依存の
I/OであるためC-06が担い（AC-C06-10）、本moduleは可否を入力に取る純粋な規則だけを持つ。

ensure_argv_allowedは、静的なcontract test（tests/test_repository_contract.py）では
原理的に検出できない「分割連結で構築された禁止flag」への補完となるruntimeの
choke pointである（ADR-0006）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import Enum, unique
from typing import Final


class PolicyError(Exception):
    """C-04の構造化errorの基底。"""


class ForbiddenFlagError(PolicyError):
    """argvが禁止flag patternに一致した。error messageへ違反値そのものは含めない。"""

    def __init__(self, pattern_name: str, index: int) -> None:
        super().__init__(f"forbidden_flag: argv[{index}]が禁止flag patternに一致した（{pattern_name}）")
        self.pattern_name = pattern_name
        self.index = index


@unique
class PermissionProfile(Enum):
    """Claude Code側へ指定するpermission profileの値域。

    bypass系のprofileは意図的に存在しない（P-006。target experienceのDecidedにより
    presetから使用不可）。
    """

    AUTO = "auto"
    ACCEPT_EDITS = "acceptEdits"
    DEFAULT = "default"
    DONT_ASK = "dontAsk"


@unique
class ProfilePurpose(Enum):
    """profile選択の用途区分（target experienceのfallback区分）。"""

    AUTOMATION = "AUTOMATION"
    INTERACTIVE = "INTERACTIVE"
    NON_INTERACTIVE = "NON_INTERACTIVE"


# match文でなくMappingで表現する（実行不能なcase _を作らず、網羅性はtestで担保する）
_FALLBACK: Final[Mapping[ProfilePurpose, PermissionProfile]] = {
    ProfilePurpose.AUTOMATION: PermissionProfile.ACCEPT_EDITS,
    ProfilePurpose.INTERACTIVE: PermissionProfile.DEFAULT,
    ProfilePurpose.NON_INTERACTIVE: PermissionProfile.DONT_ASK,
}


def select_profile(auto_mode_available: bool, purpose: ProfilePurpose) -> PermissionProfile:
    """Auto modeが利用可能ならAutoを、不可なら用途別のfallbackを返す（AC-C06-10の規則部分）。"""
    if auto_mode_available:
        return PermissionProfile.AUTO
    return _FALLBACK[purpose]


# 区切り可変・大小無視のregexで検査する。regex sourceとpattern名は禁止語の
# 隣接列を含まない形（名前は語順を反転）にし、contract testの走査対象に含めても
# 自己検出しない
_FORBIDDEN_FLAG_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("permission-bypass", re.compile(r"bypass[\s_-]*permissions", re.IGNORECASE)),
    ("permission-dangerous-skip", re.compile(r"dangerously[\s_-]*skip[\s_-]*permissions", re.IGNORECASE)),
    # Codex側の全権限・sandbox迂回、および`-c`で指定できる既知の危険な設定値も同じ
    # choke pointで拒否する。語は区切り可変にし、static contract testの対象語を連結で
    # 再導入しない（C-09）。
    (
        "codex-approval-sandbox-bypass",
        re.compile(
            r"dangerously[\s_-]*bypass[\s_-]*(?:approvals[\s_-]*and[\s_-]*sandbox|hook[\s_-]*trust)", re.IGNORECASE
        ),
    ),
    ("codex-danger-full-access", re.compile(r"danger[\s_-]*full[\s_-]*access", re.IGNORECASE)),
    (
        "codex-shell-environment-inherit-all",
        re.compile(r"shell[\s_.=.-]*environment[\s_.=.-]*policy[\s_.=.-]*inherit[\s_.=.-]*all", re.IGNORECASE),
    ),
    (
        "codex-sandbox-full-read",
        re.compile(
            r"sandbox[\s_.=\[\]\"'-]*permissions[\s_.=\[\]\"'-]*disk[\s_.=\[\]\"'-]*full[\s_-]*read[\s_-]*access",
            re.IGNORECASE,
        ),
    ),
)


def ensure_argv_allowed(argv: Sequence[str]) -> None:
    """argvへ禁止flagが混入していないことを検査する（P-006のruntime choke point）。"""
    for index, argument in enumerate(argv):
        for name, pattern in _FORBIDDEN_FLAG_PATTERNS:
            if pattern.search(argument):
                raise ForbiddenFlagError(name, index)
