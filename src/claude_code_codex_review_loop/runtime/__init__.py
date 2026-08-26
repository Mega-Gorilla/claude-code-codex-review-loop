# SPDX-License-Identifier: Apache-2.0
"""C-08 runtime: engineを駆動するprocess境界（Phase 8 PR-3b1。ADR-0020）。

engineは「次に何をすべきか」を返すstep engineで、それを呼ぶ側が本packageである。

- `session`: **単一のstep driver**（P-002）。`advance`が返したengine側の作業
  （`PersistRecord` / `HaltRun`）をこなし、host側の作業か終端まで進める
- `host`: `HostPort` protocol。active経路とheadless経路がengineから見て同一interfaceになる
- `config`: run directory内のsession config。**別processが同じportを再構成する**ために要る
- `ports`: 今日導出できるportの実装。未実装のものはC-10 / C-11を名指してfail closedにする
- `__main__`: process entry point（案B）。持つのはP-002の3責務
  （引数解析、session boundaryの受け渡し、表示）だけで、orchestrationを持たない
"""

from .config import (
    CONFIG_FILE,
    SESSION_CONFIG_VERSION,
    ConfigUnavailable,
    SessionConfig,
    config_path,
    read_session_config,
    write_session_config,
)
from .host import DriveClock, DriveResult, HostPort, drive
from .ports import (
    ChainEvidence,
    ChainNotIntactError,
    ChainRecords,
    PortSet,
    PortUnavailableError,
    RegistryRecordEvents,
    TreeStopper,
    UnavailableActionPayload,
    UserInputBody,
    default_ports,
)
from .session import (
    MAX_ENGINE_WORK,
    HostWork,
    StepOutcome,
    StepResult,
    StepTrace,
    step,
    submit_result,
)

__all__ = [
    "CONFIG_FILE",
    "MAX_ENGINE_WORK",
    "SESSION_CONFIG_VERSION",
    "ChainEvidence",
    "ChainNotIntactError",
    "ChainRecords",
    "ConfigUnavailable",
    "DriveClock",
    "DriveResult",
    "HostPort",
    "HostWork",
    "PortSet",
    "PortUnavailableError",
    "RegistryRecordEvents",
    "SessionConfig",
    "StepOutcome",
    "StepResult",
    "StepTrace",
    "TreeStopper",
    "UnavailableActionPayload",
    "UserInputBody",
    "config_path",
    "default_ports",
    "drive",
    "read_session_config",
    "step",
    "submit_result",
    "write_session_config",
]
