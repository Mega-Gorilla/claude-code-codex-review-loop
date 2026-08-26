# SPDX-License-Identifier: Apache-2.0
"""main workflow系の遷移rule。

record PRODUCED規約（旧設計3.1〜3.3）、review / fix / clarification / decision / CI /
report / merge gateの各行（旧遷移表#1〜#33・#43）、bounded progressのblock進入
（progress共通規則）を定義する。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Final, cast

from . import events as ev
from ._ruledefs import BUDGET_EVENTS, DRIVE_TABLE, BlockKind, Effect, Match, PendingMatch, Rule
from .commands import (
    CheckCi,
    CodexPurpose,
    Command,
    GenerateReport,
    HostAction,
    InvalidateApprovals,
    PersistRecord,
    RequestCodexReview,
    RequestHostAction,
)
from .states import TERMINAL_STATES, State
from .values import (
    Awaiting,
    BlockedContinuation,
    ExternalDependencyBlock,
    MachineState,
    PendingRecord,
    Progress,
    ProgressBlock,
    RecordKind,
)

_NON_TERMINAL = frozenset(State) - TERMINAL_STATES
_MATCHED = frozenset({PendingMatch.MATCH})
_ABSENT = frozenset({PendingMatch.ABSENT})
_CONTINUE = frozenset({Progress.CONTINUE})
_BOUNDED = frozenset({Progress.LIMIT_REACHED, Progress.NO_PROGRESS})


def _names(commands: tuple[Command, ...]) -> tuple[str, ...]:
    return tuple(type(c).__name__ for c in commands)


def _advance(to: State | None, awaiting_after: Awaiting | None, commands: tuple[Command, ...]) -> Effect:
    """pendingを消費し、遷移先・awaiting・command列を設定する標準effect。"""

    def effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
        return (
            replace(ms, state=ms.state if to is None else to, awaiting=awaiting_after, pending_record=None),
            commands,
        )

    return effect


# ---------------------------------------------------------------------------
# record PRODUCED規約（awaiting消費 -> pending設定 -> 冪等persist）
# ---------------------------------------------------------------------------


def _produced_effect(keep_awaiting: bool) -> Effect:
    def effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
        e = cast(ev.RecordProduced, event)
        pending = PendingRecord(kind=e.kind, binding=e.binding, source_state=ms.state)
        awaiting = ms.awaiting if keep_awaiting else None
        return replace(ms, awaiting=awaiting, pending_record=pending), (PersistRecord(e.kind, e.binding),)

    return effect


def _produced_rule(
    rule_id: str,
    kind: RecordKind,
    states: frozenset[State],
    awaitings: frozenset[Awaiting | None],
) -> Rule:
    return Rule(
        rule_id=rule_id,
        section="record",
        description=f"{kind.value}のPRODUCED（awaiting消費、pending設定、冪等persist）",
        match=Match(
            states=states,
            event_type=ev.RecordProduced,
            awaiting=awaitings,
            pending=_ABSENT,
            record_kinds=frozenset({kind}),
        ),
        effect=_produced_effect(keep_awaiting=False),
        to_state="同一state",
        command_names=("PersistRecord",),
    )


_K = RecordKind
_A = Awaiting
_S = State

PRODUCED_RULES: tuple[Rule, ...] = (
    _produced_rule("P-01", _K.REVIEW_RESULT, frozenset({_S.RUNNING_REVIEW}), frozenset({_A.CODEX_CODE_REVIEW})),
    _produced_rule("P-02", _K.FIX_RESULT, frozenset({_S.APPLYING_FIXES}), frozenset({_A.HOST_APPLY_FINDINGS})),
    _produced_rule(
        "P-03", _K.CLARIFICATION_QUESTION, frozenset({_S.CHANGES_REQUESTED}), frozenset({_A.HOST_APPLY_FINDINGS})
    ),
    _produced_rule(
        "P-04", _K.CLARIFICATION_ANSWER, frozenset({_S.CLARIFYING_REVIEW}), frozenset({_A.CODEX_CLARIFICATION})
    ),
    _produced_rule("P-05", _K.DECISION_REQUEST, frozenset({_S.APPLYING_FIXES}), frozenset({_A.HOST_APPLY_FINDINGS})),
    _produced_rule(
        "P-06",
        _K.DECISION_REQUEST,
        frozenset({_S.REVIEWING_DECISION_REQUEST}),
        frozenset({_A.HOST_DRAFT_DECISION_REQUEST, _A.HOST_REVISE_DECISION_REQUEST}),
    ),
    _produced_rule(
        "P-07", _K.DECISION_VERDICT, frozenset({_S.REVIEWING_DECISION_REQUEST}), frozenset({_A.CODEX_DECISION_VERDICT})
    ),
    _produced_rule(
        "P-08", _K.DECISION_BRIEF, frozenset({_S.REVIEWING_DECISION_REQUEST}), frozenset({_A.HOST_DRAFT_DECISION_BRIEF})
    ),
    _produced_rule(
        "P-09", _K.DECISION_RECORD, frozenset({_S.REVIEWING_DECISION_REQUEST}), frozenset({_A.HOST_RECORD_DECISION})
    ),
    _produced_rule(
        "P-10", _K.EXTERNAL_DEPENDENCY, frozenset({_S.APPLYING_FIXES}), frozenset({_A.HOST_APPLY_FINDINGS})
    ),
    _produced_rule("P-11", _K.PERMISSION_BLOCK, frozenset({_S.RUNNING_REVIEW}), frozenset({_A.CODEX_CODE_REVIEW})),
    _produced_rule("P-12", _K.PERMISSION_BLOCK, frozenset({_S.APPLYING_FIXES}), frozenset({_A.HOST_APPLY_FINDINGS})),
    _produced_rule("P-13", _K.CI_TIMEOUT, frozenset({_S.WAITING_CI}), frozenset({_A.CI_RESULT})),
    _produced_rule("P-14", _K.CI_CODE_FAILURE, frozenset({_S.WAITING_CI}), frozenset({_A.CI_RESULT})),
    _produced_rule("P-15", _K.FINAL_REPORT, frozenset({_S.GENERATING_REPORT}), frozenset({_A.REPORT})),
    _produced_rule(
        "P-16", _K.GATE_ANSWER, frozenset({_S.READY_FOR_HUMAN_MERGE}), frozenset({_A.HOST_ANSWER_GATE_QUESTION})
    ),
    _produced_rule(
        "P-17", _K.USER_DECISION, frozenset({_S.AWAITING_USER_DECISION}), frozenset({_A.USER_INPUT_DECISION})
    ),
    _produced_rule(
        "P-18", _K.GATE_QUESTION, frozenset({_S.READY_FOR_HUMAN_MERGE}), frozenset({_A.USER_INPUT_GATE})
    ),
    _produced_rule("P-19", _K.GATE_CHANGES, frozenset({_S.READY_FOR_HUMAN_MERGE}), frozenset({_A.USER_INPUT_GATE})),
    _produced_rule(
        "P-20", _K.MERGE_APPROVAL, frozenset({_S.READY_FOR_HUMAN_MERGE}), frozenset({_A.USER_INPUT_GATE})
    ),
    # BLOCK_INTERVENTIONのPRODUCEDは、blockのkindが解消を許可する場合のみ受理する
    Rule(
        rule_id="P-22",
        section="record",
        description="BLOCK_INTERVENTIONのPRODUCED（膠着block。NO_PROGRESSのみ解消を許可）",
        match=Match(
            states=frozenset({_S.BLOCKED}),
            event_type=ev.RecordProduced,
            pending=_ABSENT,
            record_kinds=frozenset({_K.BLOCK_INTERVENTION}),
            block_kinds=frozenset({BlockKind.PROGRESS}),
            block_reasons=frozenset({Progress.NO_PROGRESS}),
        ),
        effect=_produced_effect(keep_awaiting=True),
        to_state="同一state",
        command_names=("PersistRecord",),
    ),
    Rule(
        rule_id="P-23",
        section="record",
        description="BLOCK_INTERVENTIONのPRODUCED（外部依存block）",
        match=Match(
            states=frozenset({_S.BLOCKED}),
            event_type=ev.RecordProduced,
            pending=_ABSENT,
            record_kinds=frozenset({_K.BLOCK_INTERVENTION}),
            block_kinds=frozenset({BlockKind.EXTERNAL_DEPENDENCY}),
        ),
        effect=_produced_effect(keep_awaiting=True),
        to_state="同一state",
        command_names=("PersistRecord",),
    ),
    # USER_CANCELのPRODUCEDはawaiting不問・pending空を要求し、awaitingを消費せず維持する
    Rule(
        rule_id="P-21",
        section="record",
        description="USER_CANCELのPRODUCED（awaiting不問・維持。pending空を要求）",
        match=Match(
            states=_NON_TERMINAL,
            event_type=ev.RecordProduced,
            pending=_ABSENT,
            record_kinds=frozenset({_K.USER_CANCEL}),
        ),
        effect=_produced_effect(keep_awaiting=True),
        to_state="同一state",
        command_names=("PersistRecord",),
    ),
)


# ---------------------------------------------------------------------------
# 標準のVERIFIED行と応答行のrule constructor
# ---------------------------------------------------------------------------


def _verified_rule(
    rule_id: str,
    section: str,
    description: str,
    event_type: type,
    from_states: frozenset[State],
    to: State | None,
    awaiting_after: Awaiting | None,
    commands: tuple[Command, ...],
    *,
    progress: frozenset[Progress] | None = None,
) -> Rule:
    return Rule(
        rule_id=rule_id,
        section=section,
        description=description,
        match=Match(
            states=from_states, event_type=event_type, pending=_MATCHED, progress=progress
        ),
        effect=_advance(to, awaiting_after, commands),
        to_state=(to.value if to is not None else "同一state"),
        command_names=_names(commands),
    )


def _external_rule(
    rule_id: str,
    section: str,
    description: str,
    event_type: type,
    from_states: frozenset[State],
    awaiting_guard: Awaiting,
    to: State | None,
    awaiting_after: Awaiting | None,
    commands: tuple[Command, ...],
) -> Rule:
    """user-input recordの経路2（GitHub直接comment。永続化commandなしで合流）。"""
    return Rule(
        rule_id=rule_id,
        section=section,
        description=description,
        match=Match(
            states=from_states,
            event_type=event_type,
            awaiting=frozenset({awaiting_guard}),
            pending=_ABSENT,
        ),
        effect=_advance(to, awaiting_after, commands),
        to_state=(to.value if to is not None else "同一state"),
        command_names=_names(commands),
    )


def _response_rule(
    rule_id: str,
    section: str,
    description: str,
    event_type: type,
    from_states: frozenset[State],
    awaiting_guard: Awaiting,
    to: State | None,
    awaiting_after: Awaiting | None,
    commands: tuple[Command, ...],
) -> Rule:
    """record化されない応答event（awaiting guardのみ）。"""
    return Rule(
        rule_id=rule_id,
        section=section,
        description=description,
        match=Match(
            states=from_states,
            event_type=event_type,
            awaiting=frozenset({awaiting_guard}),
            pending=_ABSENT,
        ),
        effect=_advance(to, awaiting_after, commands),
        to_state=(to.value if to is not None else "同一state"),
        command_names=_names(commands),
    )


def _progress_block_rule(
    rule_id: str,
    description: str,
    event_type: type,
    from_states: frozenset[State],
    continuation: BlockedContinuation,
    entry_commands: tuple[Command, ...] = (),
) -> Rule:
    """上限・膠着の判定でBLOCKEDへ入り、本来の継続を保存する。

    agent（Codex / host）起動commandは発行しない。entry_commandsは承認失効のような
    安全側の非agent commandに限る（progress判定の結果で安全semanticsを変えない）。
    """
    budget = BUDGET_EVENTS[event_type][0]

    def effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
        e = cast(
            "ev.ReviewBlockingVerified | ev.CiCodeFailureVerified | ev.FixResultVerified"
            " | ev.ClarificationQuestionVerified | ev.VerdictResubmitVerified",
            event,
        )
        block = ProgressBlock(
            binding=e.evidence.binding,
            head=e.report.head,
            continuation=continuation,
            reason=e.report.progress,
            budget=budget,
            counter_snapshot=e.report.counter_snapshot,
            fingerprint=e.report.fingerprint,
        )
        return replace(ms, state=State.BLOCKED, awaiting=None, pending_record=None, block=block), entry_commands

    return Rule(
        rule_id=rule_id,
        section="progress",
        description=description,
        match=Match(states=from_states, event_type=event_type, pending=_MATCHED, progress=_BOUNDED),
        effect=effect,
        to_state=State.BLOCKED.value,
        command_names=_names(entry_commands),
    )


# ---------------------------------------------------------------------------
# main workflowの行
# ---------------------------------------------------------------------------

_HOST_APPLY = (RequestHostAction(HostAction.APPLY_FINDINGS),)
_CODEX_REVIEW = (RequestCodexReview(CodexPurpose.CODE_REVIEW),)

# BLOCKEDへ入るときに保存する「本来の継続」の**全体**。`BlockedContinuation`は
# registry由来の有限値であり、ruleはこの表以外の継続を構築しない（contract testで固定）。
#
# checkpointはcommand列ではなく**このtableのID**を保存する（ADR-0019 決定1）。commandは
# payloadを持つ11種の直和で、直列化すると他で使わない大きなformatを新設することになる。
# IDならreaderが同じobjectを引けてround-tripが一致し、表に無いIDはfail closedにできる。
# IDは永続値なので、entryを消す・意味を変える場合はversion bumpとmigrationが要る。
BLOCKED_CONTINUATIONS: Final[Mapping[str, BlockedContinuation]] = {
    "REVIEW_BLOCKING": BlockedContinuation(
        resume_state=_S.CHANGES_REQUESTED, commands=_HOST_APPLY, awaiting=_A.HOST_APPLY_FINDINGS
    ),
    "CLARIFICATION": BlockedContinuation(
        resume_state=_S.CLARIFYING_REVIEW,
        commands=(RequestCodexReview(CodexPurpose.CLARIFICATION),),
        awaiting=_A.CODEX_CLARIFICATION,
    ),
    "FIX_RESULT": BlockedContinuation(
        resume_state=_S.RUNNING_REVIEW, commands=_CODEX_REVIEW, awaiting=_A.CODEX_CODE_REVIEW
    ),
    "RESUBMIT": BlockedContinuation(
        resume_state=_S.REVIEWING_DECISION_REQUEST,
        commands=(RequestHostAction(HostAction.REVISE_DECISION_REQUEST),),
        awaiting=_A.HOST_REVISE_DECISION_REQUEST,
    ),
    "CI_CODE_FAILURE": BlockedContinuation(
        resume_state=_S.CHANGES_REQUESTED,
        commands=(InvalidateApprovals(),) + _HOST_APPLY,
        awaiting=_A.HOST_APPLY_FINDINGS,
    ),
    "EXTERNAL_DEPENDENCY": BlockedContinuation(
        resume_state=_S.APPLYING_FIXES, commands=_HOST_APPLY, awaiting=_A.HOST_APPLY_FINDINGS
    ),
}

_CONT_REVIEW_BLOCKING = BLOCKED_CONTINUATIONS["REVIEW_BLOCKING"]
_CONT_CLARIFICATION = BLOCKED_CONTINUATIONS["CLARIFICATION"]
_CONT_FIX_RESULT = BLOCKED_CONTINUATIONS["FIX_RESULT"]
_CONT_RESUBMIT = BLOCKED_CONTINUATIONS["RESUBMIT"]
_CONT_CI_CODE_FAILURE = BLOCKED_CONTINUATIONS["CI_CODE_FAILURE"]
_CONT_EXTERNAL_DEPENDENCY = BLOCKED_CONTINUATIONS["EXTERNAL_DEPENDENCY"]


def _permission_effect(return_to: State) -> Effect:
    def effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
        return (
            replace(
                ms,
                state=State.AWAITING_TOOL_PERMISSION,
                awaiting=Awaiting.USER_INPUT_PERMISSION,
                pending_record=None,
                return_to=return_to,
            ),
            (),
        )

    return effect


def _permission_resume_effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    # AWAITING_TOOL_PERMISSIONのinvariantによりreturn_toは必ず存在する
    return_to = cast(State, ms.return_to)
    commands, awaiting = DRIVE_TABLE[return_to]
    return replace(ms, state=return_to, awaiting=awaiting, return_to=None), commands


def _external_dependency_effect(ms: MachineState, event: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    e = cast(ev.ExternalDependencyVerified, event)
    block = ExternalDependencyBlock(
        binding=e.evidence.binding,
        head=e.head,
        continuation=_CONT_EXTERNAL_DEPENDENCY,
        evidence=e.evidence,
    )
    return replace(ms, state=State.BLOCKED, awaiting=None, pending_record=None, block=block), ()


WORKFLOW_RULES: tuple[Rule, ...] = (
    _verified_rule(
        "T-03",
        "workflow",
        "review結果blocking（CONTINUE。REVIEW_ROUND消費）-> fix依頼",
        ev.ReviewBlockingVerified,
        frozenset({_S.RUNNING_REVIEW}),
        _S.CHANGES_REQUESTED,
        _A.HOST_APPLY_FINDINGS,
        _HOST_APPLY,
        progress=_CONTINUE,
    ),
    _progress_block_rule(
        "T-B03",
        "review round上限・膠着 -> BLOCKED（継続保存、command発行なし）",
        ev.ReviewBlockingVerified,
        frozenset({_S.RUNNING_REVIEW}),
        _CONT_REVIEW_BLOCKING,
    ),
    _verified_rule(
        "T-04",
        "workflow",
        "review承認 -> CI確認",
        ev.ReviewApprovedVerified,
        frozenset({_S.RUNNING_REVIEW}),
        _S.WAITING_CI,
        _A.CI_RESULT,
        (CheckCi(),),
    ),
    Rule(
        rule_id="T-05",
        section="workflow",
        description="reviewer実行中のtool permission停止 -> AWAITING_TOOL_PERMISSION",
        match=Match(states=frozenset({_S.RUNNING_REVIEW}), event_type=ev.ToolPermissionBlocked, pending=_MATCHED),
        effect=_permission_effect(_S.RUNNING_REVIEW),
        to_state=_S.AWAITING_TOOL_PERMISSION.value,
        command_names=(),
    ),
    _response_rule(
        "T-06",
        "workflow",
        "hostのfinding対応着手（awaiting維持）",
        ev.FixStarted,
        frozenset({_S.CHANGES_REQUESTED}),
        _A.HOST_APPLY_FINDINGS,
        _S.APPLYING_FIXES,
        _A.HOST_APPLY_FINDINGS,
        (),
    ),
    _verified_rule(
        "T-07",
        "workflow",
        "coderの逆質問（CONTINUE。CLARIFICATION_TURN消費）-> clarification起動",
        ev.ClarificationQuestionVerified,
        frozenset({_S.CHANGES_REQUESTED}),
        _S.CLARIFYING_REVIEW,
        _A.CODEX_CLARIFICATION,
        (RequestCodexReview(CodexPurpose.CLARIFICATION),),
        progress=_CONTINUE,
    ),
    _progress_block_rule(
        "T-B07",
        "clarification turn上限・膠着 -> BLOCKED（継続保存、command発行なし）",
        ev.ClarificationQuestionVerified,
        frozenset({_S.CHANGES_REQUESTED}),
        _CONT_CLARIFICATION,
    ),
    _verified_rule(
        "T-08a",
        "workflow",
        "clarification回答CONFIRMED（loop終了結果）-> fix再開",
        ev.ClarificationConfirmedVerified,
        frozenset({_S.CLARIFYING_REVIEW}),
        _S.CHANGES_REQUESTED,
        _A.HOST_APPLY_FINDINGS,
        _HOST_APPLY,
    ),
    _verified_rule(
        "T-08b",
        "workflow",
        "clarification回答REVISED（loop終了結果）-> fix再開",
        ev.ClarificationRevisedVerified,
        frozenset({_S.CLARIFYING_REVIEW}),
        _S.CHANGES_REQUESTED,
        _A.HOST_APPLY_FINDINGS,
        _HOST_APPLY,
    ),
    _verified_rule(
        "T-09",
        "workflow",
        "clarification回答WITHDRAWN（loop終了結果）-> fresh review",
        ev.ClarificationWithdrawnVerified,
        frozenset({_S.CLARIFYING_REVIEW}),
        _S.RUNNING_REVIEW,
        _A.CODEX_CODE_REVIEW,
        _CODEX_REVIEW,
    ),
    _verified_rule(
        "T-10",
        "decision",
        "clarification回答ESCALATED（loop終了結果）-> decision request起草",
        ev.ClarificationEscalatedVerified,
        frozenset({_S.CLARIFYING_REVIEW}),
        _S.REVIEWING_DECISION_REQUEST,
        _A.HOST_DRAFT_DECISION_REQUEST,
        (RequestHostAction(HostAction.DRAFT_DECISION_REQUEST),),
    ),
    _verified_rule(
        "T-11",
        "workflow",
        "fix結果（CONTINUE。REVIEW_ROUND判定のみ）-> re-review",
        ev.FixResultVerified,
        frozenset({_S.APPLYING_FIXES}),
        _S.RUNNING_REVIEW,
        _A.CODEX_CODE_REVIEW,
        _CODEX_REVIEW,
        progress=_CONTINUE,
    ),
    _progress_block_rule(
        "T-B11",
        "fix完了時の膠着判定 -> BLOCKED（継続保存、command発行なし）",
        ev.FixResultVerified,
        frozenset({_S.APPLYING_FIXES}),
        _CONT_FIX_RESULT,
    ),
    _verified_rule(
        "T-12",
        "decision",
        "fix中のdecision request -> verdict依頼",
        ev.DecisionRequestVerified,
        frozenset({_S.APPLYING_FIXES}),
        _S.REVIEWING_DECISION_REQUEST,
        _A.CODEX_DECISION_VERDICT,
        (RequestCodexReview(CodexPurpose.DECISION_VERDICT),),
    ),
    Rule(
        rule_id="T-13",
        section="workflow",
        description="fix中のtool permission停止 -> AWAITING_TOOL_PERMISSION",
        match=Match(states=frozenset({_S.APPLYING_FIXES}), event_type=ev.ToolPermissionBlocked, pending=_MATCHED),
        effect=_permission_effect(_S.APPLYING_FIXES),
        to_state=_S.AWAITING_TOOL_PERMISSION.value,
        command_names=(),
    ),
    _verified_rule(
        "T-14",
        "decision",
        "decision request（draft / revised）の投稿確認 -> verdict依頼",
        ev.DecisionRequestVerified,
        frozenset({_S.REVIEWING_DECISION_REQUEST}),
        None,
        _A.CODEX_DECISION_VERDICT,
        (RequestCodexReview(CodexPurpose.DECISION_VERDICT),),
    ),
    _verified_rule(
        "T-15",
        "decision",
        "verdict ASK_USER（loop終了結果）-> brief起草",
        ev.VerdictAskUserVerified,
        frozenset({_S.REVIEWING_DECISION_REQUEST}),
        None,
        _A.HOST_DRAFT_DECISION_BRIEF,
        (RequestHostAction(HostAction.DRAFT_DECISION_BRIEF),),
    ),
    _verified_rule(
        "T-16",
        "decision",
        "brief投稿確認 -> ユーザー判断待ち",
        ev.DecisionBriefVerified,
        frozenset({_S.REVIEWING_DECISION_REQUEST}),
        _S.AWAITING_USER_DECISION,
        _A.USER_INPUT_DECISION,
        (),
    ),
    _verified_rule(
        "T-17",
        "decision",
        "verdict PROCEED（loop終了結果）-> decision record作成",
        ev.VerdictProceedVerified,
        frozenset({_S.REVIEWING_DECISION_REQUEST}),
        None,
        _A.HOST_RECORD_DECISION,
        (RequestHostAction(HostAction.RECORD_DECISION),),
    ),
    _verified_rule(
        "T-18",
        "decision",
        "decision record投稿確認 -> fix再開",
        ev.DecisionRecordVerified,
        frozenset({_S.REVIEWING_DECISION_REQUEST}),
        _S.APPLYING_FIXES,
        _A.HOST_APPLY_FINDINGS,
        _HOST_APPLY,
    ),
    _verified_rule(
        "T-19",
        "decision",
        "verdict RESUBMIT（CONTINUE。共通counterのCLARIFICATION_TURN消費）-> 再提出起草",
        ev.VerdictResubmitVerified,
        frozenset({_S.REVIEWING_DECISION_REQUEST}),
        None,
        _A.HOST_REVISE_DECISION_REQUEST,
        (RequestHostAction(HostAction.REVISE_DECISION_REQUEST),),
        progress=_CONTINUE,
    ),
    _progress_block_rule(
        "T-B19",
        "resubmit上限（共通counter）・膠着 -> BLOCKED（継続保存、command発行なし）",
        ev.VerdictResubmitVerified,
        frozenset({_S.REVIEWING_DECISION_REQUEST}),
        _CONT_RESUBMIT,
    ),
    _verified_rule(
        "T-20a",
        "decision",
        "ユーザー判断record（経路1: PowerShell転記）-> fix再開",
        ev.UserDecisionVerified,
        frozenset({_S.AWAITING_USER_DECISION}),
        _S.APPLYING_FIXES,
        _A.HOST_APPLY_FINDINGS,
        _HOST_APPLY,
    ),
    _external_rule(
        "T-20b",
        "decision",
        "ユーザー判断record（経路2: GitHub直接comment）-> fix再開",
        ev.UserDecisionVerified,
        frozenset({_S.AWAITING_USER_DECISION}),
        _A.USER_INPUT_DECISION,
        _S.APPLYING_FIXES,
        _A.HOST_APPLY_FINDINGS,
        _HOST_APPLY,
    ),
    Rule(
        rule_id="T-21",
        section="workflow",
        description="permission解除の明示resume -> return_toへ復帰し駆動commandを再発行",
        match=Match(
            states=frozenset({_S.AWAITING_TOOL_PERMISSION}),
            event_type=ev.PermissionResumeValidated,
            awaiting=frozenset({_A.USER_INPUT_PERMISSION}),
            pending=_ABSENT,
        ),
        effect=_permission_resume_effect,
        to_state="return_to",
        command_names=("return_to対応の駆動command",),
    ),
    _response_rule(
        "T-22",
        "ci",
        "CI成功 -> report生成",
        ev.CiSucceeded,
        frozenset({_S.WAITING_CI}),
        _A.CI_RESULT,
        _S.GENERATING_REPORT,
        _A.REPORT,
        (GenerateReport(),),
    ),
    _verified_rule(
        "T-23",
        "ci",
        "CI code failure（CONTINUE。REVIEW_ROUND消費）-> 承認失効 + fix依頼",
        ev.CiCodeFailureVerified,
        frozenset({_S.WAITING_CI}),
        _S.CHANGES_REQUESTED,
        _A.HOST_APPLY_FINDINGS,
        (InvalidateApprovals(),) + _HOST_APPLY,
        progress=_CONTINUE,
    ),
    _progress_block_rule(
        "T-B23",
        "CI code failureのround上限・膠着 -> 承認を即時失効してBLOCKED（継続内の失効は冪等再発行）",
        ev.CiCodeFailureVerified,
        frozenset({_S.WAITING_CI}),
        _CONT_CI_CODE_FAILURE,
        entry_commands=(InvalidateApprovals(),),
    ),
    _response_rule(
        "T-24",
        "ci",
        "CI基盤失敗 -> 再確認（awaiting維持）",
        ev.CiInfraFailure,
        frozenset({_S.WAITING_CI}),
        _A.CI_RESULT,
        None,
        _A.CI_RESULT,
        (CheckCi(),),
    ),
    _verified_rule(
        "T-25",
        "ci",
        "CI timeout記録 -> WAITING_CIで待機（awaiting解除）",
        ev.CiTimeoutRecorded,
        frozenset({_S.WAITING_CI}),
        None,
        None,
        (),
    ),
    Rule(
        rule_id="T-26",
        section="ci",
        description="CI待ち中の外部head変更 -> 承認失効 + fresh review",
        match=Match(states=frozenset({_S.WAITING_CI}), event_type=ev.HeadChangedExternally, pending=_ABSENT),
        effect=_advance(_S.RUNNING_REVIEW, _A.CODEX_CODE_REVIEW, (InvalidateApprovals(),) + _CODEX_REVIEW),
        to_state=_S.RUNNING_REVIEW.value,
        command_names=("InvalidateApprovals", "RequestCodexReview"),
    ),
    _verified_rule(
        "T-27",
        "report",
        "final report投稿確認 -> merge gate",
        ev.ReportVerified,
        frozenset({_S.GENERATING_REPORT}),
        _S.READY_FOR_HUMAN_MERGE,
        _A.USER_INPUT_GATE,
        (),
    ),
    _response_rule(
        "T-28",
        "report",
        "report失敗 -> REPORT_FAILED",
        ev.ReportFailed,
        frozenset({_S.GENERATING_REPORT}),
        _A.REPORT,
        _S.REPORT_FAILED,
        None,
        (),
    ),
    _verified_rule(
        "T-29a",
        "gate",
        "gate質問record（経路1）-> host回答依頼",
        ev.GateQuestionVerified,
        frozenset({_S.READY_FOR_HUMAN_MERGE}),
        None,
        _A.HOST_ANSWER_GATE_QUESTION,
        (RequestHostAction(HostAction.ANSWER_GATE_QUESTION),),
    ),
    _external_rule(
        "T-29b",
        "gate",
        "gate質問record（経路2: GitHub直接comment）-> host回答依頼",
        ev.GateQuestionVerified,
        frozenset({_S.READY_FOR_HUMAN_MERGE}),
        _A.USER_INPUT_GATE,
        None,
        _A.HOST_ANSWER_GATE_QUESTION,
        (RequestHostAction(HostAction.ANSWER_GATE_QUESTION),),
    ),
    _verified_rule(
        "T-30",
        "gate",
        "gate回答の投稿確認 -> gate待機へ戻る",
        ev.GateAnswerVerified,
        frozenset({_S.READY_FOR_HUMAN_MERGE}),
        None,
        _A.USER_INPUT_GATE,
        (),
    ),
    _verified_rule(
        "T-31a",
        "gate",
        "追加変更依頼record（経路1）-> 承認失効 + fix依頼",
        ev.GateChangesVerified,
        frozenset({_S.READY_FOR_HUMAN_MERGE}),
        _S.CHANGES_REQUESTED,
        _A.HOST_APPLY_FINDINGS,
        (InvalidateApprovals(),) + _HOST_APPLY,
    ),
    _external_rule(
        "T-31b",
        "gate",
        "追加変更依頼record（経路2: GitHub直接comment）-> 承認失効 + fix依頼",
        ev.GateChangesVerified,
        frozenset({_S.READY_FOR_HUMAN_MERGE}),
        _A.USER_INPUT_GATE,
        _S.CHANGES_REQUESTED,
        _A.HOST_APPLY_FINDINGS,
        (InvalidateApprovals(),) + _HOST_APPLY,
    ),
    Rule(
        rule_id="T-33",
        section="gate",
        description="merge gate中の外部head変更 -> 承認失効 + fresh review",
        match=Match(
            states=frozenset({_S.READY_FOR_HUMAN_MERGE}), event_type=ev.HeadChangedExternally, pending=_ABSENT
        ),
        effect=_advance(_S.RUNNING_REVIEW, _A.CODEX_CODE_REVIEW, (InvalidateApprovals(),) + _CODEX_REVIEW),
        to_state=_S.RUNNING_REVIEW.value,
        command_names=("InvalidateApprovals", "RequestCodexReview"),
    ),
    Rule(
        rule_id="T-43",
        section="workflow",
        description="外部依存の検出record -> BLOCKED（EXTERNAL_DEPENDENCY。継続保存）",
        match=Match(states=frozenset({_S.APPLYING_FIXES}), event_type=ev.ExternalDependencyVerified, pending=_MATCHED),
        effect=_external_dependency_effect,
        to_state=State.BLOCKED.value,
        command_names=(),
    ),
)
