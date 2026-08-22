# SPDX-License-Identifier: Apache-2.0
"""actor解決とlogin正規化（C-06）。

C-05の`UnverifiedComment.author_login`（未検証・無加工）を、受理判定に使える型付き分類へ
解決する。安全境界はallowlist完全一致とcharset guardであり、本moduleは判定材料を作るだけで
受理そのものは行わない（受理は`identity.allowlist`）。

- 「完全一致」（D-031）の解釈: GitHubのloginはcase-insensitiveに一意であるため、
  charset guard通過後のASCII lowercase正規化は受理集合を別のaccountへ広げない
  （false acceptを生まない）。一方、露骨なcase-sensitive一致は設定の大文字小文字ズレによる
  false denyを常態化させる。ADR-0006がloginの正規化をC-06の集合構築時責務として委譲済みで、
  規約の正本はADR-0008
- charset guardはGitHub loginの文字集合`[A-Za-z0-9-]`への適合を先に検査し、非適合は
  INVALIDとしてdenyする。Unicode casefoldの縮退（U+212A等）を持ち込まないため、正規化は
  ASCII限定の`str.lower()`で足りる
- bot判定（`[bot]` suffix）は拒否理由の可読性のための分類であり、`[`/`]`はcharset外なので
  botはallowlistと構造的に一致し得ない（C-05へ`user.type`の露出は追加しない。ADR-0008）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final

_LOGIN_PATTERN: Final = re.compile(r"[A-Za-z0-9-]+\Z")
_BOT_SUFFIX: Final = "[bot]"


@unique
class ActorClass(Enum):
    """actor解決の分類。USER以外は全てdeny側（fail closed）。"""

    USER = "USER"  # 正規loginとして解決できた
    BOT = "BOT"  # loginが"[bot]" suffixを持つ
    INVALID = "INVALID"  # GitHub loginのcharset外（防御的deny）
    MISSING = "MISSING"  # author_loginがない（削除済みaccount等）


@dataclass(frozen=True)
class ResolvedActor:
    """解決済みactor。loginはUSERのときのみ正規化済みloginを持つ。"""

    klass: ActorClass
    login: str | None
    raw_login: str | None


def normalize_login(raw: str) -> str:
    """charset guard通過済みloginのASCII lowercase正規化。集合構築と照合の両側で使う。"""
    return raw.lower()


def resolve_actor(author_login: str | None) -> ResolvedActor:
    """未検証のauthor loginを型付き分類へ解決する（受理判定はしない）。"""
    if author_login is None:
        return ResolvedActor(klass=ActorClass.MISSING, login=None, raw_login=None)
    if author_login.lower().endswith(_BOT_SUFFIX):
        return ResolvedActor(klass=ActorClass.BOT, login=None, raw_login=author_login)
    if _LOGIN_PATTERN.fullmatch(author_login) is None:
        return ResolvedActor(klass=ActorClass.INVALID, login=None, raw_login=author_login)
    return ResolvedActor(klass=ActorClass.USER, login=normalize_login(author_login), raw_login=author_login)
