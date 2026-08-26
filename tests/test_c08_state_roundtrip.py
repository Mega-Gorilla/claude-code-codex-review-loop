# SPDX-License-Identifier: Apache-2.0
"""`MachineState`のcheckpoint round-trip契約（Phase 8 PR-3a。ADR-0019）。

**到達可能な全非terminal `MachineState`**について`write -> save -> load -> read`が
一致することを固定する。procedureは値を持つ直和なので、discriminatorだけでなくpayloadを
含めて復元できなければ、停止手続きの途中で中断したrunを別processから継続できない。

到達可能性の判定は`MachineState`が構築できるかどうかで行う（C-01の組合せ不変条件が
構築時に検証されるため、構築できない組合せはそもそも存在しない）。
"""

from __future__ import annotations

import itertools

import pytest
from c07_support.helpers import RUN, checkpoint_payload, state_paths

from claude_code_codex_review_loop.domain._rules_workflow import BLOCKED_CONTINUATIONS
from claude_code_codex_review_loop.domain.states import RESUMABLE_STATES, TERMINAL_STATES, State
from claude_code_codex_review_loop.domain.values import (
    AWAITING_HOME,
    NORMAL,
    Awaiting,
    Budget,
    CancellingProcedure,
    ExternalDependencyBlock,
    HaltingForBlockProcedure,
    IllegalMachineStateError,
    IncidentTarget,
    IntegrityEvidenceRef,
    MachineState,
    OpaqueBinding,
    OpaqueFingerprint,
    OpaqueRef,
    OpaqueSnapshot,
    PendingRecord,
    Procedure,
    Progress,
    ProgressBlock,
    RecordEvidence,
    RecordingIncidentProcedure,
    RecordIntegrityBlock,
    RecordKind,
)
from claude_code_codex_review_loop.state import (
    CheckpointLoaded,
    checkpoint_path,
    load_checkpoint,
    save_checkpoint,
)
from claude_code_codex_review_loop.workflow import (
    SectionUnavailable,
    read_machine_state,
    with_machine_state,
    with_verified_machine_state,
)

HEAD = "a" * 40


def _violation(binding: str = "iv:gap:run-1:2") -> IntegrityEvidenceRef:
    return IntegrityEvidenceRef(
        binding=OpaqueBinding(binding), descriptor=OpaqueRef("gap"), head=OpaqueRef(HEAD)
    )


def _pending(kind: RecordKind, state: State) -> PendingRecord:
    return PendingRecord(kind=kind, binding=OpaqueBinding("cr:run-1:1:x"), source_state=state)


PROCEDURES: dict[str, Procedure] = {
    "NORMAL": NORMAL,
    "CANCELLING": CancellingProcedure(attempt_binding=OpaqueBinding("cr:run-1:1:cancel")),
    "HALTING_FOR_BLOCK": HaltingForBlockProcedure(
        block=RecordIntegrityBlock(violations=(_violation(),)),
        attempt_binding=_violation().binding,
    ),
    "RECORDING_INCIDENT_bare": RecordingIncidentProcedure(
        target=IncidentTarget.CANCELLED, audit=None
    ),
    "RECORDING_INCIDENT_audit": RecordingIncidentProcedure(
        target=IncidentTarget.MERGED,
        audit=_pending(RecordKind.FIX_RESULT, State.APPLYING_FIXES),
    ),
}

BLOCKS = {
    "RECORD_INTEGRITY": RecordIntegrityBlock(violations=(_violation(),)),
    "PROGRESS_limit": ProgressBlock(
        binding=OpaqueBinding("cr:run-1:1:progress"),
        head=OpaqueRef(HEAD),
        continuation=BLOCKED_CONTINUATIONS["REVIEW_BLOCKING"],
        reason=Progress.LIMIT_REACHED,
        budget=Budget.REVIEW_ROUND,
        counter_snapshot=OpaqueSnapshot("snap-1"),
        fingerprint=OpaqueFingerprint("fp-1"),
    ),
    "PROGRESS_stalled": ProgressBlock(
        binding=OpaqueBinding("cr:run-1:2:progress"),
        head=OpaqueRef(HEAD),
        continuation=BLOCKED_CONTINUATIONS["CLARIFICATION"],
        reason=Progress.NO_PROGRESS,
        budget=Budget.CLARIFICATION_TURN,
        counter_snapshot=OpaqueSnapshot("snap-2"),
        fingerprint=OpaqueFingerprint("fp-2"),
    ),
    "EXTERNAL_DEPENDENCY": ExternalDependencyBlock(
        binding=OpaqueBinding("cr:run-1:1:external"),
        head=OpaqueRef(HEAD),
        continuation=BLOCKED_CONTINUATIONS["EXTERNAL_DEPENDENCY"],
        evidence=RecordEvidence(
            kind=RecordKind.EXTERNAL_DEPENDENCY,
            binding=OpaqueBinding("cr:run-1:1:external"),
            ref=OpaqueRef("c-1"),
        ),
    ),
}


def _reachable_states() -> list[MachineState]:
    """C-01が構築を許す非terminal `MachineState`を組合せから集める。

    構築できない組合せは不変条件が拒否するため、`IllegalMachineStateError`を捨てるだけで
    「到達可能な範囲」が得られる。testが手で列挙すると、不変条件が緩んだときに気づけない。
    """
    states: list[MachineState] = []
    non_terminal = sorted(set(State) - TERMINAL_STATES, key=lambda s: s.value)
    awaitings: list[Awaiting | None] = [None, *sorted(Awaiting, key=lambda a: a.value)]
    for state, procedure, awaiting in itertools.product(
        non_terminal, PROCEDURES.values(), awaitings
    ):
        for block in (None, *BLOCKS.values()):
            for deferred in ((), (_violation("iv:tamper:run-1:3"),)):
                for pending in (None, _pending(RecordKind.USER_CANCEL, state)):
                    try:
                        states.append(
                            MachineState(
                                state=state,
                                procedure=procedure,
                                awaiting=awaiting,
                                pending_record=pending,
                                deferred_integrity=deferred,
                                return_to=(
                                    State.RUNNING_REVIEW
                                    if state is State.AWAITING_TOOL_PERMISSION
                                    else None
                                ),
                                recovery_to=(
                                    State.APPLYING_FIXES if state is State.FAILED else None
                                ),
                                block=block,
                            )
                        )
                    except IllegalMachineStateError:
                        continue
    return states


REACHABLE = _reachable_states()


def test_the_sample_covers_every_variant() -> None:
    """組合せが痩せていないこと（生成器が黙って縮んだ場合に気づけるようにする）。"""
    assert {state.state for state in REACHABLE} == set(State) - TERMINAL_STATES
    assert {type(state.procedure) for state in REACHABLE} == {
        type(procedure) for procedure in PROCEDURES.values()
    }
    assert {type(state.block) for state in REACHABLE if state.block is not None} == {
        type(block) for block in BLOCKS.values()
    }
    assert any(state.pending_record is not None for state in REACHABLE)
    assert any(state.deferred_integrity for state in REACHABLE)
    assert any(state.awaiting is not None for state in REACHABLE)
    assert any(state.return_to is not None for state in REACHABLE)
    assert any(state.recovery_to is not None for state in REACHABLE)


@pytest.mark.parametrize(
    "machine_state", REACHABLE, ids=[str(index) for index in range(len(REACHABLE))]
)
def test_every_reachable_state_round_trips(tmp_path, machine_state: MachineState) -> None:
    """write -> save -> load -> readが一致する（AC-C08-06の前提）。"""
    payload = with_verified_machine_state(checkpoint_payload(), machine_state)
    assert not isinstance(payload, SectionUnavailable), machine_state
    paths = state_paths(tmp_path)
    save_checkpoint(checkpoint_path(paths, RUN), payload)
    loaded = load_checkpoint(checkpoint_path(paths, RUN))
    assert isinstance(loaded, CheckpointLoaded)
    assert read_machine_state(loaded.payload) == machine_state


def test_every_continuation_round_trips(tmp_path) -> None:
    """継続はIDで保存し、registryを引いて同じobjectへ戻す（ADR-0019 決定1）。"""
    for key, continuation in BLOCKED_CONTINUATIONS.items():
        block = ProgressBlock(
            binding=OpaqueBinding(f"cr:run-1:1:{key}"),
            head=OpaqueRef(HEAD),
            continuation=continuation,
            reason=Progress.NO_PROGRESS,
            budget=Budget.REVIEW_ROUND,
            counter_snapshot=OpaqueSnapshot("snap-1"),
            fingerprint=OpaqueFingerprint("fp-1"),
        )
        state = MachineState(state=State.BLOCKED, block=block)
        payload = with_machine_state(checkpoint_payload(), state)
        section = payload["state"]
        assert isinstance(section, dict)
        assert section["block"]["continuation"] == key
        assert read_machine_state(payload) == state


class TestFailClosed:
    """未知variant / 必須payloadの欠落は`NORMAL`へ丸めず停止する。"""

    @pytest.mark.parametrize(
        "procedure",
        [
            {"kind": "CANCELLING"},
            {"kind": "HALTING_FOR_BLOCK"},
            {"kind": "RECORDING_INCIDENT"},
            {"kind": "RECORDING_INCIDENT", "target": "CANCELLED", "audit": "x"},
        ],
        ids=["cancel_binding", "halt_binding", "incident_target", "incident_audit"],
    )
    def test_incomplete_procedure_is_reported(self, procedure: dict[str, object]) -> None:
        payload = checkpoint_payload(state={"state": "APPLYING_FIXES", "procedure": procedure})
        assert isinstance(read_machine_state(payload), SectionUnavailable)

    @pytest.mark.parametrize(
        "block",
        [
            {"kind": "PROGRESS"},
            {"kind": "PROGRESS", "binding": "b", "head": "h", "continuation": "FIX_RESULT"},
            {"kind": "EXTERNAL_DEPENDENCY"},
            {
                "kind": "EXTERNAL_DEPENDENCY",
                "binding": "b",
                "head": "h",
                "continuation": "EXTERNAL_DEPENDENCY",
            },
            {"kind": "RECORD_INTEGRITY"},
        ],
        ids=["progress", "progress_partial", "external", "external_no_evidence", "integrity_empty"],
    )
    def test_incomplete_block_is_reported(self, block: dict[str, object]) -> None:
        payload = checkpoint_payload(state={"state": "BLOCKED", "block": block})
        assert isinstance(read_machine_state(payload), SectionUnavailable)

    def test_an_unknown_continuation_id_is_reported(self) -> None:
        """registryに無いIDでcommand列を推測しない。"""
        payload = checkpoint_payload(
            state={
                "state": "BLOCKED",
                "block": {
                    "kind": "PROGRESS",
                    "binding": "b",
                    "head": "h",
                    "continuation": "FIX_RESULT",
                    "reason": "NO_PROGRESS",
                    "budget": "REVIEW_ROUND",
                    "counter_snapshot": "s",
                    "fingerprint": "f",
                },
            }
        )
        assert not isinstance(read_machine_state(payload), SectionUnavailable)
        section = payload["state"]
        assert isinstance(section, dict)
        section["block"]["continuation"] = "NOT_A_CONTINUATION"
        assert isinstance(read_machine_state(payload), SectionUnavailable)


def test_resumable_states_are_covered() -> None:
    """resumable stateはすべてsampleに現れる（中断からの復元がそこで起きる）。"""
    covered = {state.state for state in REACHABLE}
    assert RESUMABLE_STATES <= covered


def test_awaiting_home_is_respected() -> None:
    """awaitingを持つsampleは、そのawaitingが滞在できるstateに限る。"""
    for state in REACHABLE:
        if state.awaiting is not None:
            assert state.state in AWAITING_HOME[state.awaiting] or state.state is State.FAILED


def test_a_continuation_outside_the_registry_is_refused() -> None:
    """registryに無い継続を持つblockは保存しない（round-trip検証が止める）。

    ruleがそのような継続を作らないことはC-01のcontract testが固定しており、ここは
    書き手が壊れたときに**黙って状態を落とさない**ことの確認である。
    """
    from claude_code_codex_review_loop.domain.values import BlockedContinuation

    block = ProgressBlock(
        binding=OpaqueBinding("cr:run-1:1:progress"),
        head=OpaqueRef(HEAD),
        continuation=BlockedContinuation(
            resume_state=State.RUNNING_REVIEW, commands=(), awaiting=None
        ),
        reason=Progress.NO_PROGRESS,
        budget=Budget.REVIEW_ROUND,
        counter_snapshot=OpaqueSnapshot("snap-1"),
        fingerprint=OpaqueFingerprint("fp-1"),
    )
    state = MachineState(state=State.BLOCKED, block=block)
    assert isinstance(with_verified_machine_state(checkpoint_payload(), state), SectionUnavailable)
