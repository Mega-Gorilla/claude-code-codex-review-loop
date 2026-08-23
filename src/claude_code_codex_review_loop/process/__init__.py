# SPDX-License-Identifier: Apache-2.0
"""C-03 process abstraction（Phase 3）。

子process treeの起動・停止・timeoutをOS差分を吸収して提供する。POSIXはprocess
group（start_new_session）、WindowsはJob Object（CREATE_SUSPENDED -> assign ->
resume）で子孫を1つのtreeとして扱う。責務はspawn・wait・timeout・process tree停止に
限定し、signal handlerの設置・checkpoint保存・workflow遷移はC-08が担う。
停止機構の設計判断はADR-0005を正本とする。
"""

from .liveness import is_process_alive
from .spawn import (
    Completed,
    JobObjectRef,
    ProcessError,
    ProcessGroupRef,
    SpawnError,
    SpawnSpec,
    StopError,
    StopMethod,
    StopResult,
    TimedOut,
    TreeHandle,
    TreeRef,
    spawn_tree,
)
from .terminate import run_tree, stop_tree, stop_tree_by_ref

__all__ = [
    "Completed",
    "JobObjectRef",
    "ProcessError",
    "ProcessGroupRef",
    "SpawnError",
    "SpawnSpec",
    "StopError",
    "StopMethod",
    "StopResult",
    "TimedOut",
    "TreeHandle",
    "TreeRef",
    "is_process_alive",
    "run_tree",
    "spawn_tree",
    "stop_tree",
    "stop_tree_by_ref",
]
