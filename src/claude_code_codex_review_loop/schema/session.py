# SPDX-License-Identifier: Apache-2.0
"""session configのschema v1（Phase 8 PR-3b1。ADR-0020）。

engineは既定値を持たない（解決はC-12）。したがってentry pointは全設定を受け取る必要があり、
それをrun directory内の`session.json`へ置く。**全fieldが必須**で、既定値の補完を行わない
（補完すればC-12の領域を侵す）。

checkpointと同じrun directoryに置くのは、**別processが同じportを再構成できる**ようにする
ためである。cross-process resume（AC-C08-06）は、checkpointだけでなくportの構成も
復元できて初めて成立する。

秘密値は置かない。GitHubの認証はgh CLI側の資格情報に委ね、本fileはcommand・path・上限値・
識別子だけを持つ（P-015）。
"""

from __future__ import annotations

from .registry import (
    SchemaDefinition,
    SchemaKind,
    array,
    integer,
    opaque,
    schema_version_field,
    sha,
    text,
)
from .validate import Field, VersionSpec


def _env_map() -> Field:
    """子processへ渡す環境変数の全体（任意keyのstring map）。"""
    return Field(types=(dict,), values=text(non_empty=False))


SESSION_CONFIG = SchemaDefinition(
    kind=SchemaKind.SESSION_CONFIG,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                # 対象runの識別（checkpointの外枠と一致することをruntimeが検査する）
                "run_id": opaque(),
                "repository": text(),
                "number": integer(),
                # 現在のhead。actionとuser requestのbind先になる
                "head_sha": sha(),
                # gh CLIの起動（`GhContext`。実行fileと引数、作業directory、timeout）
                "gh_command": array(text()),
                "gh_workdir": text(),
                # 時間は**すべてmilliseconds**の整数で持つ（validatorがfloatを扱わないため。
                # 秒への変換は決定論的な単位換算であって既定値の解決ではない）
                "gh_timeout_ms": integer(),
                "gh_grace_ms": integer(),
                # 子processへ渡す環境変数の**全体**（継承しない。C-03のexplicit env契約）
                "gh_env": _env_map(),
                # bounded retry（C-05のRetryPolicy）
                "retry_max_attempts": integer(),
                "retry_backoff_ms": integer(),
                "retry_max_wait_ms": integer(),
                # conversationの取得窓とidempotency検索。`search_since`は**必須fieldで値がnull可**
                # とし、nullは「窓の起点を置かない」を意味する（省略できる既定値ではない）
                "search_since": text(allow_none=True),
                "search_attempts": integer(),
                "search_backoff_ms": integer(),
                "search_max_pages": integer(),
                # chain検証の設定（C-06）
                "detection_head": sha(),
                "producer_logins": array(text()),
                # engineの上限値と表示名
                "max_result_bytes": integer(),
                "retry_budget": integer(),
                "speaker": text(),
                "model": text(),
                "user_speaker": text(),
                # process tree停止のgrace period（C-03）
                "halt_grace_ms": integer(),
            },
        )
    },
)
