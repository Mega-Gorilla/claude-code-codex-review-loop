# SPDX-License-Identifier: Apache-2.0
"""C-08 active host protocolとstep engine（Phase 8）。

Controller CLIはClaude Code sessionの子processであり、親のLLM turnを呼び戻せない。
そのためcore engineはClaudeを起動せず、`advance`で次の`HOST_ACTION`を返し、active hostが
自分のcontextで実行して`submit`で結果を返す**step engine**とする（implementation plan
Section 2の制御反転）。この構造は全workflowの前提で、覆すと全体の書き直しになる。

本packageの現在の内容はaction registry（`actions`）だけで、engineとadapterは後続PRが
追加する。registryの正本はADR-0014。
"""

from .actions import (
    ACTION_SPECS,
    RESULT_VARIANTS,
    ActionRegistryError,
    ActionSpec,
    ResultVariant,
    spec_for,
    spec_for_kind,
)

__all__ = [
    "ACTION_SPECS",
    "RESULT_VARIANTS",
    "ActionRegistryError",
    "ActionSpec",
    "ResultVariant",
    "spec_for",
    "spec_for_kind",
]
