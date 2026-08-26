# SPDX-License-Identifier: Apache-2.0
"""`HaltRun`の実行の受入test（Phase 8 PR-3a。ADR-0019）。

**停止してから保存する**順序と、停止対象が無い場合も正常完了すること、停止できない場合は
stateを進めず停止commandの再発行だけを行うことを固定する。実processは起動しない
（実tree停止はC-03のtestが担保する）。
"""

from __future__ import annotations

import pytest
from c07_support.helpers import RUN
from c08_support.helpers import (
    GRACE_SECONDS,
    FakeStopPort,
    cancelling,
    halt_env,
    halt_kwargs,
    halting_for_block,
    job_object_ref,
    process_group_ref,
)

from claude_code_codex_review_loop.domain.commands import HaltRun
from claude_code_codex_review_loop.domain.states import State
from claude_code_codex_review_loop.domain.values import (
    MachineState,
    OpaqueBinding,
    PendingRecord,
    RecordKind,
)
from claude_code_codex_review_loop.process import StopMethod
from claude_code_codex_review_loop.state import (
    CheckpointLoaded,
    checkpoint_path,
    load_checkpoint,
    save_checkpoint,
)
from claude_code_codex_review_loop.workflow import (
    EngineStopped,
    HaltCompleted,
    HaltFailed,
    halt,
    read_active_trees,
    read_machine_state,
)

EMERGENCY = "run-1:checkpoint-1"


def _payload(env) -> dict[str, object]:
    loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
    assert isinstance(loaded, CheckpointLoaded)
    return loaded.payload


class TestCancel:
    def test_a_cancel_halt_reaches_cancelled(self, tmp_path) -> None:
        env = halt_env(tmp_path, state=cancelling(), trees=[process_group_ref()])
        port = FakeStopPort()
        outcome = halt(**halt_kwargs(env, stop_port=port))
        assert isinstance(outcome, HaltCompleted)
        assert outcome.machine_state.state is State.CANCELLED
        assert outcome.commands == ()
        assert port.calls == [(process_group_ref(), GRACE_SECONDS)]

    def test_the_terminal_state_is_saved(self, tmp_path) -> None:
        env = halt_env(tmp_path, state=cancelling(), trees=[process_group_ref()])
        assert isinstance(halt(**halt_kwargs(env)), HaltCompleted)
        assert read_machine_state(_payload(env)) == MachineState(state=State.CANCELLED)

    def test_stopped_trees_leave_the_ledger(self, tmp_path) -> None:
        """停止できたtreeを残すと、pidが再利用されたとき別treeへ到達し得る。"""
        env = halt_env(
            tmp_path, state=cancelling(), trees=[process_group_ref(), job_object_ref(pid=99)]
        )
        outcome = halt(**halt_kwargs(env))
        assert isinstance(outcome, HaltCompleted) and len(outcome.stopped) == 2
        assert read_active_trees(_payload(env)) == ()
        assert "processes" not in _payload(env)

    def test_a_stale_pending_record_is_not_persisted(self, tmp_path) -> None:
        """cancel経路2のstale pendingは監査参照であり、永続化対象ではない。"""
        state = MachineState(
            state=State.APPLYING_FIXES,
            procedure=cancelling().procedure,
            pending_record=PendingRecord(
                kind=RecordKind.FIX_RESULT,
                binding=OpaqueBinding("cr:run-1:1:fix"),
                source_state=State.APPLYING_FIXES,
            ),
        )
        env = halt_env(tmp_path, state=state)
        outcome = halt(**halt_kwargs(env))
        assert isinstance(outcome, HaltCompleted)
        assert outcome.machine_state.state is State.CANCELLED


class TestIntegrityHalt:
    def test_a_block_halt_reaches_blocked(self, tmp_path) -> None:
        env = halt_env(tmp_path, state=halting_for_block(), trees=[process_group_ref()])
        outcome = halt(**halt_kwargs(env))
        assert isinstance(outcome, HaltCompleted)
        assert outcome.machine_state.state is State.BLOCKED
        assert outcome.machine_state.block is not None

    def test_the_blocked_state_round_trips(self, tmp_path) -> None:
        env = halt_env(tmp_path, state=halting_for_block())
        outcome = halt(**halt_kwargs(env))
        assert isinstance(outcome, HaltCompleted)
        assert read_machine_state(_payload(env)) == outcome.machine_state


class TestEmergency:
    def test_an_emergency_stop_reaches_cancelled(self, tmp_path) -> None:
        """Ctrl+C等の緊急停止も同じ停止・checkpoint境界へ合流する。"""
        env = halt_env(
            tmp_path,
            state=MachineState(state=State.APPLYING_FIXES),
            trees=[process_group_ref()],
        )
        outcome = halt(**halt_kwargs(env, emergency_evidence=EMERGENCY))
        assert isinstance(outcome, HaltCompleted)
        assert outcome.machine_state.state is State.CANCELLED

    def test_an_emergency_stop_c01_refuses_is_reported(self, tmp_path) -> None:
        """MERGINGのoutcome段階では緊急停止を受理しない（別headの黙示mergeを防ぐ設計）。"""
        from claude_code_codex_review_loop.domain.values import (
            Awaiting,
            IntegrityEvidenceRef,
            OpaqueRef,
        )

        state = MachineState(
            state=State.MERGING,
            awaiting=Awaiting.MERGE_OUTCOME_EXECUTE,
            deferred_integrity=(
                IntegrityEvidenceRef(
                    binding=OpaqueBinding("iv:gap:run-1:2"),
                    descriptor=OpaqueRef("gap"),
                    head=OpaqueRef("a" * 40),
                ),
            ),
        )
        env = halt_env(tmp_path, state=state, trees=[process_group_ref()])
        outcome = halt(**halt_kwargs(env, emergency_evidence=EMERGENCY))
        assert isinstance(outcome, EngineStopped) and outcome.code == "illegal_event"

    def test_without_evidence_there_is_no_procedure(self, tmp_path) -> None:
        """手続きも緊急停止の根拠も無ければ、停止したことにしない。"""
        env = halt_env(tmp_path, state=MachineState(state=State.APPLYING_FIXES))
        outcome = halt(**halt_kwargs(env))
        assert isinstance(outcome, EngineStopped) and outcome.code == "no_halt_procedure"


class TestNoTrees:
    @pytest.mark.parametrize(
        "state", [cancelling(), halting_for_block()], ids=["cancel", "integrity"]
    )
    def test_completes_without_anything_to_stop(self, tmp_path, state: MachineState) -> None:
        """実行中processが無い場合も正常完了する（C-01の横断規則の前提）。"""
        env = halt_env(tmp_path, state=state)
        port = FakeStopPort()
        outcome = halt(**halt_kwargs(env, stop_port=port))
        assert isinstance(outcome, HaltCompleted)
        assert outcome.stopped == () and port.calls == []

    def test_an_already_exited_tree_is_a_normal_stop(self, tmp_path) -> None:
        env = halt_env(tmp_path, state=cancelling(), trees=[process_group_ref()])
        port = FakeStopPort(method=StopMethod.ALREADY_EXITED)
        outcome = halt(**halt_kwargs(env, stop_port=port))
        assert isinstance(outcome, HaltCompleted)
        assert outcome.stopped[0].method is StopMethod.ALREADY_EXITED


class TestStopFailure:
    def test_a_failed_stop_reissues_the_halt(self, tmp_path) -> None:
        """停止できなければstateを進めず、`HaltRun`の冪等再発行だけを行う。"""
        env = halt_env(tmp_path, state=cancelling(), trees=[process_group_ref()])
        port = FakeStopPort(fails=frozenset({process_group_ref()}))
        outcome = halt(**halt_kwargs(env, stop_port=port))
        assert isinstance(outcome, HaltFailed)
        assert outcome.machine_state == cancelling()
        assert outcome.commands == (HaltRun(OpaqueBinding("cr:run-1:1:cancel")),)

    def test_the_unstopped_tree_stays_in_the_ledger(self, tmp_path) -> None:
        """次のresumeが同じrefで再試行できるよう、止められなかったtreeは残す。"""
        first, second = process_group_ref(), job_object_ref(pid=99)
        env = halt_env(tmp_path, state=cancelling(), trees=[first, second])
        port = FakeStopPort(fails=frozenset({second}))
        assert isinstance(halt(**halt_kwargs(env, stop_port=port)), HaltFailed)
        assert read_active_trees(_payload(env)) == (second,)

    def test_a_retry_after_the_failure_completes(self, tmp_path) -> None:
        """同じhaltをもう一度呼べば、残ったtreeだけを止めて完了する（冪等）。"""
        first, second = process_group_ref(), job_object_ref(pid=99)
        env = halt_env(tmp_path, state=cancelling(), trees=[first, second])
        assert isinstance(
            halt(**halt_kwargs(env, stop_port=FakeStopPort(fails=frozenset({second})))),
            HaltFailed,
        )
        port = FakeStopPort()
        outcome = halt(**halt_kwargs(env, stop_port=port))
        assert isinstance(outcome, HaltCompleted)
        assert [ref for ref, _ in port.calls] == [second]
        assert outcome.machine_state.state is State.CANCELLED


class TestResume:
    def test_a_crash_after_stopping_re_stops_and_completes(self, tmp_path) -> None:
        """停止後・保存前に落ちても、再開が同じrefへ再停止して完了する。"""
        env = halt_env(tmp_path, state=cancelling(), trees=[process_group_ref()])
        before = _payload(env)
        assert isinstance(halt(**halt_kwargs(env)), HaltCompleted)
        save_checkpoint(checkpoint_path(env.paths, RUN), before)  # 停止直後へ戻す
        port = FakeStopPort()
        outcome = halt(**halt_kwargs(env, stop_port=port))
        assert isinstance(outcome, HaltCompleted)
        assert port.calls == [(process_group_ref(), GRACE_SECONDS)]
        assert outcome.machine_state.state is State.CANCELLED

    def test_halting_a_completed_run_stops(self, tmp_path) -> None:
        """保存後の再実行は、停止手続きが無いとして止まる。"""
        env = halt_env(tmp_path, state=cancelling())
        assert isinstance(halt(**halt_kwargs(env)), HaltCompleted)
        outcome = halt(**halt_kwargs(env))
        assert isinstance(outcome, EngineStopped) and outcome.code == "no_halt_procedure"


class TestRefusals:
    def test_a_missing_checkpoint_stops(self, tmp_path) -> None:
        env = halt_env(tmp_path, state=cancelling())
        checkpoint_path(env.paths, RUN).unlink()
        outcome = halt(**halt_kwargs(env))
        assert isinstance(outcome, EngineStopped) and outcome.code == "checkpoint_unavailable"

    @pytest.mark.parametrize(
        "trees",
        [
            [{"kind": "PROCESS_GROUP", "pid": 1}],
            [{"kind": "JOB_OBJECT", "pid": 1}],
            [{"kind": "PROCESS_GROUP", "pid": 0, "pgid": 1}],
        ],
        ids=["no_pgid", "no_job_name", "bad_pid"],
    )
    def test_an_unreadable_tree_ledger_stops(self, tmp_path, trees: list[object]) -> None:
        """停止対象を推測しない（片方のfieldで代用すると別treeへ到達し得る）。"""
        env = halt_env(tmp_path, state=cancelling())
        payload = _payload(env)
        payload["processes"] = {"trees": trees}
        save_checkpoint(checkpoint_path(env.paths, RUN), payload)
        outcome = halt(**halt_kwargs(env))
        assert isinstance(outcome, EngineStopped) and outcome.code == "processes_unavailable"

    def test_grace_seconds_is_a_parameter(self, tmp_path) -> None:
        """既定値はengineが持たない（解決はC-12）。"""
        env = halt_env(tmp_path, state=cancelling(), trees=[process_group_ref()])
        port = FakeStopPort()
        halt(**halt_kwargs(env, stop_port=port, grace_seconds=7.5))
        assert port.calls == [(process_group_ref(), 7.5)]
