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
from .config import SessionConfig
from .ports import PortSet
from .session import HostWork, StepOutcome, StepResult, step, submit_result


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
        )
        outcome = result.outcome
        if isinstance(outcome, (Terminal, Blocked, EngineStopped)):
            return DriveResult(outcome=outcome, rounds=rounds, submitted=tuple(submitted))
        rounds += 1
        accepted = submit_result(
            host.execute(outcome),
            paths=paths,
            config=config,
            ports=ports,
            accepted_at=clock.accepted_at(),
        )
        if isinstance(accepted, EngineStopped):
            return DriveResult(outcome=accepted, rounds=rounds, submitted=tuple(submitted))
        submitted.append(type(accepted).__name__)
    return DriveResult(
        outcome=EngineStopped("round_limit", f"round上限{max_rounds}へ達した"),
        rounds=rounds,
        submitted=tuple(submitted),
    )
