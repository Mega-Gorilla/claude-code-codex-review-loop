# SPDX-License-Identifier: Apache-2.0
"""merge transaction / cancel / 失敗 / resumeの遷移rule。

merge実行commandの発行経路は「preconditions一致eventの消費」の1 ruleに限る（節5.5）。
cancelは停止完了gateを通過した後にのみCANCELLEDへ入り（節5.3）、停止・記録の手続き中は
横断規則が対応commandの冪等再発行だけを返す（節5.3 / 5.4、判断11）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from . import events as ev
from ._ruledefs import (
    AWAITING_COMMANDS,
    DRIVE_TABLE,
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
    ExecuteMerge,
    HaltRun,
    InvalidateApprovals,
    PersistRecord,
    QueryMergeOutcome,
    RecordIntegrityIncident,
    RequestCodexReview,
    VerifyMergePreconditions,
)
from .states import ACTIVE_STATES, RESUMABLE_STATES, TERMINAL_STATES, State
from .values import (
    Awaiting,
    CancellingProcedure,
    HaltingForBlockProcedure,
    IncidentTarget,
    MachineState,
    PendingRecord,
    RecordingIncidentProcedure,
)

_S = State
_A = Awaiting
_NON_TERMINAL = frozenset(State) - TERMINAL_STATES
_ABSENT = frozenset({PendingMatch.ABSENT})
_MATCHED = frozenset({PendingMatch.MATCH})
_MERGE_OUTCOME = frozenset({_A.MERGE_OUTCOME_EXECUTE, _A.MERGE_OUTCOME_CANCEL, _A.MERGE_OUTCOME_FAILURE})
_ANY_AWAITING: frozenset[Awaiting | None] = frozenset(Awaiting)

# resume系event（手続き中は横断規則が処理する）
_RESUME_EVENTS: tuple[type, ...] = (
    ev.ResumeValidated,
    ev.ResumeFallbackRequired,
    ev.ResumeSameHeadValidated,
    ev.CiResumeRequested,
    ev.PermissionResumeValidated,
    ev.ReporterRetryRequested,
)


def _bare(state: State) -> MachineState:
    return MachineState(state=state)


def _fresh_review(ms: MachineState) -> tuple[MachineState, tuple[Command, ...]]:
    """承認失効 + fresh review（fallback / head変更の共通終端）。"""
    return (
        MachineState(state=_S.RUNNING_REVIEW, awaiting=_A.CODEX_CODE_REVIEW),
        (InvalidateApprovals(), RequestCodexReview(CodexPurpose.CODE_REVIEW)),
    )


# ---------------------------------------------------------------------------
# merge transaction（#32・#34〜#42）
# ---------------------------------------------------------------------------


def _to_merging(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    return (
        replace(ms, state=_S.MERGING, awaiting=_A.MERGE_PRECONDITIONS, pending_record=None),
        (VerifyMergePreconditions(),),
    )


def _merge_effect(to: State | None, awaiting_after: Awaiting | None, commands: tuple[Command, ...]) -> Effect:
    def effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
        if to is not None and to in TERMINAL_STATES:
            return _bare(to), commands
        return replace(ms, state=ms.state if to is None else to, awaiting=awaiting_after), commands

    return effect


def _cancel_query_effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    """MERGINGのcancelは結果照会を優先する（#41）。"""
    return replace(ms, awaiting=_A.MERGE_OUTCOME_CANCEL, pending_record=None), (QueryMergeOutcome(),)


MERGE_RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="M-32a",
        section="merge",
        description="merge承認record（経路1）-> preconditions再検証のみを発行",
        match=Match(
            states=frozenset({_S.READY_FOR_HUMAN_MERGE}), event_type=ev.MergeApprovalVerified, pending=_MATCHED
        ),
        effect=_to_merging,
        to_state=_S.MERGING.value,
        command_names=("VerifyMergePreconditions",),
    ),
    Rule(
        rule_id="M-32b",
        section="merge",
        description="merge承認record（経路2: GitHub直接comment）-> preconditions再検証のみを発行",
        match=Match(
            states=frozenset({_S.READY_FOR_HUMAN_MERGE}),
            event_type=ev.MergeApprovalVerified,
            awaiting=frozenset({_A.USER_INPUT_GATE}),
            pending=_ABSENT,
        ),
        effect=_to_merging,
        to_state=_S.MERGING.value,
        command_names=("VerifyMergePreconditions",),
    ),
    Rule(
        rule_id="M-34",
        section="merge",
        description="preconditions全一致 -> merge実行（ExecuteMergeの唯一の発行経路）",
        match=Match(
            states=frozenset({_S.MERGING}),
            event_type=ev.MergePreconditionsOk,
            awaiting=frozenset({_A.MERGE_PRECONDITIONS}),
            pending=_ABSENT,
        ),
        effect=_merge_effect(None, _A.MERGE_OUTCOME_EXECUTE, (ExecuteMerge(),)),
        to_state=_S.MERGING.value,
        command_names=("ExecuteMerge",),
    ),
    Rule(
        rule_id="M-35",
        section="merge",
        description="preconditions不一致（head変更以外）-> MERGE_FAILED",
        match=Match(
            states=frozenset({_S.MERGING}),
            event_type=ev.MergePreconditionMismatch,
            awaiting=frozenset({_A.MERGE_PRECONDITIONS}),
            pending=_ABSENT,
        ),
        effect=_merge_effect(_S.MERGE_FAILED, None, ()),
        to_state=_S.MERGE_FAILED.value,
        command_names=(),
    ),
    Rule(
        rule_id="M-36",
        section="merge",
        description="preconditions段階のhead変更 -> 承認失効 + fresh review",
        match=Match(
            states=frozenset({_S.MERGING}),
            event_type=ev.HeadChangedExternally,
            awaiting=frozenset({_A.MERGE_PRECONDITIONS}),
            pending=_ABSENT,
        ),
        effect=lambda ms, event: _fresh_review(ms),
        to_state=_S.RUNNING_REVIEW.value,
        command_names=("InvalidateApprovals", "RequestCodexReview"),
    ),
    Rule(
        rule_id="M-37",
        section="merge",
        description="merge完了の確認（deferredなし）-> MERGED",
        match=Match(
            states=frozenset({_S.MERGING}),
            event_type=ev.MergeConfirmed,
            awaiting=_MERGE_OUTCOME,
            pending=_ABSENT,
            deferred_nonempty=False,
        ),
        effect=_merge_effect(_S.MERGED, None, ()),
        to_state=_S.MERGED.value,
        command_names=(),
    ),
    Rule(
        rule_id="M-38",
        section="merge",
        description="merge未実行の確認（cancel起点、deferredなし）-> CANCELLED",
        match=Match(
            states=frozenset({_S.MERGING}),
            event_type=ev.MergeNotExecutedConfirmed,
            awaiting=frozenset({_A.MERGE_OUTCOME_CANCEL}),
            pending=_ABSENT,
            deferred_nonempty=False,
        ),
        effect=_merge_effect(_S.CANCELLED, None, ()),
        to_state=_S.CANCELLED.value,
        command_names=(),
    ),
    Rule(
        rule_id="M-39",
        section="merge",
        description="merge未実行の確認（failure起点、deferredなし）-> MERGE_FAILED",
        match=Match(
            states=frozenset({_S.MERGING}),
            event_type=ev.MergeNotExecutedConfirmed,
            awaiting=frozenset({_A.MERGE_OUTCOME_FAILURE}),
            pending=_ABSENT,
            deferred_nonempty=False,
        ),
        effect=_merge_effect(_S.MERGE_FAILED, None, ()),
        to_state=_S.MERGE_FAILED.value,
        command_names=(),
    ),
    Rule(
        rule_id="M-40",
        section="merge",
        description="merge成否不明（deferredなし）-> MERGE_FAILEDで安全停止",
        match=Match(
            states=frozenset({_S.MERGING}),
            event_type=ev.MergeOutcomeUnknown,
            awaiting=_MERGE_OUTCOME,
            pending=_ABSENT,
            deferred_nonempty=False,
        ),
        effect=_merge_effect(_S.MERGE_FAILED, None, ()),
        to_state=_S.MERGE_FAILED.value,
        command_names=(),
    ),
    Rule(
        rule_id="M-41a",
        section="merge",
        description="MERGING中のcancel（経路1）-> merge結果の照会を優先",
        match=Match(states=frozenset({_S.MERGING}), event_type=ev.UserCancelVerified, pending=_MATCHED),
        effect=_cancel_query_effect,
        to_state=_S.MERGING.value,
        command_names=("QueryMergeOutcome",),
    ),
    Rule(
        rule_id="M-41b",
        section="merge",
        description="MERGING中のcancel（経路2: GitHub直接comment）-> merge結果の照会を優先",
        match=Match(states=frozenset({_S.MERGING}), event_type=ev.UserCancelVerified, pending=_ABSENT),
        effect=_cancel_query_effect,
        to_state=_S.MERGING.value,
        command_names=("QueryMergeOutcome",),
    ),
    Rule(
        rule_id="M-42",
        section="merge",
        description="MERGING中のrun失敗 -> failure起点の結果照会",
        match=Match(
            states=frozenset({_S.MERGING}),
            event_type=ev.RunFailed,
            awaiting=_MERGE_OUTCOME | {_A.MERGE_PRECONDITIONS},
        ),
        effect=_merge_effect(None, _A.MERGE_OUTCOME_FAILURE, (QueryMergeOutcome(),)),
        to_state=_S.MERGING.value,
        command_names=("QueryMergeOutcome",),
    ),
    Rule(
        rule_id="M-SH",
        section="merge",
        description="MERGE_FAILEDの同一head・全条件再確認 -> merge gateへ復帰",
        match=Match(
            states=frozenset({_S.MERGE_FAILED}), event_type=ev.ResumeSameHeadValidated, pending=_ABSENT
        ),
        effect=lambda ms, event: (
            replace(ms, state=_S.READY_FOR_HUMAN_MERGE, awaiting=_A.USER_INPUT_GATE),
            (),
        ),
        to_state=_S.READY_FOR_HUMAN_MERGE.value,
        command_names=(),
    ),
    Rule(
        rule_id="M-HC",
        section="merge",
        description="MERGE_FAILEDでのhead変更 -> 承認失効 + fresh review",
        match=Match(states=frozenset({_S.MERGE_FAILED}), event_type=ev.HeadChangedExternally, pending=_ABSENT),
        effect=lambda ms, event: _fresh_review(ms),
        to_state=_S.RUNNING_REVIEW.value,
        command_names=("InvalidateApprovals", "RequestCodexReview"),
    ),
)


# ---------------------------------------------------------------------------
# cancel（2系統。停止完了gate）
# ---------------------------------------------------------------------------


def _start_cancel(consume_pending: bool) -> Effect:
    def effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
        e = cast(ev.UserCancelVerified, event)
        binding = e.evidence.binding
        pending = None if consume_pending else ms.pending_record
        return (
            replace(ms, procedure=CancellingProcedure(attempt_binding=binding), awaiting=None, pending_record=pending),
            (HaltRun(binding),),
        )

    return effect


def _cancel_completed_to_incident(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    """停止完了時にdeferredがあればincident記録へ切り替える（stale pendingは監査参照として保持）。"""
    audit = ms.pending_record
    bindings = tuple(ref.binding for ref in ms.deferred_integrity)
    return (
        replace(
            ms,
            procedure=RecordingIncidentProcedure(target=IncidentTarget.CANCELLED, audit=audit),
            pending_record=None,
        ),
        (RecordIntegrityIncident(violation_bindings=bindings, audit=audit),),
    )


CANCEL_RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="C-01",
        section="cancel",
        description="cancel intent検証（経路1）-> 停止commandのみを発行（新agentは起動しない）",
        match=Match(
            states=_NON_TERMINAL - {_S.MERGING}, event_type=ev.UserCancelVerified, pending=_MATCHED
        ),
        effect=_start_cancel(consume_pending=True),
        to_state="同一state（cancelling）",
        command_names=("HaltRun",),
    ),
    Rule(
        rule_id="C-02",
        section="cancel",
        description="cancel intent検証（経路2: GitHub直接comment。stale pendingは監査保持）-> 停止commandのみ",
        match=Match(
            states=_NON_TERMINAL - {_S.MERGING},
            event_type=ev.UserCancelVerified,
            pending=frozenset({PendingMatch.ABSENT, PendingMatch.MISMATCH}),
        ),
        effect=_start_cancel(consume_pending=False),
        to_state="同一state（cancelling）",
        command_names=("HaltRun",),
    ),
    Rule(
        rule_id="C-03",
        section="cancel",
        description="binding一致の停止完了（deferredなし）-> CANCELLED",
        match=Match(
            states=_NON_TERMINAL - {_S.MERGING},
            event_type=ev.CancellationCompleted,
            procedures=frozenset({ProcedureKind.CANCELLING}),
            binding=frozenset({BindingMatch.MATCH}),
            deferred_nonempty=False,
        ),
        effect=lambda ms, event: (_bare(_S.CANCELLED), ()),
        to_state=_S.CANCELLED.value,
        command_names=(),
    ),
    Rule(
        rule_id="C-04",
        section="cancel",
        description="binding一致の停止完了（deferredあり）-> incident記録へ切替（CANCELLEDへは検証後にのみ進む）",
        match=Match(
            states=_NON_TERMINAL - {_S.MERGING},
            event_type=ev.CancellationCompleted,
            procedures=frozenset({ProcedureKind.CANCELLING}),
            binding=frozenset({BindingMatch.MATCH}),
            deferred_nonempty=True,
        ),
        effect=_cancel_completed_to_incident,
        to_state="同一state（incident記録）",
        command_names=("RecordIntegrityIncident",),
    ),
    Rule(
        rule_id="C-05",
        section="cancel",
        description="緊急停止（run / checkpoint bind検証済み）-> CANCELLED",
        match=Match(
            states=_NON_TERMINAL - {_S.MERGING},
            event_type=ev.CancellationCompleted,
            procedures=frozenset({ProcedureKind.NORMAL}),
            binding=frozenset({BindingMatch.MATCH}),
            deferred_nonempty=False,
        ),
        effect=lambda ms, event: (_bare(_S.CANCELLED), ()),
        to_state=_S.CANCELLED.value,
        command_names=(),
    ),
)


# ---------------------------------------------------------------------------
# 失敗（EV_RUN_FAILED）
# ---------------------------------------------------------------------------


def _fail_to_recovery(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    return replace(ms, state=_S.FAILED, recovery_to=ms.state), ()


FAILURE_RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="F-01",
        section="failure",
        description="active stateのrun失敗 -> FAILED（recovery_to設定。pending / awaiting引継）",
        match=Match(
            states=ACTIVE_STATES - {_S.MERGING},
            event_type=ev.RunFailed,
            procedures=frozenset({ProcedureKind.NORMAL}),
        ),
        effect=_fail_to_recovery,
        to_state=_S.FAILED.value,
        command_names=(),
    ),
    Rule(
        rule_id="F-02",
        section="failure",
        description="resumable stateのrun失敗 -> 同一state維持（全付随値保持）",
        match=Match(
            states=RESUMABLE_STATES,
            event_type=ev.RunFailed,
            procedures=frozenset({ProcedureKind.NORMAL}),
        ),
        effect=lambda ms, event: (ms, ()),
        to_state="同一state",
        command_names=(),
    ),
)


# ---------------------------------------------------------------------------
# 横断規則: 手続き中の失敗・明示resumeは段階対応commandの冪等再発行のみ
# ---------------------------------------------------------------------------


def _reissue_halt_for_cancel(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    procedure = cast(CancellingProcedure, ms.procedure)
    return ms, (HaltRun(procedure.attempt_binding),)


def _reissue_halt_for_block(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    procedure = cast(HaltingForBlockProcedure, ms.procedure)
    return ms, (HaltRun(procedure.attempt_binding),)


def _reissue_incident_request(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    procedure = cast(RecordingIncidentProcedure, ms.procedure)
    bindings = tuple(ref.binding for ref in ms.deferred_integrity)
    return ms, (RecordIntegrityIncident(violation_bindings=bindings, audit=procedure.audit),)


def _reissue_persist(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    pending = cast(PendingRecord, ms.pending_record)
    return ms, (PersistRecord(pending.kind, pending.binding),)


def _procedure_rules() -> tuple[Rule, ...]:
    rules: list[Rule] = []
    for index, event_type in enumerate((ev.RunFailed, *_RESUME_EVENTS)):
        name = event_type.__name__
        rules.append(
            Rule(
                rule_id=f"X-C{index}",
                section="procedure",
                description=f"cancel手続き中の{name} -> 停止commandの冪等再発行のみ",
                match=Match(
                    states=_NON_TERMINAL - {_S.MERGING},
                    event_type=event_type,
                    procedures=frozenset({ProcedureKind.CANCELLING}),
                ),
                effect=_reissue_halt_for_cancel,
                to_state="同一state",
                command_names=("HaltRun",),
            )
        )
        rules.append(
            Rule(
                rule_id=f"X-H{index}",
                section="procedure",
                description=f"halt gate中の{name} -> 停止commandの冪等再発行のみ",
                match=Match(
                    states=ACTIVE_STATES - {_S.MERGING},
                    event_type=event_type,
                    procedures=frozenset({ProcedureKind.HALTING_FOR_BLOCK}),
                ),
                effect=_reissue_halt_for_block,
                to_state="同一state",
                command_names=("HaltRun",),
            )
        )
        rules.append(
            Rule(
                rule_id=f"X-I{index}a",
                section="procedure",
                description=f"incident作成前の{name} -> 作成依頼commandの冪等再発行のみ",
                match=Match(
                    states=_NON_TERMINAL,
                    event_type=event_type,
                    procedures=frozenset({ProcedureKind.RECORDING_INCIDENT}),
                    pending=_ABSENT,
                ),
                effect=_reissue_incident_request,
                to_state="同一state",
                command_names=("RecordIntegrityIncident",),
            )
        )
        rules.append(
            Rule(
                rule_id=f"X-I{index}b",
                section="procedure",
                description=f"incident永続化待ちの{name} -> 永続化commandの冪等再発行のみ",
                match=Match(
                    states=_NON_TERMINAL,
                    event_type=event_type,
                    procedures=frozenset({ProcedureKind.RECORDING_INCIDENT}),
                    pending=frozenset({PendingMatch.PRESENT}),
                ),
                effect=_reissue_persist,
                to_state="同一state",
                command_names=("PersistRecord",),
            )
        )
    return tuple(rules)


PROCEDURE_RULES: tuple[Rule, ...] = _procedure_rules()


# ---------------------------------------------------------------------------
# resume（手続きなし）
# ---------------------------------------------------------------------------


def _resume_pending(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    """partial turnの再開: source_stateへ戻り、同一bindingの永続化確認のみを再発行する。

    現在のstateで生成されたpending（FAILED内のUSER_CANCEL等）はstateを変えず、
    recovery_to等の復帰情報も保持する。
    """
    pending = cast(PendingRecord, ms.pending_record)
    recovery_to = ms.recovery_to if pending.source_state is ms.state else None
    return (
        replace(ms, state=pending.source_state, recovery_to=recovery_to),
        (PersistRecord(pending.kind, pending.binding),),
    )


def _resume_awaiting_failed(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    recovery_to = cast(State, ms.recovery_to)
    awaiting = cast(Awaiting, ms.awaiting)
    return replace(ms, state=recovery_to, recovery_to=None), AWAITING_COMMANDS[awaiting]


def _resume_awaiting_live(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    awaiting = cast(Awaiting, ms.awaiting)
    return ms, AWAITING_COMMANDS[awaiting]


def _resume_drive(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    recovery_to = cast(State, ms.recovery_to)
    commands, awaiting = DRIVE_TABLE[recovery_to]
    return replace(ms, state=recovery_to, awaiting=awaiting, recovery_to=None), commands


RESUME_RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="R-P",
        section="resume",
        description="pending保持中の明示resume -> 永続化確認の再発行のみ（次agentを起動しない）",
        match=Match(
            states=_NON_TERMINAL,
            event_type=ev.ResumeValidated,
            pending=frozenset({PendingMatch.PRESENT}),
        ),
        effect=_resume_pending,
        to_state="pendingのsource_state",
        command_names=("PersistRecord",),
    ),
    Rule(
        rule_id="R-A1",
        section="resume",
        description="FAILEDの明示resume（応答待ちあり）-> 復帰先で対応commandを再発行",
        match=Match(
            states=frozenset({_S.FAILED}),
            event_type=ev.ResumeValidated,
            awaiting=_ANY_AWAITING,
            pending=_ABSENT,
            recovery_present=True,
        ),
        effect=_resume_awaiting_failed,
        to_state="recovery_to",
        command_names=("awaiting対応command",),
    ),
    Rule(
        rule_id="R-A2",
        section="resume",
        description="応答待ち中の明示resume -> 対応commandの再発行のみ（MERGINGはorigin維持で照会 / 再検証のみ）",
        match=Match(
            states=_NON_TERMINAL - {_S.FAILED, _S.BLOCKED},
            event_type=ev.ResumeValidated,
            awaiting=_ANY_AWAITING,
            pending=_ABSENT,
        ),
        effect=_resume_awaiting_live,
        to_state="同一state",
        command_names=("awaiting対応command",),
    ),
    Rule(
        rule_id="R-D",
        section="resume",
        description="FAILEDの明示resume（pending / awaitingなし）-> recovery_toの駆動command",
        match=Match(
            states=frozenset({_S.FAILED}),
            event_type=ev.ResumeValidated,
            awaiting=frozenset({None}),
            pending=_ABSENT,
            recovery_present=True,
        ),
        effect=_resume_drive,
        to_state="recovery_to",
        command_names=("recovery_to対応の駆動command",),
    ),
    Rule(
        rule_id="R-B",
        section="resume",
        description="BLOCKEDの単純resume -> BLOCKED維持・commandなし（解消経路の提示のみ）",
        match=Match(
            states=frozenset({_S.BLOCKED}),
            event_type=ev.ResumeValidated,
            pending=_ABSENT,
        ),
        effect=lambda ms, event: (ms, ()),
        to_state=_S.BLOCKED.value,
        command_names=(),
    ),
    Rule(
        rule_id="R-F",
        section="resume",
        description="FAILEDのfallback resume -> 継続破棄・承認失効 + fresh review",
        match=Match(
            states=frozenset({_S.FAILED}),
            event_type=ev.ResumeFallbackRequired,
            recovery_present=True,
        ),
        effect=lambda ms, event: _fresh_review(ms),
        to_state=_S.RUNNING_REVIEW.value,
        command_names=("InvalidateApprovals", "RequestCodexReview"),
    ),
    Rule(
        rule_id="R-FB",
        section="resume",
        description="BLOCKED（PROGRESS / EXTERNAL_DEPENDENCY）のfallback -> 継続破棄・承認失効 + fresh review",
        match=Match(
            states=frozenset({_S.BLOCKED}),
            event_type=ev.ResumeFallbackRequired,
            block_kinds=frozenset({BlockKind.PROGRESS, BlockKind.EXTERNAL_DEPENDENCY}),
        ),
        effect=lambda ms, event: _fresh_review(ms),
        to_state=_S.RUNNING_REVIEW.value,
        command_names=("InvalidateApprovals", "RequestCodexReview"),
    ),
    Rule(
        rule_id="R-CI",
        section="resume",
        description="WAITING_CIの明示resume -> CI確認の再発行",
        match=Match(
            states=frozenset({_S.WAITING_CI}),
            event_type=ev.CiResumeRequested,
            awaiting=frozenset({None, _A.CI_RESULT}),
            pending=_ABSENT,
        ),
        effect=lambda ms, event: (
            replace(ms, awaiting=_A.CI_RESULT),
            AWAITING_COMMANDS[_A.CI_RESULT],
        ),
        to_state=_S.WAITING_CI.value,
        command_names=("CheckCi",),
    ),
    Rule(
        rule_id="R-RT",
        section="resume",
        description="REPORT_FAILEDのreporter再実行 -> report生成を再依頼",
        match=Match(
            states=frozenset({_S.REPORT_FAILED}),
            event_type=ev.ReporterRetryRequested,
            awaiting=frozenset({None}),
            pending=_ABSENT,
        ),
        effect=lambda ms, event: (
            replace(ms, state=_S.GENERATING_REPORT, awaiting=_A.REPORT),
            AWAITING_COMMANDS[_A.REPORT],
        ),
        to_state=_S.GENERATING_REPORT.value,
        command_names=("GenerateReport",),
    ),
)


RECOVERY_RULES: tuple[Rule, ...] = MERGE_RULES + CANCEL_RULES + FAILURE_RULES + PROCEDURE_RULES + RESUME_RULES
