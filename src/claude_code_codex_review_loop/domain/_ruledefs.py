# SPDX-License-Identifier: Apache-2.0
"""遷移ruleの定義形式とguard discriminatorの導出。

ruleのmatch部は有限のtyped discriminatorのみで構成され、property testが全数列挙で
一意性（0件または1件）を機械検証する（AC-C01-08 / R1）。effect部は純粋関数で、
宣言summary（to_state / command名）との一致もtestで検証する（AC-C01-01）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, unique

from . import events as ev
from .commands import (
    CheckCi,
    CodexPurpose,
    Command,
    GenerateReport,
    HostAction,
    QueryMergeOutcome,
    RequestCodexReview,
    RequestHostAction,
    VerifyMergePreconditions,
)
from .states import State
from .values import (
    Awaiting,
    BlockResolutionEvidence,
    Budget,
    CancellingProcedure,
    ExternalDependencyBlock,
    HaltingForBlockProcedure,
    IncidentCoverage,
    MachineState,
    NormalProcedure,
    OpaqueBinding,
    OpaqueFingerprint,
    OpaqueSnapshot,
    Progress,
    ProgressBlock,
    RecordEvidence,
    RecordingIncidentProcedure,
    RecordIntegrityBlock,
    RecordKind,
)

# progress判定を運ぶevent（budget対応はBUDGET_EVENTSがdataとして持つ）
_REPORT_EVENT_TYPES = (
    ev.ReviewBlockingVerified,
    ev.CiCodeFailureVerified,
    ev.FixResultVerified,
    ev.ClarificationQuestionVerified,
    ev.VerdictResubmitVerified,
)


@unique
class ProcedureKind(Enum):
    """procedure直和の種別discriminator。"""

    NORMAL = "NORMAL"
    CANCELLING = "CANCELLING"
    HALTING_FOR_BLOCK = "HALTING_FOR_BLOCK"
    RECORDING_INCIDENT = "RECORDING_INCIDENT"


@unique
class PendingMatch(Enum):
    """pending_recordとevent evidenceの照合結果。

    evidenceを持つeventはABSENT / MATCH / MISMATCH、持たないeventはABSENT / PRESENTへ導出する。
    """

    ABSENT = "ABSENT"
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    PRESENT = "PRESENT"


@unique
class BindingMatch(Enum):
    """停止完了・解消evidenceのbinding照合結果。"""

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"


@unique
class BlockKind(Enum):
    """BlockContext直和の種別discriminator。"""

    PROGRESS = "PROGRESS"
    EXTERNAL_DEPENDENCY = "EXTERNAL_DEPENDENCY"
    RECORD_INTEGRITY = "RECORD_INTEGRITY"


@dataclass(frozen=True)
class GuardKey:
    """(MachineState, event)から決定論的に導出する有限guard値の組。"""

    procedure: ProcedureKind
    awaiting: Awaiting | None
    pending: PendingMatch
    record_kind: RecordKind | None
    progress: Progress | None
    deferred_nonempty: bool
    binding: BindingMatch | None
    block_kind: BlockKind | None
    block_reason: Progress | None
    coverage: IncidentCoverage | None
    recovery_present: bool


Effect = Callable[[MachineState, "ev.Event"], tuple[MachineState, tuple[Command, ...]]]


@dataclass(frozen=True)
class Match:
    """ruleのmatch部（純粋data）。Noneの成分は任意一致。"""

    states: frozenset[State]
    event_type: type
    procedures: frozenset[ProcedureKind] = frozenset({ProcedureKind.NORMAL})
    awaiting: frozenset[Awaiting | None] | None = None
    pending: frozenset[PendingMatch] | None = None
    record_kinds: frozenset[RecordKind] | None = None
    progress: frozenset[Progress] | None = None
    deferred_nonempty: bool | None = None
    binding: frozenset[BindingMatch] | None = None
    block_kinds: frozenset[BlockKind] | None = None
    block_reasons: frozenset[Progress] | None = None
    coverage: frozenset[IncidentCoverage] | None = None
    recovery_present: bool | None = None

    def matches(self, state: State, event: ev.Event, key: GuardKey) -> bool:
        return (
            state in self.states
            and type(event) is self.event_type
            and key.procedure in self.procedures
            and (self.awaiting is None or key.awaiting in self.awaiting)
            and (self.pending is None or key.pending in self.pending)
            and (self.record_kinds is None or key.record_kind in self.record_kinds)
            and (self.progress is None or key.progress in self.progress)
            and (self.deferred_nonempty is None or key.deferred_nonempty == self.deferred_nonempty)
            and (self.binding is None or key.binding in self.binding)
            and (self.block_kinds is None or key.block_kind in self.block_kinds)
            and (self.block_reasons is None or key.block_reason in self.block_reasons)
            and (self.coverage is None or key.coverage in self.coverage)
            and (self.recovery_present is None or key.recovery_present == self.recovery_present)
        )


@dataclass(frozen=True)
class Rule:
    """遷移rule。matchは純粋data、effectは純粋関数、to_state / command_namesは生成表用の宣言。"""

    rule_id: str
    section: str
    description: str
    match: Match
    effect: Effect
    to_state: str
    command_names: tuple[str, ...]


# ---------------------------------------------------------------------------
# guard discriminatorの導出（すべて等価比較のみ）
# ---------------------------------------------------------------------------


def procedure_kind(ms: MachineState) -> ProcedureKind:
    if isinstance(ms.procedure, CancellingProcedure):
        return ProcedureKind.CANCELLING
    if isinstance(ms.procedure, HaltingForBlockProcedure):
        return ProcedureKind.HALTING_FOR_BLOCK
    if isinstance(ms.procedure, RecordingIncidentProcedure):
        return ProcedureKind.RECORDING_INCIDENT
    return ProcedureKind.NORMAL


def _pending_vs_evidence(ms: MachineState, record: RecordEvidence | None) -> PendingMatch:
    if ms.pending_record is None:
        return PendingMatch.ABSENT
    if record is not None and ms.pending_record.kind is record.kind and ms.pending_record.binding == record.binding:
        return PendingMatch.MATCH
    return PendingMatch.MISMATCH


def _derive_pending(ms: MachineState, event: ev.Event) -> PendingMatch:
    if isinstance(event, ev._VerifiedEvent):
        return _pending_vs_evidence(ms, event.evidence)
    if isinstance(event, ev.BlockResolvedIntervention):
        return _pending_vs_evidence(ms, event.resolution.record)
    if ms.pending_record is None:
        return PendingMatch.ABSENT
    return PendingMatch.PRESENT


def _block_binding(block: ProgressBlock | ExternalDependencyBlock | RecordIntegrityBlock) -> OpaqueBinding:
    if isinstance(block, RecordIntegrityBlock):
        return block.representative_binding
    return block.binding


def _resolution_matches(ms: MachineState, res: BlockResolutionEvidence) -> BindingMatch:
    """解消evidenceと現在のblockの完全一致照合（該当しないkindのfieldはNoneどうしの一致）。

    RECORD_INTEGRITYでは現在のviolation集合全体（canonical order）との一致を要求する。
    集合が拡大した後の旧evidence（stale evidence）は一致しない。
    """
    block = ms.block
    if block is None:
        return BindingMatch.MISMATCH
    if res.target_block_binding != _block_binding(block) or res.head != block.head:
        return BindingMatch.MISMATCH
    expected: tuple[Progress | None, Budget | None, OpaqueSnapshot | None, OpaqueFingerprint | None]
    if isinstance(block, ProgressBlock):
        expected = (block.reason, block.budget, block.counter_snapshot, block.fingerprint)
    else:
        expected = (None, None, None, None)
    if (res.reason, res.budget, res.counter_snapshot, res.fingerprint) != expected:
        return BindingMatch.MISMATCH
    if isinstance(block, RecordIntegrityBlock):
        if res.violation_bindings != tuple(ref.binding for ref in block.violations):
            return BindingMatch.MISMATCH
    elif res.violation_bindings is not None:
        return BindingMatch.MISMATCH
    return BindingMatch.MATCH


def _derive_binding(ms: MachineState, event: ev.Event) -> BindingMatch | None:
    if isinstance(event, ev.CancellationCompleted):
        if isinstance(ms.procedure, CancellingProcedure):
            matched = event.attempt_binding is not None and event.attempt_binding == ms.procedure.attempt_binding
        elif isinstance(ms.procedure, NormalProcedure):
            matched = event.emergency_evidence is not None
        else:
            matched = False
        return BindingMatch.MATCH if matched else BindingMatch.MISMATCH
    if isinstance(event, ev.BlockHaltCompleted):
        if isinstance(ms.procedure, HaltingForBlockProcedure):
            matched = event.attempt_binding == ms.procedure.attempt_binding
        else:
            matched = False
        return BindingMatch.MATCH if matched else BindingMatch.MISMATCH
    if isinstance(
        event,
        (
            ev.BlockResolvedLimitRaised,
            ev.BlockResolvedIntervention,
            ev.IntegrityRestoredValidated,
            ev.IntegritySalvageEstablished,
        ),
    ):
        return _resolution_matches(ms, event.resolution)
    return None


def _derive_coverage(ms: MachineState, event: ev.Event) -> IncidentCoverage | None:
    if not isinstance(event, ev.IntegrityIncidentVerified):
        return None
    deferred = {ref.binding for ref in ms.deferred_integrity}
    recorded = set(event.recorded_bindings)
    return IncidentCoverage.COMPLETE if deferred <= recorded else IncidentCoverage.REMAINDER


def derive_guard_key(ms: MachineState, event: ev.Event) -> GuardKey:
    """有限guard値の組を決定論的に導出する。opaque値は等価比較のみに使う。"""
    block_kind: BlockKind | None = None
    block_reason: Progress | None = None
    if isinstance(ms.block, ProgressBlock):
        block_kind = BlockKind.PROGRESS
        block_reason = ms.block.reason
    elif isinstance(ms.block, ExternalDependencyBlock):
        block_kind = BlockKind.EXTERNAL_DEPENDENCY
    elif isinstance(ms.block, RecordIntegrityBlock):
        block_kind = BlockKind.RECORD_INTEGRITY
    report = event.report if isinstance(event, _REPORT_EVENT_TYPES) else None
    return GuardKey(
        procedure=procedure_kind(ms),
        awaiting=ms.awaiting,
        pending=_derive_pending(ms, event),
        record_kind=event.kind if isinstance(event, ev.RecordProduced) else None,
        progress=report.progress if report is not None else None,
        deferred_nonempty=bool(ms.deferred_integrity),
        binding=_derive_binding(ms, event),
        block_kind=block_kind,
        block_reason=block_reason,
        coverage=_derive_coverage(ms, event),
        recovery_present=ms.recovery_to is not None,
    )


# ---------------------------------------------------------------------------
# 共有の対応表（registry由来の有限値）
# ---------------------------------------------------------------------------

# awaiting値に対応する発行済みcommandの再発行（resume時）。USER_INPUT系はcommandを伴わない。
AWAITING_COMMANDS: dict[Awaiting, tuple[Command, ...]] = {
    Awaiting.CODEX_CODE_REVIEW: (RequestCodexReview(CodexPurpose.CODE_REVIEW),),
    Awaiting.CODEX_CLARIFICATION: (RequestCodexReview(CodexPurpose.CLARIFICATION),),
    Awaiting.CODEX_DECISION_VERDICT: (RequestCodexReview(CodexPurpose.DECISION_VERDICT),),
    Awaiting.HOST_APPLY_FINDINGS: (RequestHostAction(HostAction.APPLY_FINDINGS),),
    Awaiting.HOST_DRAFT_DECISION_REQUEST: (RequestHostAction(HostAction.DRAFT_DECISION_REQUEST),),
    Awaiting.HOST_DRAFT_DECISION_BRIEF: (RequestHostAction(HostAction.DRAFT_DECISION_BRIEF),),
    Awaiting.HOST_RECORD_DECISION: (RequestHostAction(HostAction.RECORD_DECISION),),
    Awaiting.HOST_REVISE_DECISION_REQUEST: (RequestHostAction(HostAction.REVISE_DECISION_REQUEST),),
    Awaiting.HOST_ANSWER_GATE_QUESTION: (RequestHostAction(HostAction.ANSWER_GATE_QUESTION),),
    Awaiting.CI_RESULT: (CheckCi(),),
    Awaiting.REPORT: (GenerateReport(),),
    Awaiting.MERGE_PRECONDITIONS: (VerifyMergePreconditions(),),
    Awaiting.MERGE_OUTCOME_EXECUTE: (QueryMergeOutcome(),),
    Awaiting.MERGE_OUTCOME_CANCEL: (QueryMergeOutcome(),),
    Awaiting.MERGE_OUTCOME_FAILURE: (QueryMergeOutcome(),),
    Awaiting.USER_INPUT_DECISION: (),
    Awaiting.USER_INPUT_GATE: (),
    Awaiting.USER_INPUT_PERMISSION: (),
}

# 復帰先stateの駆動command表（FAILEDの優先順位3とpermission復帰が共有する）。
DRIVE_TABLE: dict[State, tuple[tuple[Command, ...], Awaiting]] = {
    State.RUNNING_REVIEW: ((RequestCodexReview(CodexPurpose.CODE_REVIEW),), Awaiting.CODEX_CODE_REVIEW),
    State.CHANGES_REQUESTED: ((RequestHostAction(HostAction.APPLY_FINDINGS),), Awaiting.HOST_APPLY_FINDINGS),
    State.APPLYING_FIXES: ((RequestHostAction(HostAction.APPLY_FINDINGS),), Awaiting.HOST_APPLY_FINDINGS),
    State.CLARIFYING_REVIEW: ((RequestCodexReview(CodexPurpose.CLARIFICATION),), Awaiting.CODEX_CLARIFICATION),
    State.REVIEWING_DECISION_REQUEST: (
        (RequestCodexReview(CodexPurpose.DECISION_VERDICT),),
        Awaiting.CODEX_DECISION_VERDICT,
    ),
    State.GENERATING_REPORT: ((GenerateReport(),), Awaiting.REPORT),
}

# progress budgetを消費・判定するevent（消費点はregistryのdataとして明示する。AC-C01-09）
BUDGET_EVENTS: dict[type, tuple[Budget, str]] = {
    ev.ReviewBlockingVerified: (Budget.REVIEW_ROUND, "消費（新しいfix roundの開始）"),
    ev.CiCodeFailureVerified: (Budget.REVIEW_ROUND, "消費（fix loop再開）"),
    ev.FixResultVerified: (Budget.REVIEW_ROUND, "判定のみ（同一round完了。二重計上しない）"),
    ev.ClarificationQuestionVerified: (Budget.CLARIFICATION_TURN, "消費（次turn）"),
    ev.VerdictResubmitVerified: (Budget.CLARIFICATION_TURN, "消費（同一fingerprintの共通counter）"),
}
