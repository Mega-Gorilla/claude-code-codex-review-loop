# SPDX-License-Identifier: Apache-2.0
"""緊急停止の受入test（Phase 8 PR-3b2。ADR-0021）。

C-01は緊急停止を**手続きを持たないまま**完了させる（C-05 rule）ため、停止に失敗しても
`HaltRun`は再発行されない。停止意図をC-08側の台帳（`stop_request`）で持つのが本PRの要点で、
ここではその**順序**（記録が停止より先）と**失敗時にstateを進めないこと**を固定する。
"""

from __future__ import annotations

import pytest
from c06_support.helpers import HEAD
from c07_support.helpers import NUMBER, REPOSITORY, RUN, checkpoint_payload, state_paths
from c08_support.helpers import (
    GRACE_SECONDS,
    FakeStopPort,
    cancelling,
    halting_for_block,
    job_object_ref,
    machine_state,
    process_group_ref,
)

from claude_code_codex_review_loop.domain.states import State
from claude_code_codex_review_loop.domain.values import MachineState, OpaqueRef
from claude_code_codex_review_loop.process import StopError, TreeRef
from claude_code_codex_review_loop.state import (
    CheckpointLoaded,
    checkpoint_path,
    load_checkpoint,
    save_checkpoint,
)
from claude_code_codex_review_loop.workflow import (
    EmergencyStopCompleted,
    EmergencyStopFailed,
    EmergencyStopRequested,
    EngineStopped,
    StopRequest,
    emergency_stop,
    read_active_trees,
    read_machine_state,
    read_stop_request,
    request_emergency_stop,
    stop_evidence,
    with_active_trees,
    with_machine_state,
    with_stop_request,
)

REQUESTED_AT = "2026-08-26T12:00:00Z"


def _seed(tmp_path, *, state: MachineState, trees=(), request: StopRequest | None = None):
    paths = state_paths(tmp_path)
    payload = with_machine_state(checkpoint_payload(), state)
    payload = with_active_trees(payload, list(trees))
    payload = with_stop_request(payload, request)
    save_checkpoint(checkpoint_path(paths, RUN), payload)
    return paths


def _payload(paths):
    loaded = load_checkpoint(checkpoint_path(paths, RUN))
    assert isinstance(loaded, CheckpointLoaded), loaded
    return loaded.payload


def _request(paths, **overrides):
    values: dict[str, object] = {
        "paths": paths,
        "run_id": RUN,
        "repository": REPOSITORY,
        "number": NUMBER,
        "requested_at": REQUESTED_AT,
    }
    values.update(overrides)
    return request_emergency_stop(**values)  # type: ignore[arg-type]


def _stop(paths, **overrides):
    values: dict[str, object] = {
        "paths": paths,
        "run_id": RUN,
        "repository": REPOSITORY,
        "number": NUMBER,
        "stop_port": FakeStopPort(),
        "grace_seconds": GRACE_SECONDS,
    }
    values.update(overrides)
    return emergency_stop(**values)  # type: ignore[arg-type]


class TestRequest:
    def test_the_intent_is_recorded_before_anything_is_stopped(self, tmp_path) -> None:
        """**記録が停止より先**である（書く前に落ちると停止意図が消えるため）。"""
        ref = job_object_ref()
        paths = _seed(tmp_path, state=machine_state(), trees=[ref])
        port = FakeStopPort()

        outcome = _request(paths)
        assert isinstance(outcome, EmergencyStopRequested)
        assert outcome.already_recorded is False
        # 要求はcheckpointに在るが、treeはまだ止めていない
        assert read_stop_request(_payload(paths)) == outcome.request
        assert port.calls == []
        assert read_active_trees(_payload(paths)) == (ref,)

    def test_the_state_is_not_advanced_by_the_request(self, tmp_path) -> None:
        paths = _seed(tmp_path, state=machine_state())
        _request(paths)
        assert read_machine_state(_payload(paths)) == machine_state()

    def test_a_second_request_does_not_overwrite_the_first(self, tmp_path) -> None:
        """evidenceと要求時刻が変わると、同じ要求が別のeventになる。"""
        paths = _seed(tmp_path, state=machine_state())
        first = _request(paths)
        assert isinstance(first, EmergencyStopRequested)
        again = _request(paths, requested_at="2026-08-26T13:00:00Z")
        assert isinstance(again, EmergencyStopRequested)
        assert again.already_recorded is True
        assert again.request == first.request

    def test_an_unreadable_request_stops_instead_of_overwriting(self, tmp_path) -> None:
        paths = _seed(tmp_path, state=machine_state())
        payload = dict(_payload(paths))
        payload["stop_request"] = {"requested_at": REQUESTED_AT}  # evidenceが無い
        save_checkpoint(checkpoint_path(paths, RUN), payload)
        outcome = _request(paths)
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "stop_request_unavailable"

    def test_the_evidence_is_deterministic(self) -> None:
        """同じrunと要求時刻からは同じ値が出る（resumeが再生できる前提）。"""
        args = {
            "run_id": RUN,
            "repository": REPOSITORY,
            "number": NUMBER,
            "requested_at": REQUESTED_AT,
        }
        assert stop_evidence(**args) == stop_evidence(**args)  # type: ignore[arg-type]
        other = dict(args, requested_at="2026-08-26T13:00:00Z")
        assert stop_evidence(**other) != stop_evidence(**args)  # type: ignore[arg-type]


class TestStop:
    def test_a_normal_run_reaches_cancelled(self, tmp_path) -> None:
        """C-05: `NormalProcedure` + `emergency_evidence` -> `CANCELLED`。"""
        ref = job_object_ref()
        paths = _seed(tmp_path, state=machine_state(), trees=[ref])
        recorded = _request(paths)
        assert isinstance(recorded, EmergencyStopRequested)

        port = FakeStopPort()
        outcome = _stop(paths, stop_port=port)
        assert isinstance(outcome, EmergencyStopCompleted)
        assert outcome.cancelled is True
        assert outcome.machine_state.state is State.CANCELLED
        assert [call[0] for call in port.calls] == [ref]
        # 台帳も要求も消えている
        assert read_active_trees(_payload(paths)) == ()
        assert read_stop_request(_payload(paths)) is None
        assert read_machine_state(_payload(paths)).state is State.CANCELLED

    def test_every_tree_is_stopped(self, tmp_path) -> None:
        refs: list[TreeRef] = [job_object_ref(pid=11), process_group_ref(pid=22, pgid=22)]
        paths = _seed(tmp_path, state=machine_state(), trees=refs)
        _request(paths)
        port = FakeStopPort()
        outcome = _stop(paths, stop_port=port)
        assert isinstance(outcome, EmergencyStopCompleted)
        assert [call[0] for call in port.calls] == refs
        assert len(outcome.stopped) == 2

    def test_a_failed_stop_keeps_the_intent_and_the_state(self, tmp_path) -> None:
        """**`RunFailed`を入力しない**。F-01でFAILEDへ進むと停止意図が消える。"""
        ref = job_object_ref()
        paths = _seed(tmp_path, state=machine_state(), trees=[ref])
        recorded = _request(paths)
        assert isinstance(recorded, EmergencyStopRequested)

        outcome = _stop(paths, stop_port=FakeStopPort(fails=frozenset({ref})))
        assert isinstance(outcome, EmergencyStopFailed)
        assert read_stop_request(_payload(paths)) == recorded.request
        assert read_active_trees(_payload(paths)) == (ref,)
        assert read_machine_state(_payload(paths)) == machine_state()

    def test_a_resume_replays_the_same_evidence_and_finishes(self, tmp_path) -> None:
        """停止失敗 -> 再開 -> 再停止。要求はそのまま再生される（冪等）。"""
        ref = job_object_ref()
        paths = _seed(tmp_path, state=machine_state(), trees=[ref])
        recorded = _request(paths)
        assert isinstance(recorded, EmergencyStopRequested)
        assert isinstance(_stop(paths, stop_port=FakeStopPort(fails=frozenset({ref}))), EmergencyStopFailed)

        port = FakeStopPort()
        outcome = _stop(paths, stop_port=port)
        assert isinstance(outcome, EmergencyStopCompleted)
        assert outcome.machine_state.state is State.CANCELLED
        assert [call[0] for call in port.calls] == [ref]

    @pytest.mark.parametrize(
        ("label", "state"),
        [
            ("cancelling", cancelling()),
            ("halting_for_block", halting_for_block()),
        ],
        ids=lambda value: value if isinstance(value, str) else "",
    )
    def test_a_procedure_keeps_ownership_of_the_state(self, tmp_path, label: str, state) -> None:
        """手続き中はeventを入力しない（C-01が binding不一致で拒否するため）。"""
        ref = job_object_ref()
        paths = _seed(tmp_path, state=state, trees=[ref])
        _request(paths)
        port = FakeStopPort()
        outcome = _stop(paths, stop_port=port)
        assert isinstance(outcome, EmergencyStopCompleted)
        assert outcome.cancelled is False
        # treeは止まり、stateは手続きのまま残る（完了させるのは`halt`）
        assert [call[0] for call in port.calls] == [ref]
        assert read_machine_state(_payload(paths)) == state
        assert read_stop_request(_payload(paths)) is None

    def test_a_terminal_run_only_stops_the_trees(self, tmp_path) -> None:
        """終端stateはC-05の対象外。treeだけ止めて要求を消す。"""
        ref = job_object_ref()
        paths = _seed(tmp_path, state=MachineState(state=State.CANCELLED), trees=[ref])
        _request(paths)
        port = FakeStopPort()
        outcome = _stop(paths, stop_port=port)
        assert isinstance(outcome, EmergencyStopCompleted)
        assert outcome.cancelled is False
        assert [call[0] for call in port.calls] == [ref]
        assert read_machine_state(_payload(paths)).state is State.CANCELLED
        assert read_stop_request(_payload(paths)) is None

    def test_no_tree_is_still_a_completion(self, tmp_path) -> None:
        """止める対象が無くても停止は完了する（`halt`と同じ扱い）。"""
        paths = _seed(tmp_path, state=machine_state())
        _request(paths)
        outcome = _stop(paths)
        assert isinstance(outcome, EmergencyStopCompleted)
        assert outcome.stopped == ()
        assert outcome.machine_state.state is State.CANCELLED

    def test_without_a_request_the_stop_is_refused(self, tmp_path) -> None:
        paths = _seed(tmp_path, state=machine_state())
        outcome = _stop(paths)
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "no_stop_request"

    def test_an_unreadable_request_refuses_the_stop(self, tmp_path) -> None:
        paths = _seed(tmp_path, state=machine_state())
        payload = dict(_payload(paths))
        payload["stop_request"] = {"requested_at": REQUESTED_AT, "evidence": "es:1"}
        save_checkpoint(checkpoint_path(paths, RUN), payload)
        outcome = _stop(paths)
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "stop_request_unavailable"

    def test_an_unreadable_tree_ledger_refuses_the_stop(self, tmp_path) -> None:
        paths = _seed(tmp_path, state=machine_state())
        _request(paths)
        payload = dict(_payload(paths))
        payload["processes"] = {"trees": [{"kind": "JOB_OBJECT", "pid": 4242}]}
        save_checkpoint(checkpoint_path(paths, RUN), payload)
        outcome = _stop(paths)
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "processes_unavailable"

    def test_a_missing_run_is_reported(self, tmp_path) -> None:
        paths = state_paths(tmp_path)
        assert isinstance(_request(paths), EngineStopped)
        assert isinstance(_stop(paths), EngineStopped)


class TestStopRequestSection:
    def test_the_section_round_trips(self) -> None:
        request = StopRequest(
            requested_at=REQUESTED_AT,
            evidence=OpaqueRef("es:abc"),
            source_state=State.APPLYING_FIXES,
        )
        assert read_stop_request(with_stop_request({}, request)) == request

    def test_clearing_removes_the_section(self) -> None:
        request = StopRequest(
            requested_at=REQUESTED_AT, evidence=OpaqueRef("es:abc"), source_state=State.MERGING
        )
        payload = with_stop_request({}, request)
        assert "stop_request" not in with_stop_request(payload, None)

    @pytest.mark.parametrize(
        "section",
        [
            "not-an-object",
            {},
            {"requested_at": REQUESTED_AT},
            {"requested_at": REQUESTED_AT, "evidence": "es:1"},
            {"requested_at": REQUESTED_AT, "evidence": "es:1", "source_state": "NOPE"},
            {"requested_at": "", "evidence": "es:1", "source_state": "MERGING"},
        ],
        ids=("not_object", "empty", "no_evidence", "no_state", "unknown_state", "empty_at"),
    )
    def test_an_incomplete_section_is_refused(self, section: object) -> None:
        """欠けたfieldを既定値で埋めると、停止意図の内容を推測することになる。"""
        from claude_code_codex_review_loop.workflow import SectionUnavailable

        assert isinstance(read_stop_request({"stop_request": section}), SectionUnavailable)

    def test_a_stop_port_error_is_reported_as_a_failure(self, tmp_path) -> None:
        """C-03の`StopError`は`ProcessError`なので、停止失敗として扱われる。"""
        ref = job_object_ref()
        paths = _seed(tmp_path, state=machine_state(), trees=[ref])
        _request(paths)

        class Failing:
            def stop(self, ref: TreeRef, grace_seconds: float):
                raise StopError("stop", "止められない")

        outcome = _stop(paths, stop_port=Failing())
        assert isinstance(outcome, EmergencyStopFailed)
        assert "止められない" in outcome.detail


def test_head_is_unused_by_the_stop_path() -> None:
    """緊急停止はheadへbindしない（run / checkpointへのbindだけ）。"""
    evidence = stop_evidence(
        run_id=RUN, repository=REPOSITORY, number=NUMBER, requested_at=REQUESTED_AT
    )
    assert HEAD not in evidence.value
