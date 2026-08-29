# SPDX-License-Identifier: Apache-2.0
"""緊急停止（Ctrl+C）の要求と実行（Phase 8 PR-3b2。ADR-0021）。

C-01は緊急停止を**手続きを持たないまま**完了させる（C-05 rule: `NormalProcedure` +
`CancellationCompleted(emergency_evidence=...)` -> `CANCELLED`）。そのため停止に失敗しても
`HaltRun`は再発行されず（F-01の`command_names=()`）、停止意図がcheckpointへ残らない。

本moduleはその意図を**C-08側の台帳**（`stop_request` section）として持つ。

```
signal -> request_emergency_stop  台帳へ書く（停止より先）
       -> advance                 EmergencyStopRequired
       -> emergency_stop          受理可否の確認 -> tree停止 -> 保存
```

順序は`halt`と同じ「**受理可否の確認 -> tree停止 -> checkpoint保存**」だが、`halt`と違って
**要求の記録が停止より先**にある。`halt`は手続きが既にcheckpointへ在ることを前提にできる
のに対し、緊急停止には先行するdurable markerが無く、書く前に落ちると停止意図が消えるためである。

**停止に失敗しても`RunFailed`を入力しない**。F-01で`FAILED(recovery_to)`へ進むと、まさに
残したい停止意図が消える。stateは変えず要求を残し、次のresumeが同じ要求から再停止する。

**手続き中と終端ではC-01へeventを入力しない**。`CancellingProcedure`中の`emergency_evidence`は
binding不一致として拒否され（C-01のX3）、終端stateはC-05の対象外である。いずれもtreeだけ
止めて要求を消し、stateは既存経路（procedureの`HaltRun` / terminal）へ委ねる。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..domain import events as ev
from ..domain.commands import Command
from ..domain.machine import transition
from ..domain.states import TERMINAL_STATES
from ..domain.values import (
    MachineState,
    NormalProcedure,
    OpaqueRef,
    TransitionRejected,
)
from ..process import StopResult
from ..state import StatePaths, checkpoint_path, save_checkpoint
from .checkpoint_view import (
    SectionUnavailable,
    StopRequest,
    read_active_trees,
    read_stop_request,
    with_active_trees,
    with_stop_request,
    with_verified_machine_state,
)
from .halt import TreeStopFailed, stop_trees
from .ports import ProcessStopPort, StopEscalation
from .run_context import EngineStopped, RunContext, load_run

# 緊急停止evidenceのprefix（violation bindingやintent keyと同じく、値の由来を読めるようにする）
EVIDENCE_PREFIX = "es:"


@dataclass(frozen=True)
class EmergencyStopRequested:
    """停止要求をcheckpointへ記録した（treeの停止はまだ行っていない）。"""

    request: StopRequest
    already_recorded: bool


@dataclass(frozen=True)
class EmergencyStopCompleted:
    """treeを停止して要求を消した。

    `cancelled`はC-01へ完了eventを入力したか。手続き中と終端ではFalseで、stateは変わらない。
    """

    machine_state: MachineState
    commands: tuple[Command, ...]
    stopped: tuple[StopResult, ...]
    cancelled: bool


@dataclass(frozen=True)
class EmergencyStopFailed:
    """treeを停止できなかった。要求は残り、stateは変わらない。

    次のresumeが同じ要求から再停止する（`stop_tree_by_ref`は冪等）。
    """

    detail: str


RequestOutcome = EmergencyStopRequested | EngineStopped
StopOutcome = EmergencyStopCompleted | EmergencyStopFailed | EngineStopped


def stop_evidence(*, run_id: str, repository: str, number: int, requested_at: str) -> OpaqueRef:
    """緊急停止evidenceの決定論的な導出（run / checkpointへのbind）。

    C-01は`emergency_evidence`の**存在**だけを見る（`_derive_binding`）。値の正当性は
    C-08が担保するので、runと要求時刻から決まる値にして推測の余地を残さない。区切り文字を
    含むopaque値でも衝突しないよう、sorted keysのcompact JSONから導出する
    （`intent_key`と同じ方式）。
    """
    payload = {
        "at": requested_at,
        "number": number,
        "repository": repository,
        "run": run_id,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return OpaqueRef(EVIDENCE_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def request_emergency_stop(
    *, paths: StatePaths, run_id: str, repository: str, number: int, requested_at: str
) -> RequestOutcome:
    """緊急停止の意図をcheckpointへ記録する（**停止より先**）。

    既に要求が在る場合は**上書きしない**。evidenceと要求時刻が変わると、同じ要求が別の
    eventになり冪等性が壊れる。
    """
    run = load_run(paths, run_id=run_id, repository=repository, number=number)
    if isinstance(run, EngineStopped):
        return run
    existing = read_stop_request(run.payload)
    if isinstance(existing, SectionUnavailable):
        return EngineStopped("stop_request_unavailable", existing.detail)
    if existing is not None:
        return EmergencyStopRequested(request=existing, already_recorded=True)
    request = StopRequest(
        requested_at=requested_at,
        evidence=stop_evidence(
            run_id=run_id, repository=repository, number=number, requested_at=requested_at
        ),
        source_state=run.machine_state.state,
    )
    save_checkpoint(checkpoint_path(paths, run_id), with_stop_request(run.payload, request))
    return EmergencyStopRequested(request=request, already_recorded=False)


Applied = tuple[MachineState, tuple[Command, ...]]


def _completion(run: RunContext, request: StopRequest) -> Applied | None | EngineStopped:
    """完了eventを適用した結果（入力しない場合はNone）。

    **副作用の前に**確かめる。`transition`は純粋関数なので、tree停止より先に判定できる。
    """
    machine_state = run.machine_state
    if machine_state.state in TERMINAL_STATES:
        return None
    if not isinstance(machine_state.procedure, NormalProcedure):
        # 手続き中の停止はその手続きが完了させる。ここで別のeventを入力しない
        return None
    event = ev.CancellationCompleted(emergency_evidence=request.evidence)
    try:
        return transition(machine_state, event)
    except (TransitionRejected, ev.IllegalEventError) as error:  # pragma: no cover - C-05は
        # 非terminal・NormalProcedure・evidence有りを常に受理する（contract testが固定する）。
        # C-01のruleが変わったときに、treeを止める**前に**現れるよう残す
        return EngineStopped("illegal_event", f"C-01が緊急停止の完了を受理しない: {error}")


def emergency_stop(
    *,
    paths: StatePaths,
    run_id: str,
    repository: str,
    number: int,
    stop_port: ProcessStopPort,
    grace_seconds: float,
    escalation: StopEscalation | None = None,
) -> StopOutcome:
    """記録済みの緊急停止要求を1回だけ実行する。"""
    run = load_run(paths, run_id=run_id, repository=repository, number=number)
    if isinstance(run, EngineStopped):
        return run
    request = read_stop_request(run.payload)
    if isinstance(request, SectionUnavailable):
        return EngineStopped("stop_request_unavailable", request.detail)
    if request is None:
        return EngineStopped("no_stop_request", "記録された緊急停止要求が無い")
    applied = _completion(run, request)
    if isinstance(applied, EngineStopped):  # pragma: no cover - `_completion`のEngineStoppedは
        # C-01のruleが変わったときだけ現れる（同関数のpragmaと対）
        return applied
    refs = read_active_trees(run.payload)
    if isinstance(refs, SectionUnavailable):
        return EngineStopped("processes_unavailable", refs.detail)
    outcome = stop_trees(
        refs, stop_port=stop_port, grace_seconds=grace_seconds, escalation=escalation
    )
    if isinstance(outcome, TreeStopFailed):
        # stateを進めず要求も消さない。**保存もしない**（残す値が現在のcheckpointと同じ）
        return EmergencyStopFailed(detail=outcome.detail)
    machine_state = run.machine_state if applied is None else applied[0]
    commands = () if applied is None else applied[1]
    payload = with_verified_machine_state(run.payload, machine_state)
    if isinstance(payload, SectionUnavailable):  # pragma: no cover - 停止完了後の状態は
        # CANCELLED・手続き中・終端のいずれかで、すべて表現できる（`halt`の同名codeと同じ扱い）
        return EngineStopped("state_not_persistable", payload.detail)
    save_checkpoint(
        checkpoint_path(paths, run_id), with_stop_request(with_active_trees(payload, []), None)
    )
    return EmergencyStopCompleted(
        machine_state=machine_state,
        commands=commands,
        stopped=outcome.results,
        cancelled=applied is not None,
    )
