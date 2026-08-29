# SPDX-License-Identifier: Apache-2.0
"""signal受け取りと安全点の受入test（Phase 8 PR-3b2。ADR-0021）。

handlerは**flagを立てるだけ**で、checkpointの書き込みもprocessの停止も安全点で行う。
signal handlerは任意のbytecode境界で走るため、そこでI/Oをすると書きかけのfileを残し得る。

停止portは常にfakeにする。台帳へ置くrefはtestが組み立てた値で実在するprocessを指さず、
製品の`TreeStopper`へ渡すと現在のOSのprocess APIを実際に叩く（他OSのref種別は
`ref_mismatch`で拒否される）。実停止の挙動はC-03のtestが担保する。

signalの有無で**状態遷移が変わらない**ことも固定する。signalは停止要求を作るだけで、
実行は`advance` -> `EmergencyStopRequired` -> `emergency_stop`の1経路を通る。
"""

from __future__ import annotations

import dataclasses
import json
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from c07_support.helpers import NUMBER, REPOSITORY, RUN
from c08_support.helpers import (
    FakeStopPort,
    job_object_ref,
    machine_state,
    user_machine_state,
)
from c08_support.runtime import (
    ISSUED_AT,
    FakeIds,
    RuntimeEnv,
    fixed_clock,
    gate_host,
    round_ports,
    runtime_env,
    stopping_ports,
)

from claude_code_codex_review_loop.domain.values import Awaiting, RecordKind, State
from claude_code_codex_review_loop.process import StopMethod, StopResult, TreeRef
from claude_code_codex_review_loop.runtime import drive, step
from claude_code_codex_review_loop.runtime.signals import (
    StopSignal,
    install_stop_handler,
    signal_names,
)
from claude_code_codex_review_loop.state import CheckpointLoaded, checkpoint_path, load_checkpoint
from claude_code_codex_review_loop.workflow import (
    EngineStopped,
    StopRequest,
    Terminal,
    TreesStopped,
    read_active_trees,
    read_machine_state,
    read_stop_request,
    stop_evidence,
    stop_trees,
    with_active_trees,
    with_stop_request,
)


def _loaded(env: RuntimeEnv) -> dict[str, object]:
    loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
    assert isinstance(loaded, CheckpointLoaded), loaded
    return loaded.payload


def _stop_request(env: RuntimeEnv):
    return read_stop_request(_loaded(env))


def _gate_env(tmp_path) -> RuntimeEnv:
    return runtime_env(
        tmp_path,
        state=user_machine_state(Awaiting.USER_INPUT_GATE),
        seeded=(RecordKind.FINAL_REPORT,),
    )


class TestStopSignal:
    def test_a_fresh_signal_is_not_requested(self) -> None:
        assert StopSignal().requested is False

    def test_only_the_first_signal_is_kept(self) -> None:
        """2回目以降で理由が書き換わらない（最初の要求が停止の根拠）。"""
        stop = StopSignal()
        stop.record(signal.SIGINT)
        stop.record(99)
        assert stop.received == signal.SIGINT
        assert stop.requested is True


class TestHandlerInstallation:
    def test_the_handler_only_sets_the_flag(self) -> None:
        """handlerの中では例外を投げず、I/Oもしない。"""
        stop = StopSignal()
        with install_stop_handler(stop):
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)  # type: ignore[operator]
        assert stop.received == signal.SIGINT

    def test_the_previous_handler_is_restored(self) -> None:
        """設置はprocess全体の状態を変えるので、必ず元へ戻す。"""
        before = {name: signal.getsignal(getattr(signal, name)) for name in signal_names()}
        with install_stop_handler(StopSignal()):
            assert signal.getsignal(signal.SIGINT) is not before["SIGINT"]
        assert {
            name: signal.getsignal(getattr(signal, name)) for name in signal_names()
        } == before

    def test_the_restore_happens_even_on_an_exception(self) -> None:
        before = signal.getsignal(signal.SIGINT)
        with pytest.raises(RuntimeError), install_stop_handler(StopSignal()):
            raise RuntimeError("boom")
        assert signal.getsignal(signal.SIGINT) is before

    def test_the_platform_signals_are_the_expected_pair(self) -> None:
        """Windowsは`SIGTERM`をconsoleから配送できないため`SIGBREAK`を使う。"""
        expected = "SIGBREAK" if sys.platform == "win32" else "SIGTERM"
        assert signal_names() == ("SIGINT", expected)
        assert all(hasattr(signal, name) for name in signal_names())


class TestForceEscalation:
    """AC-C03-02: 1回目でgraceful、**grace待機中の2回目で即時force**。

    grace待機はC-03の停止primitiveの中にありflagでは中断できないため、2回目は
    `KeyboardInterrupt`として届く（ADR-0005 Consequencesが前提にしている中断手段）。
    昇格のwiringはC-08の責務である（同 決定6）。
    """

    def test_the_second_signal_is_recorded_as_force(self) -> None:
        stop = StopSignal()
        stop.record(signal.SIGINT)
        assert stop.force_requested is False
        stop.record_force(signal.SIGINT)
        assert stop.force_requested is True

    def test_only_the_first_force_is_kept(self) -> None:
        stop = StopSignal()
        stop.record_force(signal.SIGINT)
        stop.record_force(99)
        assert stop.force_received == signal.SIGINT

    def test_the_handler_raises_only_on_the_second_signal(self) -> None:
        """1回目は例外にしない（安全点での停止経路を使う）。"""
        stop = StopSignal()
        with install_stop_handler(stop):
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)  # type: ignore[operator]
            assert stop.requested and not stop.force_requested
            with pytest.raises(KeyboardInterrupt):
                handler(signal.SIGINT, None)  # type: ignore[operator]
        assert stop.force_requested is True

    def test_a_pending_force_skips_the_grace_wait(self) -> None:
        """停止を始める前にforce要求が在れば、最初からgrace 0で呼ぶ。"""
        stop = StopSignal()
        stop.record(signal.SIGINT)
        stop.record_force(signal.SIGINT)
        port = FakeStopPort()
        outcome = stop_trees(
            [job_object_ref()], stop_port=port, grace_seconds=30.0, escalation=stop
        )
        assert isinstance(outcome, TreesStopped)
        assert [grace for _, grace in port.calls] == [0.0]

    def test_an_interrupted_grace_wait_is_retried_as_force(self) -> None:
        """grace待機中の2回目: `KeyboardInterrupt`を捕まえて即時forceでやり直す。"""
        stop = StopSignal()
        stop.record(signal.SIGINT)
        port = _InterruptingStopPort(stop)
        outcome = stop_trees(
            [job_object_ref()], stop_port=port, grace_seconds=30.0, escalation=stop
        )
        assert isinstance(outcome, TreesStopped)
        # 1回目は設定されたgrace、2回目は即時force
        assert port.graces == [30.0, 0.0]

    def test_an_unrelated_interrupt_is_not_swallowed(self) -> None:
        """force要求を伴わない中断は握り潰さない（別の理由の中断を停止へ読み替えない）。"""

        class Interrupting:
            def stop(self, ref: TreeRef, grace_seconds: float) -> StopResult:
                raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            stop_trees([job_object_ref()], stop_port=Interrupting(), grace_seconds=30.0)

    def test_the_escalation_is_optional(self) -> None:
        """signalを持たない呼び出し（resume経路）は従来どおり動く。"""
        port = FakeStopPort()
        outcome = stop_trees([job_object_ref()], stop_port=port, grace_seconds=1.5)
        assert isinstance(outcome, TreesStopped)
        assert [grace for _, grace in port.calls] == [1.5]


@dataclass
class _InterruptingStopPort:
    """1回目の呼び出しでgrace待機中の2回目signalを再現するport。"""

    stop_signal: StopSignal
    graces: list[float] = field(default_factory=list)

    def stop(self, ref: TreeRef, grace_seconds: float) -> StopResult:
        self.graces.append(grace_seconds)
        if len(self.graces) == 1:
            # grace待機中に2回目が届いた（handlerと同じ順序でflagを立ててから送出する）
            self.stop_signal.record_force(signal.SIGINT)
            raise KeyboardInterrupt
        return StopResult(method=StopMethod.FORCED, graceful_requested=True)


class TestForceOutsideTheStop:
    """2回目が`stop_trees`の**外側**で届いた場合（ADR-0021 決定19-h）。

    1回目のsignal後、要求を保存する前・保存中・台帳の読込中・`drive`のhost作業中にも
    2回目は届く。**どこで届いても停止を完了させる**ことを、窓ごとに固定する。
    """

    def _env(self, tmp_path, ref):
        return runtime_env(
            tmp_path, state=machine_state(), extra=with_active_trees({}, [ref])
        )

    def _signalled(self) -> StopSignal:
        """1回目を受けてから2回目が届いた状態のsignal。"""
        stop = StopSignal()
        stop.record(signal.SIGINT)
        stop.record_force(signal.SIGINT)
        return stop

    def _step(self, env, stop, port):
        return step(
            paths=env.paths,
            config=env.config,
            ports=dataclasses.replace(env.ports(), stop=port),
            id_source=FakeIds("sig"),
            issued_at=ISSUED_AT,
            stop=stop,
        )

    def test_an_interrupt_before_the_request_is_saved_still_stops(
        self, tmp_path, monkeypatch
    ) -> None:
        """**保存前**の中断。台帳に根拠が無いまま終了させない。"""
        from claude_code_codex_review_loop.runtime import session as session_module

        ref = job_object_ref()
        env = self._env(tmp_path, ref)
        stop = self._signalled()
        original = session_module.request_emergency_stop
        calls: list[int] = []

        def _interrupting(**kwargs: object):
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt  # 保存に入る直前で2回目が届いた
            return original(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(session_module, "request_emergency_stop", _interrupting)
        port = FakeStopPort()
        result = self._step(env, stop, port)

        assert isinstance(result.outcome, EngineStopped)
        assert result.outcome.code == "forced_stop"
        # 要求はdurableになり、treeは即時forceで止まっている
        assert _stop_request(env) is None  # 停止完了と同時に消費される
        assert [grace for _, grace in port.calls] == [0.0]
        assert read_machine_state(_loaded(env)).state is State.CANCELLED

    def test_an_interrupt_during_advance_still_stops(self, tmp_path, monkeypatch) -> None:
        """**要求の保存後・停止の前**の中断（`advance`はGitHubを触り得る）。"""
        from claude_code_codex_review_loop.runtime import session as session_module

        ref = job_object_ref()
        env = self._env(tmp_path, ref)
        stop = self._signalled()
        original = session_module.advance
        calls: list[int] = []

        def _interrupting(**kwargs: object):
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt
            return original(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(session_module, "advance", _interrupting)
        port = FakeStopPort()
        result = self._step(env, stop, port)

        assert isinstance(result.outcome, EngineStopped)
        assert result.outcome.code == "forced_stop"
        assert [grace for _, grace in port.calls] == [0.0]
        assert read_machine_state(_loaded(env)).state is State.CANCELLED

    def test_the_forced_path_does_not_touch_github(self, tmp_path, monkeypatch) -> None:
        """forceは「待たずに殺せ」なので、chain gate（GitHub取得）を通さない。"""
        from claude_code_codex_review_loop.runtime import session as session_module

        ref = job_object_ref()
        env = self._env(tmp_path, ref)
        stop = self._signalled()

        def _interrupting(**kwargs: object):
            raise KeyboardInterrupt

        monkeypatch.setattr(session_module, "advance", _interrupting)
        result = self._step(env, stop, FakeStopPort())
        # `advance`は1度も成功していないのに停止は完了している
        assert isinstance(result.outcome, EngineStopped)
        assert result.outcome.code == "forced_stop"
        assert result.trace.stopped == 1

    def test_a_failed_forced_stop_keeps_the_request(self, tmp_path, monkeypatch) -> None:
        """force経路でも停止できなければstateを進めず、要求を残す。"""
        from claude_code_codex_review_loop.runtime import session as session_module

        ref = job_object_ref()
        env = self._env(tmp_path, ref)
        stop = self._signalled()
        monkeypatch.setattr(
            session_module, "advance", lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        result = self._step(env, stop, FakeStopPort(fails=frozenset({ref})))

        assert isinstance(result.outcome, EngineStopped)
        assert result.outcome.code == "emergency_stop_failed"
        assert _stop_request(env) is not None  # 次のresumeが停止をやり直す

    def _already_recorded(self) -> StopSignal:
        """要求は保存済みで、その後に2回目が届いた状態（`_forced_stop`へ直行する）。"""
        stop = self._signalled()
        stop.mark_recorded()
        return stop

    def test_an_unrecordable_request_is_reported(self, tmp_path, monkeypatch) -> None:
        """force経路でも要求を読めなければ推測しない。"""
        from claude_code_codex_review_loop.runtime import session as session_module

        env = runtime_env(
            tmp_path,
            state=machine_state(),
            extra={"stop_request": {"requested_at": "2026-08-26T11:00:00Z"}},
        )
        monkeypatch.setattr(
            session_module, "advance", lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        result = self._step(env, self._already_recorded(), FakeStopPort())
        assert isinstance(result.outcome, EngineStopped)
        assert result.outcome.code == "stop_request_unavailable"

    def test_an_unreadable_ledger_is_reported_in_the_forced_path(
        self, tmp_path, monkeypatch
    ) -> None:
        """force経路でも停止対象を推測しない。"""
        from claude_code_codex_review_loop.runtime import session as session_module

        request = StopRequest(
            requested_at="2026-08-26T11:00:00Z",
            evidence=stop_evidence(
                run_id=RUN,
                repository=REPOSITORY,
                number=NUMBER,
                requested_at="2026-08-26T11:00:00Z",
            ),
            source_state=State.APPLYING_FIXES,
        )
        env = runtime_env(
            tmp_path,
            state=machine_state(),
            extra=with_stop_request(
                {"processes": {"trees": [{"kind": "JOB_OBJECT", "pid": 4242}]}}, request
            ),
        )
        monkeypatch.setattr(
            session_module, "advance", lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        result = self._step(env, self._already_recorded(), FakeStopPort())
        assert isinstance(result.outcome, EngineStopped)
        assert result.outcome.code == "processes_unavailable"

    def test_an_interrupt_without_a_force_request_is_not_swallowed(
        self, tmp_path, monkeypatch
    ) -> None:
        """1回目だけの状態での中断は停止要求へ読み替えない。"""
        from claude_code_codex_review_loop.runtime import session as session_module

        env = self._env(tmp_path, job_object_ref())
        stop = StopSignal()
        stop.record(signal.SIGINT)
        monkeypatch.setattr(
            session_module,
            "request_emergency_stop",
            lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            self._step(env, stop, FakeStopPort())


class TestForceDuringHostWork:
    """`drive`の`host.execute` / `submit_result`中に2回目が届いた場合。

    この2区間はroundの中で最も長く（3-b3では headless processの起動と待機になる）、
    **`step`の外側**にある。`step`のcatchはここを覆わないので、`drive`が受け止める。
    """

    def _env(self, tmp_path):
        return runtime_env(
            tmp_path,
            state=user_machine_state(Awaiting.USER_INPUT_GATE),
            seeded=(RecordKind.FINAL_REPORT,),
            extra=with_active_trees({}, [job_object_ref()]),
        )

    def _drive(self, env, host, stop, port):
        return drive(
            host,
            paths=env.paths,
            config=env.config,
            ports=dataclasses.replace(round_ports(env), stop=port),
            clock=fixed_clock(),
            max_rounds=4,
            stop=stop,
        )

    def test_an_interrupt_in_host_work_stops_the_tree(self, tmp_path) -> None:
        """例外を漏らさず、停止portを`grace = 0`で呼び、台帳を消費する。"""
        env = self._env(tmp_path)
        stop = StopSignal()
        host = gate_host(env)

        def _interrupting(work: object) -> bytes:
            # host作業中に1回目と2回目が続けて届いた（handlerと同じ順序）
            stop.record(signal.SIGINT)
            stop.record_force(signal.SIGINT)
            raise KeyboardInterrupt

        host.execute = _interrupting  # type: ignore[method-assign, assignment]
        port = FakeStopPort()
        result = self._drive(env, host, stop, port)

        # 例外は漏れず、次の`step`が要求のdurable化と`grace = 0`の停止をやり切る
        assert result.outcome == Terminal(state=State.CANCELLED)
        assert result.submitted == ()  # 未submitの結果は捨てる
        assert [grace for _, grace in port.calls] == [0.0]
        assert _stop_request(env) is None  # 停止まで完了して消費されている
        assert read_active_trees(_loaded(env)) == ()

    def test_an_interrupt_in_submit_stops_the_tree(self, tmp_path, monkeypatch) -> None:
        """`submit_result`はchain取得を含むため、同じ窓になる。"""
        from claude_code_codex_review_loop.runtime import host as host_module

        env = self._env(tmp_path)
        stop = StopSignal()

        def _interrupting(*args: object, **kwargs: object):
            stop.record(signal.SIGINT)
            stop.record_force(signal.SIGINT)
            raise KeyboardInterrupt

        monkeypatch.setattr(host_module, "submit_result", _interrupting)
        port = FakeStopPort()
        result = self._drive(env, gate_host(env), stop, port)

        assert result.outcome == Terminal(state=State.CANCELLED)
        assert result.submitted == ()
        assert [grace for _, grace in port.calls] == [0.0]
        assert read_machine_state(_loaded(env)).state is State.CANCELLED

    def test_an_interrupt_without_a_force_request_is_not_swallowed(self, tmp_path) -> None:
        """1回目だけの中断（別の理由の中断）は`drive`が握り潰さない。"""
        env = self._env(tmp_path)
        stop = StopSignal()
        host = gate_host(env)

        def _interrupting(work: object) -> bytes:
            raise KeyboardInterrupt

        host.execute = _interrupting  # type: ignore[method-assign, assignment]
        with pytest.raises(KeyboardInterrupt):
            self._drive(env, host, stop, FakeStopPort())


class TestSafePoints:
    """flagを見るのは`step`のengine作業の境目と`drive`のround境界だけである。"""

    def test_step_records_the_request_and_stops(self, tmp_path) -> None:
        env = runtime_env(
            tmp_path, state=machine_state(), extra=with_active_trees({}, [job_object_ref()])
        )
        stop = StopSignal()
        stop.record(signal.SIGINT)
        result = step(
            paths=env.paths,
            config=env.config,
            ports=stopping_ports(env),
            id_source=FakeIds("sig"),
            issued_at=ISSUED_AT,
            stop=stop,
        )
        assert result.trace.stop_requested == 1
        assert result.trace.stopped == 1
        assert result.outcome == Terminal(state=State.CANCELLED)
        assert _stop_request(env) is None

    def test_no_signal_leaves_the_run_untouched(self, tmp_path) -> None:
        """signalが無ければ従来どおり素通しする（既存経路への影響が無い）。"""
        env = _gate_env(tmp_path)
        result = step(
            paths=env.paths,
            config=env.config,
            ports=stopping_ports(env),
            id_source=FakeIds("req"),
            issued_at=ISSUED_AT,
            stop=StopSignal(),
        )
        assert result.trace.stop_requested == 0
        assert result.trace.stopped == 0
        assert _stop_request(env) is None

    def test_the_request_is_recorded_once_across_steps(self, tmp_path) -> None:
        """同じsignalで2回要求を作らない（evidenceが変わらない）。"""
        env = runtime_env(tmp_path, state=machine_state())
        stop = StopSignal()
        stop.record(signal.SIGINT)
        kwargs = {
            "paths": env.paths,
            "config": env.config,
            "ports": stopping_ports(env),
            "issued_at": ISSUED_AT,
            "stop": stop,
        }
        first = step(id_source=FakeIds("a"), **kwargs)  # type: ignore[arg-type]
        assert first.trace.stop_requested == 1
        second = step(id_source=FakeIds("b"), **kwargs)  # type: ignore[arg-type]
        # 既にCANCELLEDなので新しい要求は作られない
        assert second.trace.stop_requested == 0
        assert second.outcome == Terminal(state=State.CANCELLED)

    def test_an_existing_request_is_not_duplicated(self, tmp_path) -> None:
        """signalより前に要求が在れば作り直さない（evidenceが変わらない）。"""
        request = StopRequest(
            requested_at="2026-08-26T11:00:00Z",
            evidence=stop_evidence(
                run_id=RUN,
                repository=REPOSITORY,
                number=NUMBER,
                requested_at="2026-08-26T11:00:00Z",
            ),
            source_state=State.APPLYING_FIXES,
        )
        env = runtime_env(
            tmp_path, state=machine_state(), extra=with_stop_request({}, request)
        )
        stop = StopSignal()
        stop.record(signal.SIGINT)
        result = step(
            paths=env.paths,
            config=env.config,
            ports=stopping_ports(env),
            id_source=FakeIds("sig"),
            issued_at=ISSUED_AT,
            stop=stop,
        )
        assert result.trace.stop_requested == 0  # 既存の要求をそのまま使う
        assert result.trace.stopped == 1
        assert result.outcome == Terminal(state=State.CANCELLED)

    def test_an_unreadable_request_stops_the_step(self, tmp_path) -> None:
        """signal経路でも構造化outcomeで返す（推測して上書きしない）。"""
        env = runtime_env(
            tmp_path,
            state=machine_state(),
            extra={"stop_request": {"requested_at": "2026-08-26T11:00:00Z"}},
        )
        stop = StopSignal()
        stop.record(signal.SIGINT)
        outcome = step(
            paths=env.paths,
            config=env.config,
            ports=stopping_ports(env),
            id_source=FakeIds("sig"),
            issued_at=ISSUED_AT,
            stop=stop,
        ).outcome
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "stop_request_unavailable"

    def test_advance_reports_an_unreadable_request(self, tmp_path) -> None:
        """signalが無い経路（resume）でも同じ分類で止まる。"""
        env = runtime_env(
            tmp_path,
            state=machine_state(),
            extra={"stop_request": {"requested_at": "2026-08-26T11:00:00Z"}},
        )
        outcome = step(
            paths=env.paths,
            config=env.config,
            ports=stopping_ports(env),
            id_source=FakeIds("sig"),
            issued_at=ISSUED_AT,
        ).outcome
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "stop_request_unavailable"

    def test_an_unreadable_tree_ledger_stops_the_step(self, tmp_path) -> None:
        """停止対象を推測しない（`emergency_stop`が返した理由をそのまま返す）。"""
        request = StopRequest(
            requested_at="2026-08-26T11:00:00Z",
            evidence=stop_evidence(
                run_id=RUN,
                repository=REPOSITORY,
                number=NUMBER,
                requested_at="2026-08-26T11:00:00Z",
            ),
            source_state=State.APPLYING_FIXES,
        )
        extra = with_stop_request(
            {"processes": {"trees": [{"kind": "JOB_OBJECT", "pid": 4242}]}}, request
        )
        env = runtime_env(tmp_path, state=machine_state(), extra=extra)
        outcome = step(
            paths=env.paths,
            config=env.config,
            ports=stopping_ports(env),
            id_source=FakeIds("sig"),
            issued_at=ISSUED_AT,
        ).outcome
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "processes_unavailable"

    def test_drive_stops_before_starting_a_round(self, tmp_path) -> None:
        env = _gate_env(tmp_path)
        host = gate_host(env)
        stop = StopSignal()
        stop.record(signal.SIGINT)
        result = drive(
            host,
            paths=env.paths,
            config=env.config,
            ports=round_ports(env),
            clock=fixed_clock(),
            max_rounds=4,
            stop=stop,
        )
        assert result.outcome == Terminal(state=State.CANCELLED)
        assert host.executed == []  # hostへは1度も出さない

    def test_a_signal_during_host_work_is_seen_on_return(self, tmp_path) -> None:
        """host作業中のsignalは戻り直後が最初の安全点になる。

        未submitの結果は捨てる。hostへ出したactionはcheckpointに未完了として残り、
        resumeが同じactionを再提示する（ADR-0014 決定22）。
        """
        env = _gate_env(tmp_path)
        stop = StopSignal()
        host = gate_host(env)
        original = host.execute

        def signalling(work: object) -> bytes:
            raw = original(work)  # type: ignore[arg-type]
            stop.record(signal.SIGINT)
            return raw

        host.execute = signalling  # type: ignore[method-assign, assignment]
        result = drive(
            host,
            paths=env.paths,
            config=env.config,
            ports=round_ports(env),
            clock=fixed_clock(),
            max_rounds=4,
            stop=stop,
        )
        assert result.outcome == Terminal(state=State.CANCELLED)
        assert result.rounds == 1
        assert result.submitted == ()  # 結果は受理していない
        assert len(host.executed) == 1
        assert read_machine_state(_loaded(env)).state is State.CANCELLED



class TestRealSignal:
    """**実signal**を別processへ送る。同一process内でhandlerを呼ぶだけでは、

    handlerが実際に配送されること・main loopが安全点でそれを見ること・tracebackにならず
    構造化結果で終わることが証明できない。
    """

    def _send(self, tmp_path, state, trees) -> dict:
        env = runtime_env(tmp_path, state=state, extra=with_active_trees({}, list(trees)))
        driver = Path(__file__).resolve().parent / "c08_support" / "driver.py"
        kwargs: dict[str, object] = {}
        if sys.platform == "win32":
            # WindowsはSIGINTをsubprocessへ送れない。新しいprocess groupを作って
            # CTRL_BREAK_EVENTを送る（C-03のjob_objectが停止要求に使うのと同じ手段）
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        child = subprocess.Popen(  # noqa: S603 - 起動するのは自分たちのtest driver
            [sys.executable, str(driver), str(env.paths.root), "wait-for-signal"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            **kwargs,
        )
        try:
            assert child.stdout is not None
            ready = child.stdout.readline().strip()
            assert ready == "READY", ready  # handler設置済みを確かめてから送る
            child.send_signal(
                signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
            )
            stdout, stderr = child.communicate(timeout=60)
        finally:
            if child.poll() is None:  # pragma: no cover - timeoutした場合の後始末
                child.kill()
                child.communicate()
        assert child.returncode == 0, stderr
        assert "Traceback" not in stderr, stderr
        payload = json.loads(stdout.splitlines()[-1])
        assert isinstance(payload, dict)
        return {**payload, "env": env}

    def test_a_real_signal_cancels_the_run(self, tmp_path) -> None:
        result = self._send(tmp_path, machine_state(), [job_object_ref()])
        assert result["outcome"] == "TERMINAL"
        assert result["state"] == State.CANCELLED.value
        assert result["stop_requested"] == 1
        assert result["stopped"] == 1
        assert _stop_request(result["env"]) is None
