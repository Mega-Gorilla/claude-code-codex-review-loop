# SPDX-License-Identifier: Apache-2.0
"""signal受け取りと安全点の受入test（Phase 8 PR-3b2。ADR-0021）。

handlerは**flagを立てるだけ**で、checkpointの書き込みもprocessの停止も安全点で行う。
signal handlerは任意のbytecode境界で走るため、そこでI/Oをすると書きかけのfileを残し得る。

signalの有無で**状態遷移が変わらない**ことも固定する。signalは停止要求を作るだけで、
実行は`advance` -> `EmergencyStopRequired` -> `emergency_stop`の1経路を通る。
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from c07_support.helpers import NUMBER, REPOSITORY, RUN
from c08_support.helpers import job_object_ref, machine_state, user_machine_state
from c08_support.runtime import (
    ISSUED_AT,
    FakeIds,
    RuntimeEnv,
    fixed_clock,
    gate_host,
    round_ports,
    runtime_env,
)

from claude_code_codex_review_loop.domain.values import Awaiting, RecordKind, State
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
    read_machine_state,
    read_stop_request,
    stop_evidence,
    with_active_trees,
    with_stop_request,
)


def _stop_request(env: RuntimeEnv):
    loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
    assert isinstance(loaded, CheckpointLoaded), loaded
    return read_stop_request(loaded.payload)


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
            ports=env.ports(),
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
            ports=env.ports(),
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
            "ports": env.ports(),
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
            ports=env.ports(),
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
            ports=env.ports(),
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
            ports=env.ports(),
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
            ports=env.ports(),
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


def _loaded(env: RuntimeEnv):
    loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
    assert isinstance(loaded, CheckpointLoaded), loaded
    return loaded.payload


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
