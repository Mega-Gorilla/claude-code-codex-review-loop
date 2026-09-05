# SPDX-License-Identifier: Apache-2.0
"""engineの駆動（Phase 8 PR-3b1。ADR-0020）。

P-002は「2つのentry pointがそれぞれround orchestrationを実装すること」を禁じる。したがって
**駆動はここに1つだけ**置き、active経路もheadless経路も同じ`step`を通す。

```
step: advance -> engine側の作業（persist / halt）をこなして再度advance
              -> host側の作業（HOST_ACTION / AWAIT_USER）か終端で返す
```

`persist`と`halt`は「`advance`が返した作業の実行」であって独立した制御経路ではない
（ADR-0017 / ADR-0019）。それをcodeの形にすると、entry pointが呼ぶのは`step`と`submit`
だけになる（AC-C08-03）。

engine側の作業には**1 stepあたりの回数上限**を置く。C-01が同じ作業を返し続ける状況は
不変条件の破れであり、推測して回し続けずに停止する。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from ..state import StatePaths
from ..workflow import (
    AdvanceOutcome,
    AwaitUser,
    Blocked,
    EmergencyStopCompleted,
    EmergencyStopFailed,
    EmergencyStopRequired,
    EngineStopped,
    HaltCompleted,
    HaltFailed,
    HaltRequired,
    HostActionIssued,
    IncidentRecorded,
    IncidentRequired,
    IntegrityDetected,
    PersistFailed,
    PersistRequired,
    RecordPersisted,
    SubmitOutcome,
    Terminal,
    advance,
    emergency_stop,
    halt,
    persist,
    record_incident,
    request_emergency_stop,
    submit,
)
from .agent_session import AgentExecution, check_agent_binding, check_agent_execution
from .config import SessionConfig
from .ports import ChainNotIntactError, PortSet, PortUnavailableError
from .signals import StopSignal

# 1 stepでこなすengine側の作業の上限。C-01が同じ作業を返し続けるのは不変条件の破れなので、
# 上限へ達したら推測して回し続けずに停止する（record 1件の永続化 + 停止1回が現実的な最大）
MAX_ENGINE_WORK: Final = 8

# 次に**副作用**（GitHubへの投稿 / process停止）を起こすoutcome。上限に達したら、
# これらを実行する前に止める
SIDE_EFFECT_OUTCOMES: Final = (
    PersistRequired,
    IncidentRequired,
    HaltRequired,
    EmergencyStopRequired,
)

# host側の作業（呼び出し側が実行して`submit`で返す）
HostWork = HostActionIssued | AwaitUser
# それ以上engineだけでは進めない結果
StepOutcome = HostWork | Terminal | Blocked | EngineStopped


@dataclass(frozen=True)
class StepTrace:
    """1 stepでこなしたengine側の作業（診断とtestの観測点）。"""

    persisted: tuple[str, ...]
    halted: int
    # 緊急停止: 要求を記録した回数と、実行して完了した回数
    stop_requested: int = 0
    stopped: int = 0
    # incident recordを作った回数（`RecordIntegrityIncident`の実行）
    incidents: int = 0


@dataclass(frozen=True)
class StepResult:
    """`step`の結果と、そこへ至るまでにengineが行った作業。"""

    outcome: StepOutcome
    trace: StepTrace


def _advance(
    paths: StatePaths,
    config: SessionConfig,
    ports: PortSet,
    id_source: Callable[[], str],
    issued_at: str,
) -> AdvanceOutcome:
    return advance(
        paths=paths,
        run_id=config.run_id,
        repository=config.repository,
        number=config.number,
        head_sha=config.head_sha,
        payload_port=ports.payload,
        evidence_port=ports.evidence,
        records_port=ports.records,
        id_source=id_source,
        issued_at=issued_at,
    )


def step(
    *,
    paths: StatePaths,
    config: SessionConfig,
    ports: PortSet,
    id_source: Callable[[], str],
    issued_at: str,
    stop: StopSignal | None = None,
    execution: AgentExecution | None = None,
) -> StepResult:
    """次にhostがすべきことまで進める（engine側の作業は途中でこなす）。

    portが未実装の場合（`PortUnavailableError`）は、担当componentを名指しした
    `EngineStopped`へ写す。例外が呼び出し側へ飛び越えると、構造化outcomeで進退を決める
    という前提が壊れる。

    2回目の停止signal（`KeyboardInterrupt`）は`stop_trees`の内側とは限らない。要求を保存する
    前・保存中・台帳の読込中・`drive`のhost作業中にも届く。**どこで届いても停止を完了させる**
    のがここの責務である（ADR-0021 決定19-h）。
    """
    if stop is None:
        return _run_step(paths, config, ports, id_source, issued_at, None, execution)
    try:
        return _run_step(paths, config, ports, id_source, issued_at, stop, execution)
    except KeyboardInterrupt:
        if not stop.force_requested:
            # 停止の昇格要求ではない中断は握り潰さない
            raise
        return _forced_stop(paths, config, ports, stop, issued_at)


def _forced_stop(
    paths: StatePaths,
    config: SessionConfig,
    ports: PortSet,
    stop: StopSignal,
    issued_at: str,
) -> StepResult:
    """2回目の停止signalで中断した後に、停止を最後までやり切る。

    **`advance`を通さない**。要求が在るときの`advance`は`EmergencyStopRequired`を返すだけで、
    その手前でchain gate（GitHub取得）を通る場合がある。forceは「待たずに殺せ」という要求
    なので、local I/Oだけで完結する2手——要求をdurableにする、台帳のtreeを止める——へ絞る。

    要求の記録は冪等で、**まだ保存していない場合でもここで保存する**。保存前に中断された場合、
    台帳に停止の根拠が無いまま終了してしまうためである。
    """
    recorded = request_emergency_stop(
        paths=paths,
        run_id=config.run_id,
        repository=config.repository,
        number=config.number,
        requested_at=issued_at,
    )
    if isinstance(recorded, EngineStopped):
        return StepResult(recorded, StepTrace((), 0, 0, 0))
    stop.mark_recorded()
    requested = 0 if recorded.already_recorded else 1
    stopped = _emergency(paths, config, ports, stop)
    if isinstance(stopped, EngineStopped):
        return StepResult(stopped, StepTrace((), 0, requested, 0))
    if isinstance(stopped, EmergencyStopFailed):
        return StepResult(
            EngineStopped("emergency_stop_failed", stopped.detail),
            StepTrace((), 0, requested, 0),
        )
    return StepResult(
        EngineStopped(
            "forced_stop",
            f"2回目の停止signalで中断し、treeを停止した（state={stopped.machine_state.state.value}）",
        ),
        StepTrace((), 0, requested, 1),
    )


def _run_step(
    paths: StatePaths,
    config: SessionConfig,
    ports: PortSet,
    id_source: Callable[[], str],
    issued_at: str,
    stop: StopSignal | None,
    execution: AgentExecution | None,
) -> StepResult:
    """`step`の本体（`KeyboardInterrupt`の扱いは呼び出し側が持つ）。"""
    persisted: list[str] = []
    halted = 0
    requested = 0
    stopped_count = 0
    incidents = 0

    def trace() -> StepTrace:
        return StepTrace(tuple(persisted), halted, requested, stopped_count, incidents)

    work = 0
    while True:
        # 安全点でsignalを見る。handlerはflagを立てるだけで、要求の記録はここで行う
        # （signal contextでcheckpointを書かない。ADR-0021 決定4）。
        # 変換は`pending`が消える1回だけで、以後は台帳が停止の持ち主になる
        if stop is not None and stop.pending:
            recorded = request_emergency_stop(
                paths=paths,
                run_id=config.run_id,
                repository=config.repository,
                number=config.number,
                requested_at=issued_at,
            )
            if isinstance(recorded, EngineStopped):
                return StepResult(recorded, trace())
            stop.mark_recorded()
            if not recorded.already_recorded:
                requested += 1
        binding_error = check_agent_binding(paths, config)
        if binding_error is not None:
            # 設定破損でも記録済み緊急停止は妨げない。local停止だけ行い、通常処理へ戻らない。
            safety = _emergency(paths, config, ports, stop)
            if isinstance(safety, EmergencyStopCompleted):
                stopped_count += 1
            elif isinstance(safety, EmergencyStopFailed):
                return StepResult(EngineStopped("emergency_stop_failed", safety.detail), trace())
            elif safety.code != "no_stop_request":
                return StepResult(safety, trace())
            return StepResult(binding_error, trace())
        try:
            outcome = _advance(paths, config, ports, id_source, issued_at)
        except PortUnavailableError as error:
            return StepResult(
                EngineStopped("port_unavailable", str(error)), trace()
            )
        except ChainNotIntactError as error:
            # engineの`_chain_gate`と同じ分類にする（portが後から観測しても結果は同じ）
            return StepResult(
                EngineStopped("chain_violation", str(error)), trace()
            )
        if isinstance(outcome, SIDE_EFFECT_OUTCOMES) and work >= MAX_ENGINE_WORK:
            # 上限**ちょうど**までは実行し、次の副作用を起こす前に止める
            return StepResult(
                EngineStopped(
                    "engine_work_limit", f"1 stepのengine側作業が上限{MAX_ENGINE_WORK}回へ達した"
                ),
                trace(),
            )
        if isinstance(outcome, EmergencyStopRequired):
            halt_outcome = _emergency(paths, config, ports, stop)
            if isinstance(halt_outcome, EngineStopped):
                return StepResult(halt_outcome, trace())
            if isinstance(halt_outcome, EmergencyStopFailed):
                # 要求は台帳に残る。同じ理由で失敗し続けるためここでは回さない
                return StepResult(
                    EngineStopped("emergency_stop_failed", halt_outcome.detail), trace()
                )
            stopped_count += 1
            work += 1
            continue
        if isinstance(outcome, PersistRequired):
            stored = _persist(paths, config, ports)
            if isinstance(stored, EngineStopped):
                return StepResult(stored, trace())
            # `IntegrityDetected` / `PersistFailed`もC-01が状態を決めている。次のadvanceが
            # その状態に応じた作業（停止手続き等）を返すので、ここでは分岐しない
            persisted.append(outcome.record.binding.value)
            work += 1
            continue
        if isinstance(outcome, IncidentRequired):
            incident = _incident(paths, config, ports)
            if isinstance(incident, EngineStopped):
                return StepResult(incident, trace())
            incidents += 1
            work += 1
            continue
        if isinstance(outcome, HaltRequired):
            stopped = _halt(paths, config, ports, stop)
            if isinstance(stopped, EngineStopped):
                return StepResult(stopped, trace())
            halted += 1
            work += 1
            if isinstance(stopped, HaltFailed):
                # 停止できなかった。C-01は停止commandを再発行するが同じ理由で失敗し続けるため、
                # ここでは回さず呼び出し側へ返す（次のresumeがやり直す）
                return StepResult(
                    EngineStopped("halt_failed", stopped.detail),
                    trace(),
                )
            continue
        if isinstance(outcome, HostActionIssued):
            denied = check_agent_execution(config, execution or AgentExecution())
            if denied is not None:
                return StepResult(denied, trace())
        return StepResult(outcome, trace())


def _incident(
    paths: StatePaths, config: SessionConfig, ports: PortSet
) -> IncidentRecorded | EngineStopped:
    """`RecordIntegrityIncident`を実行する（incident recordを1件作る）。

    payload portも本文portも未実装であり得るため、`_persist` / `submit_result`と同じ
    変換境界をここへ置く。例外が`step`の外へ飛ぶと、構造化outcomeで進退を決めるという
    前提が壊れてCLIが例外終了する。
    """
    try:
        return record_incident(
            paths=paths,
            run_id=config.run_id,
            repository=config.repository,
            number=config.number,
            head_sha=config.head_sha,
            incident_port=ports.incident,
            body_port=ports.body,
            records_port=ports.records,
            speaker=config.controller_speaker,
        )
    except PortUnavailableError as error:
        return EngineStopped("port_unavailable", str(error))


def _emergency(
    paths: StatePaths, config: SessionConfig, ports: PortSet, stop: StopSignal | None
) -> EmergencyStopCompleted | EmergencyStopFailed | EngineStopped:
    return emergency_stop(
        paths=paths,
        run_id=config.run_id,
        repository=config.repository,
        number=config.number,
        stop_port=ports.stop,
        grace_seconds=config.halt_grace_seconds,
        escalation=stop,
    )


def _persist(
    paths: StatePaths, config: SessionConfig, ports: PortSet
) -> RecordPersisted | IntegrityDetected | PersistFailed | EngineStopped:
    try:
        return persist(
            paths=paths,
            run_id=config.run_id,
            repository=config.repository,
            number=config.number,
            context=config.context(),
            repo=config.repo,
            records_port=ports.records,
            event_port=ports.events,
            policy=config.policy(),
            search_since=config.search_since,
            search_attempts=config.search_attempts,
            search_backoff_seconds=config.search_backoff_seconds,
            search_max_pages=config.search_max_pages,
        )
    except PortUnavailableError as error:
        return EngineStopped("port_unavailable", str(error))


def _halt(
    paths: StatePaths, config: SessionConfig, ports: PortSet, stop: StopSignal | None
) -> HaltCompleted | HaltFailed | EngineStopped:
    return halt(
        paths=paths,
        run_id=config.run_id,
        repository=config.repository,
        number=config.number,
        stop_port=ports.stop,
        grace_seconds=config.halt_grace_seconds,
        escalation=stop,
    )


def submit_result(
    raw: bytes, *, paths: StatePaths, config: SessionConfig, ports: PortSet, accepted_at: str
) -> SubmitOutcome:
    """hostの応答をengineへ渡す（`step`と並ぶもう1つの制御経路。AC-C08-03）。"""
    denied = check_agent_binding(paths, config)
    if denied is not None:
        return denied
    try:
        return submit(
            raw,
            paths=paths,
            run_id=config.run_id,
            repository=config.repository,
            number=config.number,
            records_port=ports.records,
            body_port=ports.body,
            max_result_bytes=config.max_result_bytes,
            retry_budget=config.retry_budget,
            accepted_at=accepted_at,
            speaker=config.speaker,
            model=config.model,
            user_speaker=config.user_speaker,
        )
    except PortUnavailableError as error:
        return EngineStopped("port_unavailable", str(error))
