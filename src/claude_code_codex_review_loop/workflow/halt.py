# SPDX-License-Identifier: Apache-2.0
"""`HaltRun`の実行（Phase 8 PR-3a。ADR-0019）。

C-01が停止手続きへ入ると`HaltRun`を発行する。本moduleがそれを実行し、走っている
process treeを止めてからcheckpointを保存し、完了eventでstateを進める。

```
advance -> HaltRequired
        -> halt: tree停止 -> 完了event -> checkpoint保存
        -> CANCELLED（cancel）/ BLOCKED（integrity halt）
```

順序（**停止してから保存する**）は逆にできない。先に保存して停止前に落ちると、stateだけが
「停止済み」になり、走り続けるprocessを誰も止めない。停止してから落ちた場合は、resumeが
C-01のX系列ruleで`HaltRun`を冪等に再発行し、停止をやり直す（`stop_tree_by_ref`は冪等）。

停止対象が**無い場合も正常完了**である。C-01の横断規則は「手続き中の失敗・明示resumeは
停止commandの冪等再発行のみ」を前提にしており、tree台帳が空でも手続きは完了へ進める。

signal handlerの設置（Ctrl+C）はentry pointの責務で、本moduleは緊急停止の**状態境界**
だけを提供する（`emergency_evidence`）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import events as ev
from ..domain.commands import Command
from ..domain.machine import transition
from ..domain.values import (
    CancellingProcedure,
    HaltingForBlockProcedure,
    MachineState,
    NormalProcedure,
    OpaqueRef,
    TransitionRejected,
)
from ..process import ProcessError, StopResult, TreeRef
from ..state import StatePaths, checkpoint_path, save_checkpoint
from .checkpoint_view import (
    SectionUnavailable,
    read_active_trees,
    with_active_trees,
    with_verified_machine_state,
)
from .ports import ProcessStopPort
from .run_context import EngineStopped, RunContext, load_run


@dataclass(frozen=True)
class HaltCompleted:
    """process treeを停止し、完了eventでstateを進めた。"""

    machine_state: MachineState
    commands: tuple[Command, ...]
    stopped: tuple[StopResult, ...]


@dataclass(frozen=True)
class HaltFailed:
    """停止に失敗したため`RunFailed`をC-01へ入力した。

    手続き中の`RunFailed`は`HaltRun`の冪等再発行になるので（C-01のX系列rule）、
    これは「次のresumeでやり直す」を意味する。stateは手続き中のまま変わらない。
    """

    detail: str
    machine_state: MachineState
    commands: tuple[Command, ...]


HaltOutcome = HaltCompleted | HaltFailed | EngineStopped


def _completion_event(
    run: RunContext, emergency_evidence: str | None
) -> ev.Event | EngineStopped:
    """手続きから完了eventを決める（推測しない）。"""
    procedure = run.machine_state.procedure
    if isinstance(procedure, CancellingProcedure):
        return ev.CancellationCompleted(attempt_binding=procedure.attempt_binding)
    if isinstance(procedure, HaltingForBlockProcedure):
        return ev.BlockHaltCompleted(attempt_binding=procedure.attempt_binding)
    if isinstance(procedure, NormalProcedure) and emergency_evidence is not None:
        # 緊急停止（Ctrl+C等）。run / checkpointへのbindを検証した根拠は呼び出し側が渡す
        return ev.CancellationCompleted(emergency_evidence=OpaqueRef(emergency_evidence))
    return EngineStopped(
        "no_halt_procedure", f"{type(procedure).__name__}に対応する停止手続きが無い"
    )


def _apply(
    run: RunContext, event: ev.Event, *, paths: StatePaths, run_id: str, refs: tuple[TreeRef, ...]
) -> tuple[MachineState, tuple[Command, ...]] | EngineStopped:
    """C-01へeventを入力し、停止済みtreeを台帳から外してcheckpointを保存する。"""
    try:
        machine_state, commands = transition(run.machine_state, event)
    except (TransitionRejected, ev.IllegalEventError) as error:
        return EngineStopped("illegal_event", f"C-01が{type(event).__name__}を受理しない: {error}")
    payload = with_verified_machine_state(run.payload, machine_state)
    if isinstance(payload, SectionUnavailable):  # pragma: no cover - 停止完了後の状態は
        # CANCELLED / BLOCKED / 手続き中のいずれかで、すべて表現できる。表現範囲を広げる
        # Phaseがここを踏むまで防御として残す（`persist`の同名codeと同じ扱い）
        return EngineStopped("state_not_persistable", payload.detail)
    save_checkpoint(checkpoint_path(paths, run_id), with_active_trees(payload, refs))
    return machine_state, commands


def halt(
    *,
    paths: StatePaths,
    run_id: str,
    repository: str,
    number: int,
    stop_port: ProcessStopPort,
    grace_seconds: float,
    emergency_evidence: str | None = None,
) -> HaltOutcome:
    """進行中の停止手続きを1回だけ完了させる。

    台帳のtreeを順に停止し、**すべて止まってから**完了eventを入力する。1つでも止められ
    なければstateを進めず、`RunFailed`で停止commandの再発行だけを行う。
    """
    run = load_run(paths, run_id=run_id, repository=repository, number=number)
    if isinstance(run, EngineStopped):
        return run
    event = _completion_event(run, emergency_evidence)
    if isinstance(event, EngineStopped):
        return event
    refs = read_active_trees(run.payload)
    if isinstance(refs, SectionUnavailable):
        return EngineStopped("processes_unavailable", refs.detail)
    stopped: list[StopResult] = []
    for index, ref in enumerate(refs):
        try:
            stopped.append(stop_port.stop(ref, grace_seconds))
        except ProcessError as error:
            # 止められなかったtreeは台帳へ残す（次のresumeが同じrefで再試行する）
            failed = _apply(run, ev.RunFailed(), paths=paths, run_id=run_id, refs=refs[index:])
            if isinstance(failed, EngineStopped):  # pragma: no cover - 手続き中の`RunFailed`は
                # C-01のX系列ruleが必ず受理する（停止commandの冪等再発行）
                return failed
            return HaltFailed(
                detail=f"process treeを停止できない: {error}",
                machine_state=failed[0],
                commands=failed[1],
            )
    applied = _apply(run, event, paths=paths, run_id=run_id, refs=())
    if isinstance(applied, EngineStopped):
        return applied
    return HaltCompleted(
        machine_state=applied[0], commands=applied[1], stopped=tuple(stopped)
    )

