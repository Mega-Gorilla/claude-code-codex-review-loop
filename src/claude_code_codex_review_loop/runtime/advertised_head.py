# SPDX-License-Identifier: Apache-2.0
"""C-05の`get_pull_request`（ADR-0012）でGitHub上の現在のPR headを読む`AdvertisedHeadPort`実装（ADR-0030 決定6）。

reviewer turn adapterは、review終了後・投稿判断の直前にこのportでheadを取り直し、三者照合へ渡す。
本moduleは取得値を加工せず、head SHAだけを返す。PRのstate（closed / merged）の扱いはC-10の責務で、
ここでは判断しない。失敗はC-05の分類（`ErrorCategory`）を固定stageへ写した`TurnError`で報告し、
応答本文・URL・native出力を含めない。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..transport import GhContext, RepoRef, RetryPolicy, TransportError, get_pull_request
from .reviewer_turn import TurnError


@dataclass(frozen=True)
class GitHubAdvertisedHead:
    """認証済み`gh`の文脈（C-05 `GhContext`）とbounded retry方針を束ねたport実装。"""

    context: GhContext
    policy: RetryPolicy

    def advertised_head(self, *, repository: str, number: int) -> str:
        owner, separator, name = repository.partition("/")
        if not separator or not owner or not name or "/" in name:
            raise TurnError("advertised_head:repository")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise TurnError("advertised_head:number")
        try:
            pull = get_pull_request(self.context, RepoRef(owner=owner, name=name), number, policy=self.policy)
        except TransportError as error:
            raise TurnError(f"advertised_head:{error.category.value.lower()}") from error
        return pull.head_sha
