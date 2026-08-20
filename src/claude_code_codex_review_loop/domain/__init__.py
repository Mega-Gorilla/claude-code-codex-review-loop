# SPDX-License-Identifier: Apache-2.0
"""C-01 domain state machine（Phase 1）。

公開APIは`initialize` / `transition`と、その入出力を構成する型。完全遷移の正本は
`machine.REGISTRY`であり、遷移表・遷移図はそこから生成される（Phase 1計画の節1）。
"""

from .machine import REGISTRY, check_registry, derive_guard_key, initialize, transition
from .states import ACTIVE_STATES, RESUMABLE_STATES, TERMINAL_STATES, State
from .values import (
    Awaiting,
    DomainError,
    IllegalMachineStateError,
    MachineState,
    RegistryIntegrityError,
    TransitionRejected,
)

__all__ = [
    "ACTIVE_STATES",
    "Awaiting",
    "DomainError",
    "IllegalMachineStateError",
    "MachineState",
    "REGISTRY",
    "RESUMABLE_STATES",
    "RegistryIntegrityError",
    "State",
    "TERMINAL_STATES",
    "TransitionRejected",
    "check_registry",
    "derive_guard_key",
    "initialize",
    "transition",
]
