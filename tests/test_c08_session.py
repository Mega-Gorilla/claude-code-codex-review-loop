# SPDX-License-Identifier: Apache-2.0
"""step driverの受入test（Phase 8 PR-3b1。ADR-0020）。

P-002は「entry pointごとにround orchestrationを実装すること」を禁じる。ここでは駆動が
`step`と`drive`の2つだけであること、engine側の作業（persist / halt）が`step`の内側で
こなされること、進めない場合に**推測せず構造化outcomeで止まる**ことを固定する。
"""

from __future__ import annotations

import dataclasses

import pytest
from c07_support.helpers import verified_chain
from c08_support.helpers import (
    FakeStopPort,
    cancelling,
    fix_result_payload,
    job_object_ref,
    machine_state,
    user_machine_state,
)
from c08_support.runtime import (
    ISSUED_AT,
    FakeActionPayloads,
    FakeActiveHost,
    FakeIds,
    RuntimeEnv,
    fixed_clock,
    gate_host,
    round_ports,
    runtime_env,
)

from claude_code_codex_review_loop.domain.values import (
    Awaiting,
    MachineState,
    OpaqueBinding,
    PendingRecord,
    RecordKind,
    State,
)
from claude_code_codex_review_loop.runtime import (
    MAX_ENGINE_WORK,
    DriveResult,
    StepResult,
    drive,
    step,
    submit_result,
)
from claude_code_codex_review_loop.workflow import (
    AwaitUser,
    EngineStopped,
    HostActionIssued,
    PersistRequired,
    RecordPersisted,
    Terminal,
    with_active_trees,
)

GATE_STATE = user_machine_state(Awaiting.USER_INPUT_GATE)


def _step(env: RuntimeEnv, ports: object | None = None, prefix: str = "rid") -> StepResult:
    return step(
        paths=env.paths,
        config=env.config,
        ports=ports if ports is not None else env.ports(),  # type: ignore[arg-type]
        id_source=FakeIds(prefix),
        issued_at=ISSUED_AT,
    )


def _gate_env(tmp_path, **kwargs: object) -> RuntimeEnv:
    return runtime_env(
        tmp_path, state=GATE_STATE, seeded=(RecordKind.FINAL_REPORT,), **kwargs  # type: ignore[arg-type]
    )


class TestStep:
    def test_host_work_is_returned_without_engine_work(self, tmp_path) -> None:
        """engine側の作業が無いときはそのままhost作業を返す。"""
        env = _gate_env(tmp_path)
        result = _step(env)
        assert isinstance(result.outcome, AwaitUser)
        assert result.outcome.awaiting is Awaiting.USER_INPUT_GATE
        assert result.trace.persisted == ()
        assert result.trace.halted == 0

    def test_engine_work_is_done_inside_the_step(self, tmp_path) -> None:
        """cancel受理後の`step`は、**永続化と停止を自分でこなして**終端まで進む。

        entry pointが`persist` / `halt`を個別に呼ぶ必要が無いこと（AC-C08-03）の実体である。
        """
        env = _gate_env(tmp_path)
        host = gate_host(env)
        host.user_kinds = (RecordKind.USER_CANCEL,)
        issued = _step(env)
        assert isinstance(issued.outcome, AwaitUser)
        accepted = submit_result(
            host.execute(issued.outcome),
            paths=env.paths,
            config=env.config,
            ports=env.ports(),
            accepted_at="2026-08-26T09:05:00Z",
        )
        assert not isinstance(accepted, EngineStopped), accepted

        result = _step(env, prefix="second")
        assert result.outcome == Terminal(state=State.CANCELLED)
        assert len(result.trace.persisted) == 1
        assert result.trace.halted == 1

    def test_a_missing_port_names_the_owning_component(self, tmp_path) -> None:
        """portの例外は呼び出し側へ飛ばさず、構造化outcomeへ写す。"""
        env = runtime_env(tmp_path, state=machine_state(), seeded=(RecordKind.REVIEW_RESULT,))
        result = _step(env)
        assert isinstance(result.outcome, EngineStopped)
        assert result.outcome.code == "port_unavailable"
        assert "C-10" in result.outcome.detail

    def test_an_unreadable_tree_ledger_stops_before_the_halt(self, tmp_path) -> None:
        """停止対象を推測しない（`halt`が返した停止理由をそのまま返す）。"""
        env = runtime_env(
            tmp_path,
            state=cancelling(),
            # job_nameの無いJOB_OBJECT。schemaはkindごとの必須を表せないため、欠落は
            # `read_active_trees`が検出する（pgidで代用すると別treeへ到達し得る）
            extra={"processes": {"trees": [{"kind": "JOB_OBJECT", "pid": 4242}]}},
        )
        result = _step(env)
        assert isinstance(result.outcome, EngineStopped)
        assert result.outcome.code == "processes_unavailable"
        assert result.trace.halted == 0

    def test_a_failed_halt_is_not_retried_in_the_same_step(self, tmp_path) -> None:
        """停止に失敗したら同じstepで回さない（同じ理由で失敗し続けるため）。"""
        ref = job_object_ref()
        env = runtime_env(tmp_path, state=cancelling(), extra=with_active_trees({}, [ref]))
        ports = dataclasses.replace(env.ports(), stop=FakeStopPort(fails=frozenset({ref})))
        result = _step(env, ports)
        assert isinstance(result.outcome, EngineStopped)
        assert result.outcome.code == "halt_failed"
        assert result.trace.halted == 1

    def test_a_resume_retries_the_stop_and_finishes(self, tmp_path) -> None:
        """停止意図はcheckpointに残り、次の`step`が停止をやり直して終端へ進む。"""
        ref = job_object_ref()
        env = runtime_env(tmp_path, state=cancelling(), extra=with_active_trees({}, [ref]))
        failing = dataclasses.replace(env.ports(), stop=FakeStopPort(fails=frozenset({ref})))
        assert isinstance(_step(env, failing).outcome, EngineStopped)

        working = FakeStopPort()
        result = _step(env, dataclasses.replace(env.ports(), stop=working))
        assert result.outcome == Terminal(state=State.CANCELLED)
        assert [call[0] for call in working.calls] == [ref]

    def test_a_persist_that_needs_a_missing_port_stops(self, tmp_path) -> None:
        """永続化の途中でportが無ければ止まる（`FIX_RESULT`のeventはC-10 / C-11が作る）。"""
        env = runtime_env(tmp_path, state=machine_state(), seeded=(RecordKind.REVIEW_RESULT,))
        ports = round_ports(
            env, payload=FakeActionPayloads({"APPLY_FINDINGS": {"round": 1, "finding_ids": ["F-1"]}})
        )
        host = FakeActiveHost(
            env=env,
            action_results={"APPLY_FINDINGS": (RecordKind.FIX_RESULT, fix_result_payload())},
        )
        issued = _step(env, ports)
        assert isinstance(issued.outcome, HostActionIssued)
        submit_result(
            host.execute(issued.outcome),
            paths=env.paths,
            config=env.config,
            ports=ports,
            accepted_at="2026-08-26T09:05:00Z",
        )

        result = _step(env, ports, prefix="persist")
        assert isinstance(result.outcome, EngineStopped)
        assert result.outcome.code == "port_unavailable"
        assert "C-10" in result.outcome.detail
        assert result.trace.persisted == ()

    def test_repeated_engine_work_stops_at_the_limit(self, tmp_path, monkeypatch) -> None:
        """C-01が同じ作業を返し続けるのは不変条件の破れなので、推測して回し続けない。"""
        from claude_code_codex_review_loop.runtime import session as session_module

        pending = PersistRequired(
            record=PendingRecord(
                kind=RecordKind.FINAL_REPORT,
                binding=OpaqueBinding("cr:run-1:1:final"),
                source_state=State.READY_FOR_HUMAN_MERGE,
            )
        )
        record = verified_chain([RecordKind.FINAL_REPORT]).records[0]
        stored = RecordPersisted(
            record=record, machine_state=GATE_STATE, commands=(), posted=True
        )
        monkeypatch.setattr(session_module, "advance", lambda **kwargs: pending)
        monkeypatch.setattr(session_module, "persist", lambda **kwargs: stored)

        env = _gate_env(tmp_path)
        result = _step(env)
        assert isinstance(result.outcome, EngineStopped)
        assert result.outcome.code == "engine_work_limit"
        # 上限**ちょうど**まで実行し、次の副作用を起こす前に止める（超過しない）
        assert len(result.trace.persisted) == MAX_ENGINE_WORK


class TestSubmitResult:
    def test_a_missing_body_port_stops_instead_of_raising(self, tmp_path) -> None:
        """`MERGE_APPROVAL`の本文表現はC-13が持つ。無い表現をC-08が作らない。"""
        env = _gate_env(tmp_path)
        host = gate_host(env)
        host.user_kinds = (RecordKind.MERGE_APPROVAL,)
        issued = _step(env)
        assert isinstance(issued.outcome, AwaitUser)
        outcome = submit_result(
            host.execute(issued.outcome),
            paths=env.paths,
            config=env.config,
            ports=env.ports(),
            accepted_at="2026-08-26T09:05:00Z",
        )
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "port_unavailable"
        assert "C-13" in outcome.detail


class TestDrive:
    def _drive(self, env: RuntimeEnv, host: object, *, max_rounds: int = 6) -> DriveResult:
        return drive(
            host,  # type: ignore[arg-type]
            paths=env.paths,
            config=env.config,
            ports=round_ports(env),
            clock=fixed_clock(),
            max_rounds=max_rounds,
        )

    def test_rounds_run_until_the_run_ends(self, tmp_path) -> None:
        """質問 -> 回答 -> cancelの3 roundを回して終端へ達する。"""
        env = _gate_env(tmp_path)
        host = gate_host(env)
        result = self._drive(env, host)
        assert result.outcome == Terminal(state=State.CANCELLED)
        assert result.rounds == 3
        assert result.submitted == ("UserInputAccepted", "SubmitAccepted", "UserInputAccepted")
        assert host.executed == [
            "user:GATE_QUESTION",
            "action:ANSWER_GATE_QUESTION",
            "user:USER_CANCEL",
        ]

    def test_the_round_limit_is_the_callers_decision(self, tmp_path) -> None:
        """上限はengineが既定値を持たず、呼び出し側が渡す（C-12の領域を侵さない）。"""
        env = _gate_env(tmp_path)
        result = self._drive(env, gate_host(env), max_rounds=1)
        assert isinstance(result.outcome, EngineStopped)
        assert result.outcome.code == "round_limit"
        assert result.rounds == 1

    def test_no_round_is_started_when_the_limit_is_zero(self, tmp_path) -> None:
        env = _gate_env(tmp_path)
        host = gate_host(env)
        result = self._drive(env, host, max_rounds=0)
        assert isinstance(result.outcome, EngineStopped)
        assert result.rounds == 0
        assert host.executed == []

    def test_a_rejected_submit_stops_the_loop(self, tmp_path) -> None:
        """submitが止まったらroundを進めない（推測して次のactionを出さない）。"""
        env = _gate_env(tmp_path)
        host = gate_host(env)
        host.user_kinds = (RecordKind.MERGE_APPROVAL,)
        result = drive(
            host,  # type: ignore[arg-type]
            paths=env.paths,
            config=env.config,
            ports=env.ports(),
            clock=fixed_clock(),
            max_rounds=4,
        )
        assert isinstance(result.outcome, EngineStopped)
        assert result.outcome.code == "port_unavailable"
        assert result.submitted == ()

    def test_an_engine_stop_ends_the_loop_before_any_host_work(self, tmp_path) -> None:
        env = runtime_env(tmp_path, state=machine_state(), seeded=(RecordKind.REVIEW_RESULT,))
        host = gate_host(env)
        result = drive(
            host,  # type: ignore[arg-type]
            paths=env.paths,
            config=env.config,
            ports=env.ports(),
            clock=fixed_clock(),
            max_rounds=4,
        )
        assert isinstance(result.outcome, EngineStopped)
        assert host.executed == []

    def test_a_host_action_round_reaches_the_host(self, tmp_path) -> None:
        """`HOST_ACTION`もuser入力と同じ`HostPort.execute`を通る（同一interface）。"""
        env = _gate_env(tmp_path)
        host = gate_host(env)
        first = _step(env)
        assert isinstance(first.outcome, AwaitUser)
        submit_result(
            host.execute(first.outcome),
            paths=env.paths,
            config=env.config,
            ports=env.ports(),
            accepted_at="2026-08-26T09:05:00Z",
        )
        second = _step(env, round_ports(env), prefix="act")
        assert isinstance(second.outcome, HostActionIssued)
        assert second.outcome.action.action_kind == "ANSWER_GATE_QUESTION"
        assert len(second.trace.persisted) == 1


@pytest.mark.parametrize("state", [GATE_STATE, MachineState(state=State.CANCELLED)])
def test_step_never_raises_for_reachable_states(tmp_path, state: MachineState) -> None:
    """到達し得るstateで例外を投げない（進退は必ず構造化outcomeで返る）。"""
    env = runtime_env(tmp_path, state=state, seeded=(RecordKind.FINAL_REPORT,))
    assert isinstance(_step(env), StepResult)
