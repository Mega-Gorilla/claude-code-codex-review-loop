# SPDX-License-Identifier: Apache-2.0
"""C-04のcredential redaction（patternの単一registry。AC-C04-01）。

投稿本文 / prompt / log / artifactへのredactionは、各consumer（C-05の投稿前、
C-07のprompt組立、C-08のlog、C-12のartifact）が本moduleのredact()を呼ぶことで
共通適用する。patternはREDACTION_PATTERNSだけで管理し、consumer側に独自patternを
持たせない。設計判断（ASCII lookaroundの採用理由、既知のFP / FN、PEMの長さcap）は
ADR-0006を正本とする。

- 置換は`[REDACTED:<pattern名>]`。markerはどのpatternにも再matchせず、redactは冪等
- RedactionResult.hitsはpattern名と件数のみで、秘密値そのものを保持しない（P-015）
- 語境界は`\\b`でなくASCII lookaroundを使う（日本語隣接では`\\b`が境界にならず
  取り逃すため）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

# 日本語（漢字・かな）はUnicodeの語文字であり`\b`が境界にならないため、ASCIIの
# 英数字とunderscoreだけを語文字とみなすlookaroundで境界を定義する
_L = r"(?<![0-9A-Za-z_])"
_R = r"(?![0-9A-Za-z_])"

# credentialを運ぶ環境変数名の正本。redactionのenv-assignment patternと、C-06の
# reviewer env denylist（identity/credentials.py）が同じ集合を参照し、片方だけが
# 更新されるdriftを防ぐ（test_c06_credentials.pyが両者の一致を常設検証する）
TOKEN_ENV_NAMES: Final[tuple[str, ...]] = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "OPENAI_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)


@dataclass(frozen=True)
class RedactionPattern:
    """redaction対象1種。nameは置換markerへ入る識別子（字母は[a-z-]に限定）。"""

    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class RedactionHit:
    """1 patternの置換実績。秘密値そのものは保持しない（P-015）。"""

    name: str
    count: int


@dataclass(frozen=True)
class RedactionResult:
    """redactの結果。textは置換後の本文、hitsは置換が発生したpatternの実績。"""

    text: str
    hits: tuple[RedactionHit, ...]


# 適用順: 具体的なtoken -> PEM block -> wrapper（header / URL / env代入）。
# 前段の置換markerを後段のwrapperが値として飲み込むことがあるが、全体の結果は
# 冪等に収束する（test_c04_redaction.pyのproperty testで常設検証）
REDACTION_PATTERNS: Final[tuple[RedactionPattern, ...]] = (
    RedactionPattern("github-token", re.compile(_L + r"gh[pousr]_[A-Za-z0-9]{20,}" + _R)),
    RedactionPattern("github-fine-grained", re.compile(_L + r"github_pat_[A-Za-z0-9_]{60,}" + _R)),
    RedactionPattern("anthropic-key", re.compile(_L + r"sk-ant-[A-Za-z0-9_-]{20,}" + _R)),
    RedactionPattern("openai-key", re.compile(_L + r"sk-[A-Za-z0-9_-]{32,}" + _R)),
    RedactionPattern("aws-access-key", re.compile(_L + r"(?:AKIA|ASIA)[0-9A-Z]{16}" + _R)),
    RedactionPattern("google-api-key", re.compile(_L + r"AIza[0-9A-Za-z_-]{35}" + _R)),
    RedactionPattern("slack-token", re.compile(_L + r"xox[abeprs]-[A-Za-z0-9-]{10,}" + _R)),
    RedactionPattern(
        "jwt",
        re.compile(_L + r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}" + _R),
    ),
    # 実際のPEM本体は数KBのため、敵対的入力での超線形時間を避ける長さcapを持つ
    RedactionPattern(
        "private-key-block",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.{0,10000}?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # END行が欠けた（logの切詰め等）PEMを素通りさせず、末尾まで安全側でredactする
    RedactionPattern(
        "private-key-unterminated",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*\Z", re.DOTALL),
    ),
    # wrapperは名前で識別できた時点で、値の長さ・schemeに依存せず値全体をredactする。
    # quoted値は閉じquoteで停止し（JSONの同一行にある他のfieldを飲み込まない）、
    # 未quoted値は行末まで安全側でredactする（headerは行単位のため）。値の文字クラスは
    # 単純な線形scanでbacktrackingが発生しないため、長さ上限は設けない（上限があると
    # 超過分の末尾が公開面へ残る）
    # quoted値はescape-awareに走査する: `\X`（escaped文字。escaped quoteとescaped
    # backslashを含む）は値の一部とし、escapeされていないquoteだけを終端とする。
    # 選択肢の先頭文字が排他（backslash / 非backslash）のためbacktrackingは線形。
    # 空のquoted値（"" / ''）も「正常に閉じた値」としてmatchさせ、fallbackへ流さない
    # （後続fieldを飲み込まない）。閉じquoteが行内に無い場合はfallback（headerは
    # 行末まで、envは開きquote以降を行末まで）で安全側にredactする
    RedactionPattern(
        "authorization-header",
        re.compile(
            r"(?<![0-9A-Za-z_])authorization[\"']?\s*[:=]\s*"
            r"(?:\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|[^\r\n]+)",
            re.IGNORECASE,
        ),
    ),
    RedactionPattern("url-userinfo", re.compile(r"(?<=://)[^/\s:@]*:[^/\s@]+(?=@)")),
    # 値は非空なら長さ・空白の有無を問わない。`${{ ... }}`はworkflowの参照であり
    # 秘密値ではないため対象外（既知のFNとして正しい挙動。ADR-0006）
    RedactionPattern(
        "env-assignment",
        re.compile(
            _L
            + r"(?:"
            + "|".join(TOKEN_ENV_NAMES)
            + r")"
            + r"[\"']?\s*[=:]\s*"
            + r"(?:\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|\"[^\r\n]*|'[^\r\n]*|(?!\$\{)\S+)"
        ),
    ),
)


def redact(text: str) -> RedactionResult:
    """全patternを順に適用し、redaction対象を`[REDACTED:<name>]`へ置換する。"""
    hits: list[RedactionHit] = []
    for entry in REDACTION_PATTERNS:
        text, count = entry.pattern.subn(f"[REDACTED:{entry.name}]", text)
        if count:
            hits.append(RedactionHit(name=entry.name, count=count))
    return RedactionResult(text=text, hits=tuple(hits))
