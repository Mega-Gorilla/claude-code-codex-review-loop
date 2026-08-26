# SPDX-License-Identifier: Apache-2.0
"""`HaltRun`の実行（Phase 8 PR-3a。ADR-0019）。

C-01が停止手続きへ入ると`HaltRun`を発行する。本moduleがそれを実行し、走っている
process treeを止めてからcheckpointを保存し、完了eventでstateを進める。

```
advance -> HaltRequired
        -> halt: tree停止 -> 完了event -> checkpoint保存
        -> CANCELLED（cancel）/ BLOCKED（integrity halt）
```

順序は`受理可否の確認 -> tree停止 -> checkpoint保存`である。

- **停止する前にC-01が完了eventを受理できるかを確かめる**。`transition`は純粋関数なので
  副作用なしに判定でき、拒否される状態でtreeを止めると「processは死んだがstateは走行中」
  という不整合が残る
- **保存より先に停止する**。先に保存して停止前に落ちると、stateだけが「停止済み」になり、
  走り続けるprocessを誰も止めない。停止してから落ちた場合は、resumeがC-01のX系列ruleで
  `HaltRun`を冪等に再発行し、停止をやり直す（`stop_tree_by_ref`は冪等）

停止対象が**無い場合も正常完了**である。C-01の横断規則は「手続き中の失敗・明示resumeは
停止commandの冪等再発行のみ」を前提にしており、tree台帳が空でも手続きは完了へ進める。

**緊急停止（Ctrl+C）はここでは扱わない**。C-01は緊急停止を`NormalProcedure`のまま完了させる
（C-05 rule）ため、停止に失敗したときの`RunFailed`はF-01で`FAILED(recovery_to)`へ進み、
`HaltRun`を**再発行しない**。つまり停止意図がcheckpointへ残らず、次のresumeが再停止しない。
durableに表現する方法（C-01のprocedure追加か、C-08側の停止要求台帳）とsignal handlerの設置は
同じPRで決める必要があるため、entry pointを持つPR-3bへ送る。
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
    Procedure,
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

    停止手続き中の`RunFailed`は`HaltRun`の冪等再発行になるので（C-01のX系列rule）、
    これは「次のresumeでやり直す」を意味する。stateは手続き中のまま変わらない。
    """

    detail: str
    machine_state: MachineState
    commands: tuple[Command, ...]


HaltOutcome = HaltCompleted | HaltFailed | EngineStopped


def completion_event_for(procedure: Procedure) -> ev.Event | None:
    """停止手続きに対応する完了event（手続き以外はNone）。

    C-08が構成し得る完了eventはこの2つだけで、いずれもC-01が当該手続きの成立するstateで
    必ず受理する（contract testが固定する）。推測でeventを作らない。
    """
    if isinstance(procedure, CancellingProcedure):
        return ev.CancellationCompleted(attempt_binding=procedure.attempt_binding)
    if isinstance(procedure, HaltingForBlockProcedure):
        return ev.BlockHaltCompleted(attempt_binding=procedure.attempt_binding)
    return None


Applied = tuple[MachineState, tuple[Command, ...]]


def _accepted(run: RunContext, event: ev.Event) -> Applied | EngineStopped:
    """C-01が受理するかを**副作用なしで**確かめる（`transition`は純粋関数）。"""
    try:
        return transition(run.machine_state, event)
    except (TransitionRejected, ev.IllegalEventError) as error:  # pragma: no cover - C-08が
        # 構成する完了eventはC-01が必ず受理する（contract testが全到達stateで固定する）。
        # C-01のruleが変わったときに、treeを止める**前に**現れるよう残す
        return EngineStopped("illegal_event", f"C-01が{type(event).__name__}を受理しない: {error}")


def _save(
    run: RunContext, applied: Applied, *, paths: StatePaths, run_id: str, refs: tuple[TreeRef, ...]
) -> Applied | EngineStopped:
    """停止済みtreeを台帳から外してcheckpointを保存する。"""
    machine_state, commands = applied
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
) -> HaltOutcome:
    """進行中の停止手続きを1回だけ完了させる。

    **C-01が完了eventを受理できることを確かめてから**treeを止める。台帳のtreeを順に停止し、
    すべて止まってから状態を進めて保存する。1つでも止められなければstateを進めず、
    `RunFailed`で停止commandの再発行だけを行う。
    """
    run = load_run(paths, run_id=run_id, repository=repository, number=number)
    if isinstance(run, EngineStopped):
        return run
    event = completion_event_for(run.machine_state.procedure)
    if event is None:
        return EngineStopped(
            "no_halt_procedure",
            f"{type(run.machine_state.procedure).__name__}に対応する停止手続きが無い",
        )
    # 副作用（tree停止）の前に受理可否を確かめる。拒否される状態で止めると
    # 「processは死んだがstateは走行中」という不整合が残る
    completion = _accepted(run, event)
    if isinstance(completion, EngineStopped):  # pragma: no cover - contract testが「C-08の作る
        # 完了eventはC-01が全到達stateで受理する」を固定している。この分岐は、その契約が
        # 崩れたときに**treeを止める前に**現れるための網である
        return completion
    refs = read_active_trees(run.payload)
    if isinstance(refs, SectionUnavailable):
        return EngineStopped("processes_unavailable", refs.detail)
    stopped: list[StopResult] = []
    for index, ref in enumerate(refs):
        try:
            stopped.append(stop_port.stop(ref, grace_seconds))
        except ProcessError as error:
            failed = _accepted(run, ev.RunFailed())
            if isinstance(failed, EngineStopped):  # pragma: no cover - 手続き中の`RunFailed`は
                # C-01のX系列ruleが必ず受理する（停止commandの冪等再発行）
                return failed
            # 止められなかったtreeは台帳へ残す（次のresumeが同じrefで再試行する）
            saved = _save(run, failed, paths=paths, run_id=run_id, refs=refs[index:])
            if isinstance(saved, EngineStopped):  # pragma: no cover - 手続き中の状態は表現できる
                return saved
            return HaltFailed(
                detail=f"process treeを停止できない: {error}",
                machine_state=saved[0],
                commands=saved[1],
            )
    applied = _save(run, completion, paths=paths, run_id=run_id, refs=())
    if isinstance(applied, EngineStopped):  # pragma: no cover - 停止完了後の状態は表現できる
        return applied
    return HaltCompleted(
        machine_state=applied[0], commands=applied[1], stopped=tuple(stopped)
    )
