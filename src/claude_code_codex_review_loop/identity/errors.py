# SPDX-License-Identifier: Apache-2.0
"""C-06の構造化error。

violation（改ざんの証拠）とerror（呼び出し誤り・設定誤り）を区別する: 前者は
`IntegrityEvidenceRef`として検証結果に載り、後者は`IdentityError`としてraiseする。
設定誤りや一時障害からviolationを捏造しない（ADR-0008）。
"""

from __future__ import annotations


class IdentityError(Exception):
    """C-06の構造化errorの基底。stageは失敗した段階、detailは診断用の説明。"""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail
