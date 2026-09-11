# SPDX-License-Identifier: Apache-2.0
"""C-09 head binding（AC-C09-04）: 隔離checkoutのHEAD、PRのadvertised head、review出力の対象headの三者照合。

review結果は特定のhead SHAへbindされ、headが変われば失効する（target-experience「head binding」）。
3つのheadは異なる経路から来る:

- checkout head: 隔離checkoutの`rev-parse HEAD`（`observe_checkout_head`）
- advertised head: PRが現在advertiseしているhead（C-05 `get_pull_request`、C-07の観測）
- reported head: reviewerの出力が対象として明記したhead（C-10がreportから取り出す）

1つでも一致しない、または形式が不正なら`HeadMismatch`を返し、呼出側（C-10）は結果を投稿しない
（head race時の非投稿）。判定は純粋関数で、GitHubにもfilesystemにも触れない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

_SHA1: Final = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class HeadsBound:
    """3つのheadが一致した。reviewはこのheadへbindされる。"""

    head_sha: str


@dataclass(frozen=True)
class HeadMismatch:
    """一致しない、または形式不正。`reasons`は固定語彙で、値の詳細を含めない。"""

    checkout_head: str
    advertised_head: str
    reported_head: str
    reasons: tuple[str, ...]


def verify_review_target(*, checkout_head: str, advertised_head: str, reported_head: str) -> HeadsBound | HeadMismatch:
    """三者照合。形式不正はそれ自体を理由にし、値の比較は3つとも正しい形式のときだけ行う。"""
    reasons: list[str] = []
    for label, value in (("checkout", checkout_head), ("advertised", advertised_head), ("reported", reported_head)):
        if not isinstance(value, str) or _SHA1.fullmatch(value) is None:
            reasons.append(f"{label}_invalid")
    if not reasons:
        if advertised_head != checkout_head:
            reasons.append("advertised_moved")
        if reported_head != checkout_head:
            reasons.append("reported_differs")
    if reasons:
        return HeadMismatch(
            checkout_head=checkout_head,
            advertised_head=advertised_head,
            reported_head=reported_head,
            reasons=tuple(reasons),
        )
    return HeadsBound(head_sha=checkout_head)
