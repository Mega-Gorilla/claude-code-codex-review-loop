# SPDX-License-Identifier: Apache-2.0
"""pid生存判定（OS非依存のfacade）。

stale lockの回収可否（C-07）が唯一の利用者で、OS分岐は`spawn.py`が持つ`_backend`を
そのまま使う（分岐点を増やさない。C-03のterminate facadeと同じ構造）。

判定の非対称性: 「不在」と確定できた場合だけFalseを返し、権限不足やpid再利用のような
曖昧な状況はTrue（生存扱い）へ倒す。回収は「生存していないこと」を条件にするため、
曖昧さは常に**回収しない**側へ働く（ADR-0011）。
"""

from __future__ import annotations

from .spawn import _backend


def is_process_alive(pid: int) -> bool:
    """pidのprocessが生存しているか（曖昧な場合はTrue = 生存扱い）。"""
    return bool(_backend.is_process_alive(pid))
