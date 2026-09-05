# SPDX-License-Identifier: Apache-2.0
"""hostのinterfaceと駆動loop（Phase 8 PR-3b1。ADR-0020）。

engineから見たhostは「actionを実行してsubmit envelopeを返すもの」であり、それがactive
sessionでもheadless subprocessでも同じである。この同一性が、active / headlessの同値性
（AC-C08-04）を実装の一致ではなく**構造**で担保する。

**主経路のactive hostはこのprotocolを実装しない**。Claude sessionは我々のprocessの外に
あり、`HOST_ACTION`を返した時点で制御が一度戻るためである（AC-C08-02: subprocess化も
キー入力注入もしない）。entry pointのadvanceは`step`を1回だけ実行して結果を表示し、終了する。

`drive`を使うのは、subprocessでhostを起動するheadless経路（PR-3b2）と、同一sessionでの
複数round（AC-C08-01）を確かめるtestのfake active hostである。**同じ`drive`を両者が通る**。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ..state import StatePaths
from ..workflow import Blocked, EngineStopped, Terminal
from .agent_session import AgentExecution, check_agent_binding
from .config import SessionConfig
from .ports import PortSet
from .session import HostWork, StepOutcome, StepResult, step, submit_result
from .signals import StopSignal


class HostPort(Protocol):
    """1つのhost作業を実行し、submit envelopeを返す。

    戻り値は`SUBMIT` / `USER_SUBMIT`のいずれかのbytesで、engineが構造で判別する
    （ADR-0018 決定5）。hostはstateを決めず、resultとその宣言だけを返す。
    """

    def execute(self, work: HostWork) -> bytes: ...


@dataclass(frozen=True)
class DriveResult:
    """`drive`が止まった理由と、そこまでに回したround数。"""

    outcome: StepOutcome
    rounds: int
    submitted: tuple[str, ...]


@dataclass
class DriveClock:
    """round単位で進むID・時刻の供給源（engineは生成源を持たない）。

    実時間とUUIDはruntimeが与える。testは決定論的な値を渡せる。
    """

    id_source: Callable[[], str]
    issued_at: Callable[[], str]
    accepted_at: Callable[[], str]


def drive(
    host: HostPort,
    *,
    paths: StatePaths,
    config: SessionConfig,
    ports: PortSet,
    clock: DriveClock,
    max_rounds: int,
    stop: StopSignal | None = None,
    execution: AgentExecution | None = None,
) -> DriveResult:
    """host作業が無くなるまで`step` -> `execute` -> `submit`を繰り返す。

    **round orchestrationはここ1箇所**である（P-002）。entry pointはこれを呼ぶか、
    `step`を1回だけ呼ぶかのどちらかで、自前のloopを持たない。

    `max_rounds`は呼び出し側が決める（engineは既定値を持たない）。到達したら停止し、
    「まだhost作業がある」ことを`EngineStopped`で伝える。
    """
    submitted: list[str] = []
    rounds = 0
    while rounds < max_rounds:
        result: StepResult = step(
            paths=paths,
            config=config,
            ports=ports,
            id_source=clock.id_source,
            issued_at=clock.issued_at(),
            stop=stop,
            execution=execution,
        )
        outcome = result.outcome
        if isinstance(outcome, (Terminal, Blocked, EngineStopped)):
            return DriveResult(outcome=outcome, rounds=rounds, submitted=tuple(submitted))
        rounds += 1
        # **`step`の外側**もsignalから守る。`host.execute`（3-b3ではheadless processの起動と
        # 待機）と`submit_result`（chain取得を含む）はroundの中で最も長く、2回目の
        # `KeyboardInterrupt`はここへ落ちやすい。`step`のcatchはこの区間を覆わない
        try:
            denied = check_agent_binding(paths, config)
            if denied is not None:
                return DriveResult(denied, rounds, tuple(submitted))
            raw = host.execute(outcome)
            if stop is not None and stop.requested:
                # host作業中の1回目。戻り直後が最初の安全点になる
                return _stopped_round(
                    paths=paths, config=config, ports=ports, clock=clock, stop=stop,
                    rounds=rounds, submitted=tuple(submitted),
                )
            accepted = submit_result(
                raw,
                paths=paths,
                config=config,
                ports=ports,
                accepted_at=clock.accepted_at(),
            )
        except KeyboardInterrupt:
            if stop is None or not stop.force_requested:
                # 停止の昇格要求ではない中断は握り潰さない
                raise
            # 2回目がhost作業 / submit中に届いた。未submitの結果は捨て、停止は次の`step`が
            # 完了させる（要求のdurable化と`grace = 0`の停止）
            return _stopped_round(
                paths=paths, config=config, ports=ports, clock=clock, stop=stop,
                rounds=rounds, submitted=tuple(submitted),
            )
        if isinstance(accepted, EngineStopped):
            return DriveResult(outcome=accepted, rounds=rounds, submitted=tuple(submitted))
        submitted.append(type(accepted).__name__)
    return DriveResult(
        outcome=EngineStopped("round_limit", f"round上限{max_rounds}へ達した"),
        rounds=rounds,
        submitted=tuple(submitted),
    )


def _stopped_round(
    *,
    paths: StatePaths,
    config: SessionConfig,
    ports: PortSet,
    clock: DriveClock,
    stop: StopSignal,
    rounds: int,
    submitted: tuple[str, ...],
) -> DriveResult:
    """host作業中にsignalを受けた場合の締めくくり。

    未submitの結果は捨てる。hostへ出したactionはcheckpointに未完了として残っており、
    停止後にresumeすれば**同じactionが再提示される**（ADR-0014 決定22）。ここで結果だけを
    先に受理すると、停止要求を挟んだ状態遷移がsignalの有無で変わる。

    1回目（flagだけ）と2回目（`KeyboardInterrupt`）のどちらから来ても同じ経路を通る。
    停止要求のdurable化と`grace = 0`の停止は、ここが呼ぶ`step`が行う。
    """
    result = step(
        paths=paths,
        config=config,
        ports=ports,
        id_source=clock.id_source,
        issued_at=clock.issued_at(),
        stop=stop,
    )
    return DriveResult(outcome=result.outcome, rounds=rounds, submitted=submitted)
