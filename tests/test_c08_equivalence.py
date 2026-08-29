# SPDX-License-Identifier: Apache-2.0
"""AC-C08-04 / MVP-06: active経路とheadless経路の同値性（Phase 8 PR-3b3。ADR-0022）。

`HostPort`が同一interfaceであることの**意味**は、engineから見た振る舞いが一致することである。
そこで同じシナリオを2つのhost実装で回し、次の2つが完全に一致することを主張する。

| 比較対象 | 取り方 |
| --- | --- |
| state遷移列 | `step` / `submit_result`の各呼び出し後にcheckpointの`MachineState.state`を読む |
| canonical record列 | fake GitHubのchainをseq昇順で読む |

driverは**同じloop**である（`_run_scenario`）。違うのは`HostPort`の実装だけで、engine側は
どちらの経路でも同じcodeを通る。これが「実装の一致ではなく構造で担保する」ということである。

**実Claudeは起動しない**。headless側が起動するのはtestが生成したfake host（Python script）で、
境界が実行fileなのでspawn・待機・stdout回収・`processes`台帳・redactionは製品codeが走る。
"""

from __future__ import annotations

import sys
from pathlib import Path

from c07_support.helpers import RUN
from c08_support.headless import action_entry, host_env, user_entry, write_fake_host, write_plan
from c08_support.helpers import gate_answer_payload, user_machine_state
from c08_support.runtime import (
    ISSUED_AT,
    FakeActiveHost,
    FakeIds,
    RuntimeEnv,
    gate_host,
    round_ports,
    runtime_env,
)

from claude_code_codex_review_loop.domain.values import Awaiting, RecordKind, State
from claude_code_codex_review_loop.runtime import HeadlessHost, PortSet, step, submit_result
from claude_code_codex_review_loop.state import CheckpointLoaded, checkpoint_path, load_checkpoint
from claude_code_codex_review_loop.workflow import (
    AwaitUser,
    EngineStopped,
    HostActionIssued,
    Terminal,
    read_machine_state,
)

ACCEPTED_AT = "2026-08-26T09:05:00Z"
MAX_ROUNDS = 6

# 代表シナリオ（PR-3b1のR-04と同じ）: gate質問 -> 回答 -> cancel -> CANCELLED
SCENARIO = (
    user_entry(RecordKind.GATE_QUESTION),
    action_entry(RecordKind.GATE_ANSWER, gate_answer_payload()),
    user_entry(RecordKind.USER_CANCEL),
)


class Observation:
    """1 runの観測（state遷移列とcanonical record列）。"""

    def __init__(self) -> None:
        self.states: list[str] = []
        self.records: list[str] = []
        self.rounds = 0

    def as_tuple(self) -> tuple[tuple[str, ...], tuple[str, ...], int]:
        return tuple(self.states), tuple(self.records), self.rounds


def _state_of(env: RuntimeEnv) -> str:
    loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
    assert isinstance(loaded, CheckpointLoaded), loaded
    return read_machine_state(loaded.payload).state.value


def _records_of(env: RuntimeEnv, ports: PortSet) -> list[str]:
    chain = ports.records.chain(RUN)
    assert chain.is_intact, chain.violations
    return [record.kind.value for record in sorted(chain.records, key=lambda item: item.seq)]


def _run_scenario(env: RuntimeEnv, ports: PortSet, host: object) -> Observation:
    """**両経路が通る唯一のloop**。違うのは`host`の実装だけである。"""
    seen = Observation()
    for index in range(MAX_ROUNDS):
        result = step(
            paths=env.paths,
            config=env.config,
            ports=ports,
            id_source=FakeIds(f"r{index}"),
            issued_at=ISSUED_AT,
        )
        seen.states.append(_state_of(env))
        outcome = result.outcome
        if isinstance(outcome, Terminal):
            seen.records = _records_of(env, ports)
            return seen
        assert isinstance(outcome, (AwaitUser, HostActionIssued)), outcome
        raw = host.execute(outcome)  # type: ignore[attr-defined]
        accepted = submit_result(
            raw, paths=env.paths, config=env.config, ports=ports, accepted_at=ACCEPTED_AT
        )
        assert not isinstance(accepted, EngineStopped), accepted
        seen.states.append(_state_of(env))
        seen.rounds += 1
    raise AssertionError("シナリオが終端へ到達しなかった")


def _gate_env(tmp_path: Path, name: str) -> RuntimeEnv:
    return runtime_env(
        tmp_path / name,
        state=user_machine_state(Awaiting.USER_INPUT_GATE),
        seeded=(RecordKind.FINAL_REPORT,),
    )


def _active(tmp_path: Path) -> tuple[Observation, FakeActiveHost]:
    env = _gate_env(tmp_path, "active")
    host = gate_host(env)
    return _run_scenario(env, round_ports(env), host), host


def _headless(tmp_path: Path) -> tuple[Observation, Path]:
    root = tmp_path / "headless"
    env = _gate_env(tmp_path, "headless")
    script = write_fake_host(root)
    plan = write_plan(root, SCENARIO)
    state = root / "fake-host-state.json"
    host = HeadlessHost(
        paths=env.paths,
        run_id=RUN,
        command=(sys.executable, str(script)),
        workdir=root,
        env=host_env(plan, state),
        timeout_seconds=60.0,
        grace_seconds=1.0,
    )
    return _run_scenario(env, round_ports(env), host), state


def test_both_paths_produce_the_same_states_and_records(tmp_path: Path) -> None:
    """**AC-C08-04**: 同一シナリオでstate遷移列とcanonical record列が一致する。"""
    active, _ = _active(tmp_path)
    headless, _ = _headless(tmp_path)

    assert active.as_tuple() == headless.as_tuple()
    # 観測が空でないこと（一致の主張が意味を持つ前提）
    assert active.rounds == 3
    assert active.states[-1] == State.CANCELLED.value
    assert active.records == [
        RecordKind.FINAL_REPORT.value,
        RecordKind.GATE_QUESTION.value,
        RecordKind.GATE_ANSWER.value,
        RecordKind.USER_CANCEL.value,
    ]


def test_the_active_path_starts_no_process(tmp_path: Path) -> None:
    """**AC-C08-02の対比**: 同じ結果を、主経路はprocessを1つも起動せずに出す。"""
    active, host = _active(tmp_path)
    assert active.rounds == 3
    assert host.spawned == 0
    assert host.key_injections == 0


def test_the_headless_path_runs_one_process_per_round(tmp_path: Path) -> None:
    """headless側は1 roundにつき1つの子processを起動して同じ結果へ至る。"""
    import json

    headless, state = _headless(tmp_path)
    assert headless.rounds == 3
    # 子が自分で消費順を進めた回数 = 起動回数
    assert json.loads(state.read_text(encoding="utf-8"))["consumed"] == 3
