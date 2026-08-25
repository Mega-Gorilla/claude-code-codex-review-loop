# SPDX-License-Identifier: Apache-2.0
"""runのload経路と停止outcome（Phase 8。ADR-0015 / ADR-0017）。

`advance` / `submit`（`engine`）と`persist`（`persistence`）は、いずれも「checkpointを読み、
runの同一性を確かめ、MachineStateを復元する」ところから始まる。private名をmodule間で
importする形を作らないため、共有部分をここへ置く。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..domain.values import MachineState
from ..state import CheckpointLoaded, StatePaths, checkpoint_path, load_checkpoint, run_directory
from .checkpoint_view import SectionUnavailable, read_machine_state


@dataclass(frozen=True)
class EngineStopped:
    """進められない（推測して進めない）。codeは診断とtestの安定した識別子。"""

    code: str
    detail: str


@dataclass(frozen=True)
class RunContext:
    """checkpointと、そこから復元した状態。"""

    payload: dict[str, object]
    machine_state: MachineState
    run_dir: Path


def load_run(
    paths: StatePaths, *, run_id: str, repository: str, number: int
) -> RunContext | EngineStopped:
    """checkpointを読み、runの同一性とMachineStateの復元まで済ませる。"""
    result = load_checkpoint(checkpoint_path(paths, run_id))
    if not isinstance(result, CheckpointLoaded):
        return EngineStopped("checkpoint_unavailable", f"checkpointを読めない: {type(result).__name__}")
    payload = result.payload
    if payload.get("run_id") != run_id:
        return EngineStopped("run_mismatch", "checkpointのrun IDが一致しない")
    if payload.get("repository") != repository or payload.get("number") != number:
        return EngineStopped("target_mismatch", "checkpointのrepository / 番号が一致しない")
    machine_state = read_machine_state(payload)
    if isinstance(machine_state, SectionUnavailable):
        return EngineStopped("state_unavailable", machine_state.detail)
    return RunContext(
        payload=payload, machine_state=machine_state, run_dir=run_directory(paths, run_id)
    )
