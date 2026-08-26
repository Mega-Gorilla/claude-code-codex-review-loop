# SPDX-License-Identifier: Apache-2.0
"""R系列: registry性質のproperty test（AC-C01-01 / 02 / 04 / 05 / 08）。

到達可能な（状態 × 付随値 × event × guard値）の全展開で一致ruleが常に0件または1件で
あること（R1）、純粋性（R2）、17 stateの到達可能性とterminal保護（R3）、宣言summaryと
実効の一致を機械検証する。
"""

from __future__ import annotations

import re
from itertools import product
from pathlib import Path

import pytest
from c01_support.helpers import HEAD

from claude_code_codex_review_loop.domain import (
    REGISTRY,
    State,
    TransitionRejected,
    check_registry,
    initialize,
    transition,
)
from claude_code_codex_review_loop.domain import events as ev
from claude_code_codex_review_loop.domain._rules_workflow import BLOCKED_CONTINUATIONS
from claude_code_codex_review_loop.domain.machine import select_rule
from claude_code_codex_review_loop.domain.states import ACTIVE_STATES, TERMINAL_STATES
from claude_code_codex_review_loop.domain.values import (
    AWAITING_HOME,
    NORMAL,
    Awaiting,
    BlockedContinuation,
    BlockResolutionEvidence,
    CancellingProcedure,
    ExternalDependencyBlock,
    HaltingForBlockProcedure,
    IllegalMachineStateError,
    IncidentTarget,
    IntegrityEvidenceRef,
    MachineState,
    OpaqueBinding,
    OpaqueRef,
    PendingRecord,
    Procedure,
    Progress,
    ProgressBlock,
    ProgressReport,
    RecordEvidence,
    RecordingIncidentProcedure,
    RecordIntegrityBlock,
    RecordKind,
    RegistryIntegrityError,
)
from claude_code_codex_review_loop.domain.values import (
    OpaqueFingerprint as Fp,
)
from claude_code_codex_review_loop.domain.values import (
    OpaqueSnapshot as Snap,
)

_S = State
_K = RecordKind
_NON_TERMINAL = tuple(s for s in State if s not in TERMINAL_STATES)

_PEND_BINDING = OpaqueBinding("pend-1")
_PROC_BINDING = OpaqueBinding("proc-1")
_HALT_VIOLATION = IntegrityEvidenceRef(OpaqueBinding("hv-1"), OpaqueRef("desc"), HEAD)
_DEFERRED = (IntegrityEvidenceRef(OpaqueBinding("dv-1"), OpaqueRef("desc"), HEAD),)

# stateごとにpendingとして意味を持つrecord kind（PRODUCED rule由来）+ 代表の無関係kind
_PENDING_KINDS: dict[State, tuple[RecordKind, ...]] = {}
for _rule in REGISTRY:
    if _rule.match.event_type is ev.RecordProduced and _rule.match.record_kinds is not None:
        for _state in _rule.match.states:
            for _kind in _rule.match.record_kinds:
                _PENDING_KINDS.setdefault(_state, ())
                if _kind not in _PENDING_KINDS[_state]:
                    _PENDING_KINDS[_state] = _PENDING_KINDS[_state] + (_kind,)


def _pending_options(state: State) -> tuple[PendingRecord | None, ...]:
    kinds = set(_PENDING_KINDS.get(state, ()))
    kinds.add(RecordKind.GATE_ANSWER)  # どのruleにも一致しない「無関係kind」の代表
    kinds.add(RecordKind.INTEGRITY_INCIDENT)
    options: list[PendingRecord | None] = [None]
    options.extend(PendingRecord(kind, _PEND_BINDING, state) for kind in sorted(kinds, key=lambda k: k.value))
    if state is State.FAILED:
        # 引継pending（source_stateが進入元）
        options.append(PendingRecord(RecordKind.REVIEW_RESULT, _PEND_BINDING, State.RUNNING_REVIEW))
    return tuple(options)


def _block_options(state: State) -> tuple[object, ...]:
    if state is not State.BLOCKED:
        return (None,)
    from c01_support.helpers import to_progress_blocked

    progress_block = to_progress_blocked(Progress.LIMIT_REACHED).block
    stalled_block = to_progress_blocked(Progress.NO_PROGRESS).block
    assert isinstance(progress_block, ProgressBlock) and isinstance(stalled_block, ProgressBlock)
    external = ExternalDependencyBlock(
        binding=OpaqueBinding("ext-1"),
        head=HEAD,
        continuation=progress_block.continuation,
        evidence=RecordEvidence(RecordKind.EXTERNAL_DEPENDENCY, OpaqueBinding("ext-1"), OpaqueRef("r")),
    )
    integrity = RecordIntegrityBlock((_HALT_VIOLATION,))
    return (progress_block, stalled_block, external, integrity)


def _procedure_options(state: State) -> tuple[Procedure, ...]:
    options: list[Procedure] = [NORMAL]
    if state is not State.MERGING:
        options.append(CancellingProcedure(_PROC_BINDING))
    if state in ACTIVE_STATES and state is not State.MERGING:
        options.append(
            HaltingForBlockProcedure(
                block=RecordIntegrityBlock((_HALT_VIOLATION,)), attempt_binding=_HALT_VIOLATION.binding
            )
        )
    options.append(RecordingIncidentProcedure(IncidentTarget.MERGED, None))
    options.append(
        RecordingIncidentProcedure(
            IncidentTarget.CANCELLED, PendingRecord(RecordKind.REVIEW_RESULT, OpaqueBinding("audit-1"), state)
        )
    )
    return tuple(options)


def _awaiting_options(state: State) -> tuple[Awaiting | None, ...]:
    allowed = [a for a, homes in AWAITING_HOME.items() if state in homes or state is State.FAILED]
    return (None, *allowed)


def enumerate_machine_states() -> list[MachineState]:
    """構築可能な代表MachineStateを全数列挙する（opaque値は照合用の固定代表値）。"""
    results: list[MachineState] = []
    for state in _NON_TERMINAL:
        for procedure, awaiting, pending, deferred, block, return_to, recovery_to in product(
            _procedure_options(state),
            _awaiting_options(state),
            _pending_options(state),
            ((), _DEFERRED),
            _block_options(state),
            ((None, State.RUNNING_REVIEW) if state is State.AWAITING_TOOL_PERMISSION else (None,)),
            ((None, State.RUNNING_REVIEW) if state is State.FAILED else (None,)),
        ):
            try:
                results.append(
                    MachineState(
                        state=state,
                        procedure=procedure,
                        awaiting=awaiting,
                        pending_record=pending,
                        deferred_integrity=deferred,
                        return_to=return_to,
                        recovery_to=recovery_to,
                        block=block,  # type: ignore[arg-type]
                    )
                )
            except IllegalMachineStateError:
                continue
    results.append(MachineState(state=State.MERGED))
    results.append(MachineState(state=State.CANCELLED))
    return results


def _resolutions(ms: MachineState) -> list[BlockResolutionEvidence]:
    mismatched = BlockResolutionEvidence(target_block_binding=OpaqueBinding("no-such-block"), head=HEAD)
    if not isinstance(ms.block, (ProgressBlock, ExternalDependencyBlock, RecordIntegrityBlock)):
        return [mismatched]
    block = ms.block
    if isinstance(block, ProgressBlock):
        matched = BlockResolutionEvidence(
            target_block_binding=block.binding,
            head=block.head,
            record=RecordEvidence(RecordKind.BLOCK_INTERVENTION, _PEND_BINDING, OpaqueRef("r")),
            reason=block.reason,
            budget=block.budget,
            counter_snapshot=block.counter_snapshot,
            fingerprint=block.fingerprint,
        )
    elif isinstance(block, ExternalDependencyBlock):
        matched = BlockResolutionEvidence(
            target_block_binding=block.binding,
            head=block.head,
            record=RecordEvidence(RecordKind.BLOCK_INTERVENTION, _PEND_BINDING, OpaqueRef("r")),
        )
    else:
        matched = BlockResolutionEvidence(
            target_block_binding=block.representative_binding,
            head=block.head,
            violation_bindings=tuple(ref.binding for ref in block.violations),
        )
    return [matched, mismatched]


def events_for(ms: MachineState) -> list[ev.Event]:
    """当該MachineStateに対する代表eventを網羅的に生成する。"""
    events: list[ev.Event] = []
    for kind in RecordKind:
        events.append(ev.RecordProduced(kind, _PEND_BINDING))

    def evidences(kind: RecordKind) -> list[RecordEvidence]:
        return [
            RecordEvidence(kind, _PEND_BINDING, OpaqueRef("r")),
            RecordEvidence(kind, OpaqueBinding("other-1"), OpaqueRef("r")),
        ]

    def reports() -> list[ProgressReport]:
        return [ProgressReport(p, HEAD, Snap("snap-1"), Fp("fp-1")) for p in Progress]

    for evd in evidences(_K.REVIEW_RESULT):
        events.append(ev.ReviewApprovedVerified(evd))
        events.extend(ev.ReviewBlockingVerified(evd, r) for r in reports())
    for evd in evidences(_K.FIX_RESULT):
        events.extend(ev.FixResultVerified(evd, r) for r in reports())
    for evd in evidences(_K.CLARIFICATION_QUESTION):
        events.extend(ev.ClarificationQuestionVerified(evd, r) for r in reports())
    for evd in evidences(_K.CLARIFICATION_ANSWER):
        events.append(ev.ClarificationConfirmedVerified(evd))
        events.append(ev.ClarificationRevisedVerified(evd))
        events.append(ev.ClarificationWithdrawnVerified(evd))
        events.append(ev.ClarificationEscalatedVerified(evd))
    for evd in evidences(_K.DECISION_REQUEST):
        events.append(ev.DecisionRequestVerified(evd))
    for evd in evidences(_K.DECISION_VERDICT):
        events.append(ev.VerdictAskUserVerified(evd))
        events.append(ev.VerdictProceedVerified(evd))
        events.extend(ev.VerdictResubmitVerified(evd, r) for r in reports())
    for evd in evidences(_K.DECISION_BRIEF):
        events.append(ev.DecisionBriefVerified(evd))
    for evd in evidences(_K.DECISION_RECORD):
        events.append(ev.DecisionRecordVerified(evd))
    for evd in evidences(_K.USER_DECISION):
        events.append(ev.UserDecisionVerified(evd))
    for evd in evidences(_K.EXTERNAL_DEPENDENCY):
        events.append(ev.ExternalDependencyVerified(evd, HEAD))
    for evd in evidences(_K.PERMISSION_BLOCK):
        events.append(ev.ToolPermissionBlocked(evd))
    for evd in evidences(_K.CI_TIMEOUT):
        events.append(ev.CiTimeoutRecorded(evd))
    for evd in evidences(_K.CI_CODE_FAILURE):
        events.extend(ev.CiCodeFailureVerified(evd, r) for r in reports())
    for evd in evidences(_K.FINAL_REPORT):
        events.append(ev.ReportVerified(evd))
    for evd in evidences(_K.GATE_QUESTION):
        events.append(ev.GateQuestionVerified(evd))
    for evd in evidences(_K.GATE_ANSWER):
        events.append(ev.GateAnswerVerified(evd))
    for evd in evidences(_K.GATE_CHANGES):
        events.append(ev.GateChangesVerified(evd))
    for evd in evidences(_K.MERGE_APPROVAL):
        events.append(ev.MergeApprovalVerified(evd))
    for evd in evidences(_K.USER_CANCEL):
        events.append(ev.UserCancelVerified(evd))
    for evd in evidences(_K.INTEGRITY_INCIDENT):
        # 記録済み集合がdeferred全体を含む場合（COMPLETE）と空の場合（REMAINDER）
        events.append(ev.IntegrityIncidentVerified(evd, tuple(ref.binding for ref in ms.deferred_integrity)))
        events.append(ev.IntegrityIncidentVerified(evd, ()))

    events.extend(
        (
            ev.FixStarted(),
            ev.PermissionResumeValidated(),
            ev.CiSucceeded(),
            ev.CiInfraFailure(),
            ev.CiResumeRequested(),
            ev.ReportFailed(),
            ev.ReporterRetryRequested(),
            ev.MergePreconditionsOk(),
            ev.MergePreconditionMismatch(),
            ev.MergeConfirmed(),
            ev.MergeNotExecutedConfirmed(),
            ev.MergeOutcomeUnknown(),
            ev.HeadChangedExternally(),
            ev.RunFailed(),
            ev.ResumeValidated(),
            ev.ResumeFallbackRequired(),
            ev.ResumeSameHeadValidated(),
            ev.CancellationCompleted(attempt_binding=_PROC_BINDING),
            ev.CancellationCompleted(attempt_binding=OpaqueBinding("stale-attempt")),
            ev.CancellationCompleted(emergency_evidence=OpaqueRef("ckpt-1")),
            ev.BlockHaltCompleted(attempt_binding=OpaqueBinding("hv-1")),
            ev.BlockHaltCompleted(attempt_binding=OpaqueBinding("other-halt")),
            ev.RecordIntegrityViolationDetected(_HALT_VIOLATION),
            ev.RecordIntegrityViolationDetected(
                IntegrityEvidenceRef(OpaqueBinding("nv-1"), OpaqueRef("desc"), HEAD)
            ),
        )
    )
    from dataclasses import replace as dc_replace

    intervention_record = RecordEvidence(RecordKind.BLOCK_INTERVENTION, _PEND_BINDING, OpaqueRef("r"))
    for resolution in _resolutions(ms):
        events.append(ev.BlockResolvedLimitRaised(resolution))
        # 復元 / salvageはviolation集合全体へのbindを必ず伴う（構築時検査）
        exit_resolution = resolution
        if not exit_resolution.violation_bindings:
            exit_resolution = dc_replace(exit_resolution, violation_bindings=(OpaqueBinding("no-such-violation"),))
        events.append(ev.IntegrityRestoredValidated(exit_resolution))
        events.append(ev.IntegritySalvageEstablished(exit_resolution))
        # interventionはBLOCK_INTERVENTION recordのcanonical検証を必ず伴う（構築時検査）
        if resolution.record is None or resolution.record.kind is not RecordKind.BLOCK_INTERVENTION:
            resolution = dc_replace(resolution, record=intervention_record)
        events.append(ev.BlockResolvedIntervention(resolution))
    return events


_RULES_BY_EVENT: dict[type, tuple[object, ...]] = {}
for _r in REGISTRY:
    _RULES_BY_EVENT[_r.match.event_type] = _RULES_BY_EVENT.get(_r.match.event_type, ()) + (_r,)

_STATE_VALUES = {s.value for s in State}
_COMMAND_NAME_PATTERN = re.compile(r"^[A-Za-z]+$")


class TestR1UniquenessAndEffectConsistency:
    """R1: 全展開で一致ruleが0件または1件。R2: 同一入力の再適用が同一結果。宣言と実効の一致。"""

    def test_exhaustive_enumeration(self) -> None:
        states = enumerate_machine_states()
        assert len(states) > 300  # 列挙が退化していないこと
        checked = 0
        for ms in states:
            if ms.state in TERMINAL_STATES:
                continue
            for event in events_for(ms):
                rules = _RULES_BY_EVENT.get(type(event), ())
                try:
                    rule = select_rule(rules, ms, event)  # type: ignore[arg-type]
                except TransitionRejected:
                    continue
                checked += 1
                # 純粋性: 同一入力の再適用で同一結果、入力は変更されない（frozen）
                first = rule.effect(ms, event)  # type: ignore[attr-defined]
                second = rule.effect(ms, event)  # type: ignore[attr-defined]
                assert first == second
                result_state, commands = first
                assert isinstance(result_state, MachineState)  # 不変条件を満たす（構築時検証）
                # 宣言summaryと実効の一致（具体値の宣言のみ検査。抽象label（同一state等）は対象外）
                to_state = rule.to_state  # type: ignore[attr-defined]
                if to_state in _STATE_VALUES:
                    assert result_state.state.value == to_state, rule.rule_id  # type: ignore[attr-defined]
                declared = rule.command_names  # type: ignore[attr-defined]
                if all(_COMMAND_NAME_PATTERN.match(name) for name in declared):
                    assert tuple(type(c).__name__ for c in commands) == declared, rule.rule_id  # type: ignore[attr-defined]
        assert checked > 2000  # 実効の検証が退化していないこと

    def test_multiple_matches_are_rejected_not_prioritized(self) -> None:
        rules = _RULES_BY_EVENT[ev.RunFailed]
        duplicated = rules + rules
        ms = MachineState(state=State.WAITING_CI)
        with pytest.raises(RegistryIntegrityError):
            select_rule(duplicated, ms, ev.RunFailed())  # type: ignore[arg-type]


class TestR2Purity:
    """R2: I/O・時刻・乱数・環境変数への非依存（source levelの機械検証）。"""

    def test_domain_modules_do_not_import_impure_modules(self) -> None:
        domain_dir = (
            Path(__file__).resolve().parent.parent / "src" / "claude_code_codex_review_loop" / "domain"
        )
        pattern = re.compile(r"^\s*(?:import|from)\s+(os|sys|time|random|datetime|secrets|subprocess|pathlib)\b", re.M)
        for path in sorted(domain_dir.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            assert not pattern.search(source), f"{path.name}が純粋性を破るmoduleをimportしている"


class TestR3Reachability:
    """R3: 17 stateすべて到達可能。terminalは全event拒否。未定義遷移は構造化error。"""

    def test_all_states_reachable_from_initialize(self) -> None:
        start_ok, _ = initialize(ev.PreflightOk())
        start_ng, _ = initialize(ev.PreflightNg())
        visited: set[MachineState] = set()
        frontier = [start_ok, start_ng]
        reached: set[State] = {start_ok.state, start_ng.state}
        while frontier:
            ms = frontier.pop()
            if ms in visited or ms.state in TERMINAL_STATES:
                reached.add(ms.state)
                continue
            visited.add(ms)
            assert len(visited) < 20000, "探索空間が想定を超えた"
            for event in events_for(ms):
                try:
                    nxt, _ = transition(ms, event)
                except TransitionRejected:
                    continue
                reached.add(nxt.state)
                if nxt not in visited:
                    frontier.append(nxt)
        assert reached == set(State), f"到達不能state: {set(State) - reached}"

    def test_terminal_states_reject_all_events(self) -> None:
        for terminal in (MachineState(state=State.MERGED), MachineState(state=State.CANCELLED)):
            for event in (ev.ResumeValidated(), ev.RunFailed(), ev.HeadChangedExternally()):
                with pytest.raises(TransitionRejected):
                    transition(terminal, event)

    def test_undefined_transition_is_structured_error(self) -> None:
        ms, _ = initialize(ev.PreflightOk())
        with pytest.raises(TransitionRejected) as exc_info:
            transition(ms, ev.MergeConfirmed())
        assert exc_info.value.state is State.RUNNING_REVIEW
        assert exc_info.value.event_name == "MergeConfirmed"


class TestRegistrySelfCheck:
    """registry構築時の自己検査（rule_id重複とterminal起点の拒否）。"""

    def test_duplicate_rule_ids_are_rejected(self) -> None:
        with pytest.raises(RegistryIntegrityError):
            check_registry((REGISTRY[0], REGISTRY[0]))

    def test_terminal_state_rules_are_rejected(self) -> None:
        from dataclasses import replace as dc_replace

        bad_match = dc_replace(REGISTRY[0].match, states=frozenset({State.MERGED}))
        bad_rule = dc_replace(REGISTRY[0], match=bad_match)
        with pytest.raises(RegistryIntegrityError):
            check_registry((bad_rule,))

    def test_current_registry_passes(self) -> None:
        check_registry(REGISTRY)


class TestBlockedContinuations:
    """`BlockedContinuation`はregistry由来の有限値である（ADR-0019 決定1）。

    checkpointはcommand列ではなく**このtableのID**を保存するため、ruleが表に無い継続を
    構築するとround-tripが成立しない。
    """

    def test_rules_build_no_continuation_outside_the_registry(self) -> None:
        """ruleが構築し得る継続はすべてtableのentryである。

        module globalを走査するのは、ruleのeffectがclosureで中身を覗けないためである。
        inlineで組み立てた継続がここで検出できる。
        """
        from claude_code_codex_review_loop.domain import _rules_workflow

        known = set(BLOCKED_CONTINUATIONS.values())
        found = [
            value
            for value in vars(_rules_workflow).values()
            if isinstance(value, BlockedContinuation)
        ]
        assert found, "継続が1つも見つからない（走査が壊れている）"
        assert all(value in known for value in found)

    def test_every_entry_is_used_by_a_rule(self) -> None:
        """使われないentryを残さない（IDは永続値なので語彙を実態と一致させる）。"""
        from claude_code_codex_review_loop.domain import _rules_workflow

        used = {
            value
            for value in vars(_rules_workflow).values()
            if isinstance(value, BlockedContinuation)
        }
        assert used == set(BLOCKED_CONTINUATIONS.values())

    def test_ids_are_stable_and_unique(self) -> None:
        """IDはcheckpointへ保存する永続値である（変更にはmigrationが要る）。"""
        assert set(BLOCKED_CONTINUATIONS) == {
            "REVIEW_BLOCKING",
            "CLARIFICATION",
            "FIX_RESULT",
            "RESUBMIT",
            "CI_CODE_FAILURE",
            "EXTERNAL_DEPENDENCY",
        }
        values = list(BLOCKED_CONTINUATIONS.values())
        assert len({id(value) for value in values}) == len(values)

    def test_every_continuation_resumes_into_a_valid_state(self) -> None:
        """継続のresume_stateとawaitingの組はC-01の不変条件を満たす。"""
        for continuation in BLOCKED_CONTINUATIONS.values():
            MachineState(state=continuation.resume_state, awaiting=continuation.awaiting)

    def test_continuations_are_distinguishable(self) -> None:
        """IDの逆引きが一意である（同値な2 entryがあるとwriterが選べない）。"""
        values = list(BLOCKED_CONTINUATIONS.values())
        assert len(values) == len(set(values))
