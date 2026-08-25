# SPDX-License-Identifier: Apache-2.0
"""integrity violationの検出・incident監査・RECORD_INTEGRITY blockの遷移rule。

検出を受理する全経路で承認を即時・冪等に失効させ（節5.4）、集合追加は常にunion、
terminalへは全violationが検証済みincident recordへ含まれた後にのみ遷移する。
RECORD_INTEGRITYのblockはgeneric fallbackを受理しない（fail closed）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from . import events as ev
from ._ruledefs import (
    BindingMatch,
    BlockKind,
    Effect,
    Match,
    PendingMatch,
    ProcedureKind,
    Rule,
)
from .commands import (
    CodexPurpose,
    Command,
    HaltRun,
    InvalidateApprovals,
    PersistRecord,
    QueryMergeOutcome,
    RecordIntegrityIncident,
    RequestCodexReview,
)
from .states import ACTIVE_STATES, RESUMABLE_STATES, TERMINAL_STATES, State
from .values import (
    Awaiting,
    ExternalDependencyBlock,
    HaltingForBlockProcedure,
    IncidentCoverage,
    IncidentTarget,
    IntegrityEvidenceRef,
    MachineState,
    PendingRecord,
    Progress,
    ProgressBlock,
    RecordingIncidentProcedure,
    RecordIntegrityBlock,
    RecordKind,
    canonicalize_integrity,
)

_S = State
_A = Awaiting
_NON_TERMINAL = frozenset(State) - TERMINAL_STATES
_ABSENT = frozenset({PendingMatch.ABSENT})
_MATCHED = frozenset({PendingMatch.MATCH})
_MERGE_OUTCOME = frozenset({_A.MERGE_OUTCOME_EXECUTE, _A.MERGE_OUTCOME_CANCEL, _A.MERGE_OUTCOME_FAILURE})
_INVALIDATE = (InvalidateApprovals(),)


def _union_deferred(ms: MachineState, evidence: IntegrityEvidenceRef) -> tuple[IntegrityEvidenceRef, ...]:
    return canonicalize_integrity(ms.deferred_integrity + (evidence,))


def _detect_union_effect(extra: tuple[Command, ...]) -> Effect:
    """検出evidenceをdeferred集合へunionし、承認を即時失効させる（状態・手続きは維持）。"""

    def effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
        e = cast(ev.RecordIntegrityViolationDetected, event)
        return replace(ms, deferred_integrity=_union_deferred(ms, e.evidence)), _INVALIDATE + extra

    return effect


def _detect_direct_block_effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    """resumable state / preconditions段階の検出: 直接BLOCKEDへ（旧resume情報を破棄）。"""
    e = cast(ev.RecordIntegrityViolationDetected, event)
    return (
        MachineState(state=_S.BLOCKED, block=RecordIntegrityBlock(violations=(e.evidence,))),
        _INVALIDATE,
    )


def _detect_halt_gate_effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    """active stateの検出: process停止完了までBLOCKEDにしない（halt gate）。"""
    e = cast(ev.RecordIntegrityViolationDetected, event)
    block = RecordIntegrityBlock(violations=(e.evidence,))
    return (
        replace(
            ms,
            procedure=HaltingForBlockProcedure(block=block, attempt_binding=e.evidence.binding),
            awaiting=None,
            pending_record=None,
            deferred_integrity=(),
        ),
        _INVALIDATE + (HaltRun(e.evidence.binding),),
    )


def _detect_blocked_union_effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    """RECORD_INTEGRITY block滞在中の検出: blockのviolation集合へunion（silent lossを作らない）。"""
    e = cast(ev.RecordIntegrityViolationDetected, event)
    block = cast(RecordIntegrityBlock, ms.block)
    merged = RecordIntegrityBlock(violations=canonicalize_integrity(block.violations + (e.evidence,)))
    return replace(ms, block=merged), _INVALIDATE


def _detect_halt_gate_union_effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    """halt gate中の追加検出: blockのviolation集合へunion。"""
    e = cast(ev.RecordIntegrityViolationDetected, event)
    procedure = cast(HaltingForBlockProcedure, ms.procedure)
    merged = RecordIntegrityBlock(violations=canonicalize_integrity(procedure.block.violations + (e.evidence,)))
    # attempt bindingは保つ（集合だけ伸ばす。発行済みの停止を別attemptへ差し替えない）
    return replace(ms, procedure=replace(procedure, block=merged)), _INVALIDATE


DETECTION_RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="I-D1",
        section="integrity",
        description="incident記録中の追加検出 -> 集合へunion + 即時失効（記録の直列化が処理）",
        match=Match(
            states=_NON_TERMINAL,
            event_type=ev.RecordIntegrityViolationDetected,
            procedures=frozenset({ProcedureKind.RECORDING_INCIDENT}),
        ),
        effect=_detect_union_effect(()),
        to_state="同一state",
        command_names=("InvalidateApprovals",),
    ),
    Rule(
        rule_id="I-D2",
        section="integrity",
        description="cancel手続き中の検出 -> 集合へunion + 即時失効（停止処理を継続）",
        match=Match(
            states=_NON_TERMINAL - {_S.MERGING},
            event_type=ev.RecordIntegrityViolationDetected,
            procedures=frozenset({ProcedureKind.CANCELLING}),
        ),
        effect=_detect_union_effect(()),
        to_state="同一state",
        command_names=("InvalidateApprovals",),
    ),
    Rule(
        rule_id="I-D3",
        section="integrity",
        description="MERGINGのoutcome段階の検出 -> union + 即時失効 + 照会継続（outcome確定を最優先）",
        match=Match(
            states=frozenset({_S.MERGING}),
            event_type=ev.RecordIntegrityViolationDetected,
            awaiting=_MERGE_OUTCOME,
        ),
        effect=_detect_union_effect((QueryMergeOutcome(),)),
        to_state=_S.MERGING.value,
        command_names=("InvalidateApprovals", "QueryMergeOutcome"),
    ),
    Rule(
        rule_id="I-D4",
        section="integrity",
        description="MERGINGのpreconditions段階の検出 -> mergeを実行せず安全停止（BLOCKED）",
        match=Match(
            states=frozenset({_S.MERGING}),
            event_type=ev.RecordIntegrityViolationDetected,
            awaiting=frozenset({_A.MERGE_PRECONDITIONS}),
        ),
        effect=_detect_direct_block_effect,
        to_state=_S.BLOCKED.value,
        command_names=("InvalidateApprovals",),
    ),
    Rule(
        rule_id="I-D5",
        section="integrity",
        description="active stateの検出 -> halt gate（停止完了までBLOCKEDにしない）",
        match=Match(
            states=ACTIVE_STATES - {_S.MERGING},
            event_type=ev.RecordIntegrityViolationDetected,
        ),
        effect=_detect_halt_gate_effect,
        to_state="同一state（halt gate）",
        command_names=("InvalidateApprovals", "HaltRun"),
    ),
    Rule(
        rule_id="I-D5u",
        section="integrity",
        description="halt gate中の追加検出 -> block集合へunion + 即時失効",
        match=Match(
            states=ACTIVE_STATES - {_S.MERGING},
            event_type=ev.RecordIntegrityViolationDetected,
            procedures=frozenset({ProcedureKind.HALTING_FOR_BLOCK}),
        ),
        effect=_detect_halt_gate_union_effect,
        to_state="同一state（halt gate）",
        command_names=("InvalidateApprovals",),
    ),
    Rule(
        rule_id="I-D6",
        section="integrity",
        description="resumable stateの検出 -> 直接BLOCKED（旧resume情報を破棄）",
        match=Match(
            states=RESUMABLE_STATES - {_S.BLOCKED},
            event_type=ev.RecordIntegrityViolationDetected,
        ),
        effect=_detect_direct_block_effect,
        to_state=_S.BLOCKED.value,
        command_names=("InvalidateApprovals",),
    ),
    Rule(
        rule_id="I-D7",
        section="integrity",
        description="RECORD_INTEGRITY block滞在中の検出 -> block集合へunion + 即時失効（同一bindingは冪等）",
        match=Match(
            states=frozenset({_S.BLOCKED}),
            event_type=ev.RecordIntegrityViolationDetected,
            block_kinds=frozenset({BlockKind.RECORD_INTEGRITY}),
        ),
        effect=_detect_blocked_union_effect,
        to_state=_S.BLOCKED.value,
        command_names=("InvalidateApprovals",),
    ),
    Rule(
        rule_id="I-D8",
        section="integrity",
        description="PROGRESS / EXTERNAL_DEPENDENCY block滞在中の検出 -> RECORD_INTEGRITY blockへ（旧blockを破棄）",
        match=Match(
            states=frozenset({_S.BLOCKED}),
            event_type=ev.RecordIntegrityViolationDetected,
            block_kinds=frozenset({BlockKind.PROGRESS, BlockKind.EXTERNAL_DEPENDENCY}),
        ),
        effect=_detect_direct_block_effect,
        to_state=_S.BLOCKED.value,
        command_names=("InvalidateApprovals",),
    ),
)


# ---------------------------------------------------------------------------
# incident監査記録（canonical record gateを通過した後にのみterminalへ）
# ---------------------------------------------------------------------------


def _incident_produced_effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    e = cast(ev.RecordProduced, event)
    pending = PendingRecord(kind=e.kind, binding=e.binding, source_state=ms.state)
    return replace(ms, pending_record=pending), (PersistRecord(e.kind, e.binding),)


def _incident_complete_effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    procedure = cast(RecordingIncidentProcedure, ms.procedure)
    target = _S.MERGED if procedure.target is IncidentTarget.MERGED else _S.CANCELLED
    return MachineState(state=target), ()


def _incident_remainder_effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    e = cast(ev.IntegrityIncidentVerified, event)
    procedure = cast(RecordingIncidentProcedure, ms.procedure)
    recorded = set(e.recorded_bindings)
    remaining = tuple(ref for ref in ms.deferred_integrity if ref.binding not in recorded)
    bindings = tuple(ref.binding for ref in remaining)
    return (
        replace(ms, pending_record=None, deferred_integrity=remaining),
        (RecordIntegrityIncident(violation_bindings=bindings, audit=procedure.audit),),
    )


INCIDENT_RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="I-P",
        section="incident",
        description="INTEGRITY_INCIDENTのPRODUCED -> 冪等persist（incident記録の永続化待ちへ）",
        match=Match(
            states=_NON_TERMINAL,
            event_type=ev.RecordProduced,
            procedures=frozenset({ProcedureKind.RECORDING_INCIDENT}),
            pending=_ABSENT,
            record_kinds=frozenset({RecordKind.INTEGRITY_INCIDENT}),
        ),
        effect=_incident_produced_effect,
        to_state="同一state",
        command_names=("PersistRecord",),
    ),
    Rule(
        rule_id="I-VC",
        section="incident",
        description="incident record検証（coverage = COMPLETE）-> incident_targetのterminalへ遷移",
        match=Match(
            states=_NON_TERMINAL,
            event_type=ev.IntegrityIncidentVerified,
            procedures=frozenset({ProcedureKind.RECORDING_INCIDENT}),
            pending=_MATCHED,
            coverage=frozenset({IncidentCoverage.COMPLETE}),
        ),
        effect=_incident_complete_effect,
        to_state="incident_target（MERGED / CANCELLED）",
        command_names=(),
    ),
    Rule(
        rule_id="I-VR",
        section="incident",
        description="incident record検証（coverage = REMAINDER）-> 残余で作成依頼を再発行（直列化）",
        match=Match(
            states=_NON_TERMINAL,
            event_type=ev.IntegrityIncidentVerified,
            procedures=frozenset({ProcedureKind.RECORDING_INCIDENT}),
            pending=_MATCHED,
            coverage=frozenset({IncidentCoverage.REMAINDER}),
        ),
        effect=_incident_remainder_effect,
        to_state="同一state（incident記録の継続）",
        command_names=("RecordIntegrityIncident",),
    ),
)


# ---------------------------------------------------------------------------
# MERGINGのoutcome確定（deferredあり。#46〜#49）
# ---------------------------------------------------------------------------


def _outcome_to_incident(target: IncidentTarget) -> Effect:
    def effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
        bindings = tuple(ref.binding for ref in ms.deferred_integrity)
        return (
            replace(ms, procedure=RecordingIncidentProcedure(target=target, audit=None), awaiting=None),
            (RecordIntegrityIncident(violation_bindings=bindings, audit=None),),
        )

    return effect


def _outcome_failure_to_block(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    """failure起点の未実行確認（deferredあり）: MERGE_FAILEDではなくRECORD_INTEGRITYのBLOCKEDへ。"""
    block = RecordIntegrityBlock(violations=ms.deferred_integrity)
    return MachineState(state=_S.BLOCKED, block=block), ()


MERGE_INTEGRITY_RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="I-46",
        section="incident",
        description="merge完了の確認（deferredあり）-> incident記録へ（MERGEDへは検証後にのみ進む）",
        match=Match(
            states=frozenset({_S.MERGING}),
            event_type=ev.MergeConfirmed,
            awaiting=_MERGE_OUTCOME,
            pending=_ABSENT,
            deferred_nonempty=True,
        ),
        effect=_outcome_to_incident(IncidentTarget.MERGED),
        to_state="MERGING（incident記録）",
        command_names=("RecordIntegrityIncident",),
    ),
    Rule(
        rule_id="I-47",
        section="incident",
        description="merge未実行の確認（cancel起点、deferredあり）-> incident記録へ（CANCELLEDへは検証後）",
        match=Match(
            states=frozenset({_S.MERGING}),
            event_type=ev.MergeNotExecutedConfirmed,
            awaiting=frozenset({_A.MERGE_OUTCOME_CANCEL}),
            pending=_ABSENT,
            deferred_nonempty=True,
        ),
        effect=_outcome_to_incident(IncidentTarget.CANCELLED),
        to_state="MERGING（incident記録）",
        command_names=("RecordIntegrityIncident",),
    ),
    Rule(
        rule_id="I-48",
        section="incident",
        description="merge未実行の確認（failure起点、deferredあり）-> RECORD_INTEGRITYのBLOCKED（gate迂回禁止）",
        match=Match(
            states=frozenset({_S.MERGING}),
            event_type=ev.MergeNotExecutedConfirmed,
            awaiting=frozenset({_A.MERGE_OUTCOME_FAILURE}),
            pending=_ABSENT,
            deferred_nonempty=True,
        ),
        effect=_outcome_failure_to_block,
        to_state=_S.BLOCKED.value,
        command_names=(),
    ),
    Rule(
        rule_id="I-49",
        section="incident",
        description="merge成否不明（deferredあり）-> 照会のみ継続（成否不明をintegrityで上書きしない）",
        match=Match(
            states=frozenset({_S.MERGING}),
            event_type=ev.MergeOutcomeUnknown,
            awaiting=_MERGE_OUTCOME,
            pending=_ABSENT,
            deferred_nonempty=True,
        ),
        effect=lambda ms, event: (ms, (QueryMergeOutcome(),)),
        to_state=_S.MERGING.value,
        command_names=("QueryMergeOutcome",),
    ),
)


# ---------------------------------------------------------------------------
# halt gateの完了とRECORD_INTEGRITY blockの解消
# ---------------------------------------------------------------------------


def _halt_completed_effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    procedure = cast(HaltingForBlockProcedure, ms.procedure)
    return MachineState(state=_S.BLOCKED, block=procedure.block), ()


def _replay_continuation(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    """保存された本来の継続を1回だけ再現し、blockを消去する。"""
    # blockの型はguard（block_kinds）で限定済み
    block = cast(ProgressBlock | ExternalDependencyBlock, ms.block)
    cont = block.continuation
    return MachineState(state=cont.resume_state, awaiting=cont.awaiting), cont.commands


def _integrity_exit_effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    return (
        MachineState(state=_S.RUNNING_REVIEW, awaiting=_A.CODEX_CODE_REVIEW),
        (InvalidateApprovals(), RequestCodexReview(CodexPurpose.CODE_REVIEW)),
    )


BLOCK_RESOLUTION_RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="B-HC",
        section="integrity",
        description="halt gateの停止完了（binding一致）-> BLOCKED（RECORD_INTEGRITY）",
        match=Match(
            states=ACTIVE_STATES - {_S.MERGING},
            event_type=ev.BlockHaltCompleted,
            procedures=frozenset({ProcedureKind.HALTING_FOR_BLOCK}),
            binding=frozenset({BindingMatch.MATCH}),
        ),
        effect=_halt_completed_effect,
        to_state=_S.BLOCKED.value,
        command_names=(),
    ),
    Rule(
        rule_id="B-LR",
        section="block",
        description="limit引き上げの検証（完全binding一致）-> 保存した継続を1回だけ再現",
        match=Match(
            states=frozenset({_S.BLOCKED}),
            event_type=ev.BlockResolvedLimitRaised,
            pending=_ABSENT,
            binding=frozenset({BindingMatch.MATCH}),
            block_kinds=frozenset({BlockKind.PROGRESS}),
            block_reasons=frozenset({Progress.LIMIT_REACHED}),
        ),
        effect=_replay_continuation,
        to_state="continuationのresume_state",
        command_names=("保存したcommand列",),
    ),
    Rule(
        rule_id="B-IV1",
        section="block",
        description="膠着解消のuser-input record（2経路。完全binding一致）-> 保存した継続を1回だけ再現",
        match=Match(
            states=frozenset({_S.BLOCKED}),
            event_type=ev.BlockResolvedIntervention,
            pending=frozenset({PendingMatch.MATCH, PendingMatch.ABSENT}),
            binding=frozenset({BindingMatch.MATCH}),
            block_kinds=frozenset({BlockKind.PROGRESS}),
            block_reasons=frozenset({Progress.NO_PROGRESS}),
        ),
        effect=_replay_continuation,
        to_state="continuationのresume_state",
        command_names=("保存したcommand列",),
    ),
    Rule(
        rule_id="B-IV2",
        section="block",
        description="外部依存解消のuser-input record（2経路。完全binding一致）-> 保存した継続を1回だけ再現",
        match=Match(
            states=frozenset({_S.BLOCKED}),
            event_type=ev.BlockResolvedIntervention,
            pending=frozenset({PendingMatch.MATCH, PendingMatch.ABSENT}),
            binding=frozenset({BindingMatch.MATCH}),
            block_kinds=frozenset({BlockKind.EXTERNAL_DEPENDENCY}),
        ),
        effect=_replay_continuation,
        to_state="continuationのresume_state",
        command_names=("保存したcommand列",),
    ),
    Rule(
        rule_id="B-RS",
        section="block",
        description="整合性復元と同一chain再検証（対象binding一致）-> 承認失効 + fresh review",
        match=Match(
            states=frozenset({_S.BLOCKED}),
            event_type=ev.IntegrityRestoredValidated,
            pending=_ABSENT,
            binding=frozenset({BindingMatch.MATCH}),
            block_kinds=frozenset({BlockKind.RECORD_INTEGRITY}),
        ),
        effect=_integrity_exit_effect,
        to_state=_S.RUNNING_REVIEW.value,
        command_names=("InvalidateApprovals", "RequestCodexReview"),
    ),
    Rule(
        rule_id="B-SV",
        section="block",
        description="salvageによる新baseline確立（対象binding一致。供給はPhase 14）-> fresh review",
        match=Match(
            states=frozenset({_S.BLOCKED}),
            event_type=ev.IntegritySalvageEstablished,
            pending=_ABSENT,
            binding=frozenset({BindingMatch.MATCH}),
            block_kinds=frozenset({BlockKind.RECORD_INTEGRITY}),
        ),
        effect=_integrity_exit_effect,
        to_state=_S.RUNNING_REVIEW.value,
        command_names=("InvalidateApprovals", "RequestCodexReview"),
    ),
)


INTEGRITY_RULES: tuple[Rule, ...] = DETECTION_RULES + INCIDENT_RULES + MERGE_INTEGRITY_RULES + BLOCK_RESOLUTION_RULES
