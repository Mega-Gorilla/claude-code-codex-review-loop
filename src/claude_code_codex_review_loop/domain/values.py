# SPDX-License-Identifier: Apache-2.0
"""C-01のvalue objectと構造化error。

opaque値（binding / head / snapshot / fingerprint）は他componentが採番・検証した監査値で、
C-01は等価比較のみを行う（Phase 1計画の節2「純粋性」）。MachineStateは排他的procedure直和と
構築時invariant検証により、不正な付随値の組合せを構築できない（AC-C01-06、R4系列）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, unique
from typing import TYPE_CHECKING

from .states import ACTIVE_STATES, TERMINAL_STATES, State

if TYPE_CHECKING:
    from .commands import Command


# ---------------------------------------------------------------------------
# 構造化error
# ---------------------------------------------------------------------------


class DomainError(Exception):
    """C-01の構造化errorの基底。"""


class IllegalMachineStateError(DomainError):
    """MachineStateの組合せ不変条件違反（構築時に拒否する）。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class TransitionRejected(DomainError):
    """未定義または不整合な(state, event, guard)入力の拒否。silent no-opにしない。"""

    def __init__(self, state: State, event_name: str, detail: str) -> None:
        super().__init__(f"{state.value} <- {event_name}: {detail}")
        self.state = state
        self.event_name = event_name
        self.detail = detail


class RegistryIntegrityError(DomainError):
    """registry自己検査の失敗（rule重複など）。構築時に検出する。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


# ---------------------------------------------------------------------------
# opaque値（等価比較のみ）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpaqueBinding:
    """record / attempt / violationのbinding。採番はC-06 / C-08。"""

    value: str


@dataclass(frozen=True)
class OpaqueRef:
    """head SHA・record参照・違反記述などへのopaque参照。"""

    value: str


@dataclass(frozen=True)
class OpaqueSnapshot:
    """counter snapshot（C-10 / C-11の監査値）。"""

    value: str


@dataclass(frozen=True)
class OpaqueFingerprint:
    """clarification / decisionのtopic fingerprint。"""

    value: str


# ---------------------------------------------------------------------------
# 有限discriminator
# ---------------------------------------------------------------------------


@unique
class RecordKind(Enum):
    """canonical recordの種別（内部15種 + user-input 6種）。"""

    # 内部record（agent / Controllerが生成し、Controllerが投稿する）
    REVIEW_RESULT = "REVIEW_RESULT"
    FIX_RESULT = "FIX_RESULT"
    CLARIFICATION_QUESTION = "CLARIFICATION_QUESTION"
    CLARIFICATION_ANSWER = "CLARIFICATION_ANSWER"
    DECISION_REQUEST = "DECISION_REQUEST"
    DECISION_VERDICT = "DECISION_VERDICT"
    DECISION_BRIEF = "DECISION_BRIEF"
    DECISION_RECORD = "DECISION_RECORD"
    EXTERNAL_DEPENDENCY = "EXTERNAL_DEPENDENCY"
    PERMISSION_BLOCK = "PERMISSION_BLOCK"
    CI_TIMEOUT = "CI_TIMEOUT"
    CI_CODE_FAILURE = "CI_CODE_FAILURE"
    FINAL_REPORT = "FINAL_REPORT"
    INTEGRITY_INCIDENT = "INTEGRITY_INCIDENT"
    GATE_ANSWER = "GATE_ANSWER"
    # user-input record（2経路。PowerShell転記 / GitHub直接comment）
    USER_DECISION = "USER_DECISION"
    GATE_QUESTION = "GATE_QUESTION"
    GATE_CHANGES = "GATE_CHANGES"
    MERGE_APPROVAL = "MERGE_APPROVAL"
    BLOCK_INTERVENTION = "BLOCK_INTERVENTION"
    USER_CANCEL = "USER_CANCEL"


USER_INPUT_RECORD_KINDS: frozenset[RecordKind] = frozenset(
    {
        RecordKind.USER_DECISION,
        RecordKind.GATE_QUESTION,
        RecordKind.GATE_CHANGES,
        RecordKind.MERGE_APPROVAL,
        RecordKind.BLOCK_INTERVENTION,
        RecordKind.USER_CANCEL,
    }
)

INTERNAL_RECORD_KINDS: frozenset[RecordKind] = frozenset(RecordKind) - USER_INPUT_RECORD_KINDS


@unique
class Awaiting(Enum):
    """発行済みcommandに対応する「次に受理してよい応答」の期待値（18値）。

    停止・記録の手続き中の期待値はProcedure直和が表現するため、ここには含めない。
    """

    CODEX_CODE_REVIEW = "CODEX_CODE_REVIEW"
    CODEX_CLARIFICATION = "CODEX_CLARIFICATION"
    CODEX_DECISION_VERDICT = "CODEX_DECISION_VERDICT"
    HOST_APPLY_FINDINGS = "HOST_APPLY_FINDINGS"
    HOST_DRAFT_DECISION_REQUEST = "HOST_DRAFT_DECISION_REQUEST"
    HOST_DRAFT_DECISION_BRIEF = "HOST_DRAFT_DECISION_BRIEF"
    HOST_RECORD_DECISION = "HOST_RECORD_DECISION"
    HOST_REVISE_DECISION_REQUEST = "HOST_REVISE_DECISION_REQUEST"
    HOST_ANSWER_GATE_QUESTION = "HOST_ANSWER_GATE_QUESTION"
    CI_RESULT = "CI_RESULT"
    REPORT = "REPORT"
    MERGE_PRECONDITIONS = "MERGE_PRECONDITIONS"
    MERGE_OUTCOME_EXECUTE = "MERGE_OUTCOME_EXECUTE"
    MERGE_OUTCOME_CANCEL = "MERGE_OUTCOME_CANCEL"
    MERGE_OUTCOME_FAILURE = "MERGE_OUTCOME_FAILURE"
    USER_INPUT_DECISION = "USER_INPUT_DECISION"
    USER_INPUT_GATE = "USER_INPUT_GATE"
    USER_INPUT_PERMISSION = "USER_INPUT_PERMISSION"


MERGE_OUTCOME_AWAITINGS: frozenset[Awaiting] = frozenset(
    {
        Awaiting.MERGE_OUTCOME_EXECUTE,
        Awaiting.MERGE_OUTCOME_CANCEL,
        Awaiting.MERGE_OUTCOME_FAILURE,
    }
)

# awaiting値が（procedureなしで）滞在できるstate。FAILEDは進入時に引き継ぐため常に許可される。
AWAITING_HOME: dict[Awaiting, frozenset[State]] = {
    Awaiting.CODEX_CODE_REVIEW: frozenset({State.RUNNING_REVIEW}),
    Awaiting.CODEX_CLARIFICATION: frozenset({State.CLARIFYING_REVIEW}),
    Awaiting.CODEX_DECISION_VERDICT: frozenset({State.REVIEWING_DECISION_REQUEST}),
    Awaiting.HOST_APPLY_FINDINGS: frozenset({State.CHANGES_REQUESTED, State.APPLYING_FIXES}),
    Awaiting.HOST_DRAFT_DECISION_REQUEST: frozenset({State.REVIEWING_DECISION_REQUEST}),
    Awaiting.HOST_DRAFT_DECISION_BRIEF: frozenset({State.REVIEWING_DECISION_REQUEST}),
    Awaiting.HOST_RECORD_DECISION: frozenset({State.REVIEWING_DECISION_REQUEST}),
    Awaiting.HOST_REVISE_DECISION_REQUEST: frozenset({State.REVIEWING_DECISION_REQUEST}),
    Awaiting.HOST_ANSWER_GATE_QUESTION: frozenset({State.READY_FOR_HUMAN_MERGE}),
    Awaiting.CI_RESULT: frozenset({State.WAITING_CI}),
    Awaiting.REPORT: frozenset({State.GENERATING_REPORT}),
    Awaiting.MERGE_PRECONDITIONS: frozenset({State.MERGING}),
    Awaiting.MERGE_OUTCOME_EXECUTE: frozenset({State.MERGING}),
    Awaiting.MERGE_OUTCOME_CANCEL: frozenset({State.MERGING}),
    Awaiting.MERGE_OUTCOME_FAILURE: frozenset({State.MERGING}),
    Awaiting.USER_INPUT_DECISION: frozenset({State.AWAITING_USER_DECISION}),
    Awaiting.USER_INPUT_GATE: frozenset({State.READY_FOR_HUMAN_MERGE}),
    Awaiting.USER_INPUT_PERMISSION: frozenset({State.AWAITING_TOOL_PERMISSION}),
}


@unique
class Progress(Enum):
    """bounded-progress判定の結果（判定・counter管理はC-10 / C-11）。"""

    CONTINUE = "CONTINUE"
    LIMIT_REACHED = "LIMIT_REACHED"
    NO_PROGRESS = "NO_PROGRESS"


@unique
class Budget(Enum):
    """bounded loopのbudget種別。"""

    REVIEW_ROUND = "REVIEW_ROUND"
    CLARIFICATION_TURN = "CLARIFICATION_TURN"


@unique
class IncidentTarget(Enum):
    """incident監査記録の完了後に進むterminal。"""

    MERGED = "MERGED"
    CANCELLED = "CANCELLED"


@unique
class IncidentCoverage(Enum):
    """記録済みviolation集合とdeferred集合の差分から導出する2値（節5.4 / 判断12）。"""

    COMPLETE = "COMPLETE"
    REMAINDER = "REMAINDER"


# ---------------------------------------------------------------------------
# record / evidenceのvalue object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingRecord:
    """永続化の確認待ちrecord（単一slot）。"""

    kind: RecordKind
    binding: OpaqueBinding
    source_state: State


@dataclass(frozen=True)
class RecordEvidence:
    """投稿・read-after-write確認・検証が完了したrecordのevidence。"""

    kind: RecordKind
    binding: OpaqueBinding
    ref: OpaqueRef


@dataclass(frozen=True)
class IntegrityEvidenceRef:
    """integrity violationの参照。404 / sequence gap / hash差分等はdescriptorがopaqueに保持する。

    headは検出時の対象head（C-06 / C-07由来の監査値。等価比較のみ）。
    """

    binding: OpaqueBinding
    descriptor: OpaqueRef
    head: OpaqueRef


@dataclass(frozen=True)
class ProgressReport:
    """bounded loop継続遷移に付くprogress判定と監査値（C-10 / C-11由来）。"""

    progress: Progress
    head: OpaqueRef
    counter_snapshot: OpaqueSnapshot
    fingerprint: OpaqueFingerprint


@dataclass(frozen=True)
class BlockedContinuation:
    """上限・膠着でBLOCKEDへ入るときに保存する本来の継続（registry由来の有限値）。"""

    resume_state: State
    commands: tuple[Command, ...]
    awaiting: Awaiting | None


@dataclass(frozen=True)
class BlockResolutionEvidence:
    """block解消eventのevidence。target_block_bindingは解除対象のblock attemptを指す。

    record自身のbinding（一意性・再利用防止）とは別のfieldであり、C-01は対象blockとの
    完全一致照合のみを行う（AC-C01-11）。violation_bindingsはRECORD_INTEGRITY blockの
    解消evidence専用で、解除対象のviolation集合全体（canonical order）へbindする —
    集合が拡大した後の旧evidenceは一致しない（stale evidenceの拒否）。
    """

    target_block_binding: OpaqueBinding
    head: OpaqueRef
    record: RecordEvidence | None = None
    reason: Progress | None = None
    budget: Budget | None = None
    counter_snapshot: OpaqueSnapshot | None = None
    fingerprint: OpaqueFingerprint | None = None
    violation_bindings: tuple[OpaqueBinding, ...] | None = None


# ---------------------------------------------------------------------------
# BlockContext（3種の直和）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgressBlock:
    """bounded progress上限・膠着による停止。"""

    binding: OpaqueBinding
    head: OpaqueRef
    continuation: BlockedContinuation
    reason: Progress
    budget: Budget
    counter_snapshot: OpaqueSnapshot
    fingerprint: OpaqueFingerprint

    def __post_init__(self) -> None:
        if self.reason is Progress.CONTINUE:
            raise IllegalMachineStateError("PROGRESS_BLOCK_REASON", "reasonはLIMIT_REACHED / NO_PROGRESSに限る")


@dataclass(frozen=True)
class ExternalDependencyBlock:
    """外部依存の検出record（C-06検証済み）による停止。"""

    binding: OpaqueBinding
    head: OpaqueRef
    continuation: BlockedContinuation
    evidence: RecordEvidence


@dataclass(frozen=True)
class RecordIntegrityBlock:
    """record整合性違反による停止。violationsはcanonical order（binding昇順・重複なし）。"""

    violations: tuple[IntegrityEvidenceRef, ...]

    def __post_init__(self) -> None:
        if not self.violations:
            raise IllegalMachineStateError("INTEGRITY_BLOCK_EMPTY", "violation集合は空にできない")
        bindings = [v.binding.value for v in self.violations]
        if bindings != sorted(bindings) or len(set(bindings)) != len(bindings):
            raise IllegalMachineStateError("INTEGRITY_BLOCK_ORDER", "violationsはbinding昇順・重複なしで正規化する")

    @property
    def representative_binding(self) -> OpaqueBinding:
        """解消evidenceの照合に使う代表binding（canonical order先頭）。"""
        return self.violations[0].binding

    @property
    def head(self) -> OpaqueRef:
        """解消evidenceの照合に使うhead（代表violationの検出時head）。"""
        return self.violations[0].head


BlockContext = ProgressBlock | ExternalDependencyBlock | RecordIntegrityBlock


# ---------------------------------------------------------------------------
# Procedure（排他的な進行中手続きの直和）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalProcedure:
    """手続きなし。"""


@dataclass(frozen=True)
class CancellingProcedure:
    """cancel attemptの停止・checkpoint完了待ち。"""

    attempt_binding: OpaqueBinding


@dataclass(frozen=True)
class HaltingForBlockProcedure:
    """integrity halt gate: process停止完了を待ってからBLOCKEDへ入る。"""

    block: RecordIntegrityBlock


@dataclass(frozen=True)
class RecordingIncidentProcedure:
    """incident監査記録中。auditはcancelで未完了になったturnの監査参照。"""

    target: IncidentTarget
    audit: PendingRecord | None


Procedure = NormalProcedure | CancellingProcedure | HaltingForBlockProcedure | RecordingIncidentProcedure

NORMAL: NormalProcedure = NormalProcedure()


# ---------------------------------------------------------------------------
# MachineState
# ---------------------------------------------------------------------------


def canonicalize_integrity(refs: tuple[IntegrityEvidenceRef, ...]) -> tuple[IntegrityEvidenceRef, ...]:
    """binding重複を排除しbinding昇順へ正規化する（同一集合から同一のMachineStateを得る）。

    同一bindingの再検出は最初のevidenceを保持する（冪等。上書きしない）。
    """
    unique_refs: dict[str, IntegrityEvidenceRef] = {}
    for ref in refs:
        unique_refs.setdefault(ref.binding.value, ref)
    return tuple(unique_refs[key] for key in sorted(unique_refs))


@dataclass(frozen=True)
class MachineState:
    """C-01の完全な状態。組合せ不変条件は構築時に検証し、違反は構築できない。"""

    state: State
    procedure: Procedure = NORMAL
    awaiting: Awaiting | None = None
    pending_record: PendingRecord | None = None
    deferred_integrity: tuple[IntegrityEvidenceRef, ...] = field(default=())
    return_to: State | None = None
    recovery_to: State | None = None
    block: BlockContext | None = None

    def __post_init__(self) -> None:
        self._check_canonical_deferred()
        self._check_terminal_bare()
        self._check_block_scope()
        self._check_recovery_scope()
        self._check_return_scope()
        self._check_procedure_shape()
        self._check_deferred_scope()
        self._check_pending_home()
        self._check_awaiting_home()

    # 各検査は構造化errorのcodeで違反を特定できるようにする（R4系列が全codeを検査する）

    def _check_canonical_deferred(self) -> None:
        if self.deferred_integrity != canonicalize_integrity(self.deferred_integrity):
            raise IllegalMachineStateError("DEFERRED_CANONICAL", "deferred_integrityはbinding昇順・重複なしに限る")

    def _check_terminal_bare(self) -> None:
        if self.state in TERMINAL_STATES and (
            not isinstance(self.procedure, NormalProcedure)
            or self.awaiting is not None
            or self.pending_record is not None
            or self.deferred_integrity
            or self.return_to is not None
            or self.recovery_to is not None
            or self.block is not None
        ):
            raise IllegalMachineStateError("TERMINAL_BARE", "terminal stateは付随値を持てない")

    def _check_block_scope(self) -> None:
        if (self.block is not None) != (self.state is State.BLOCKED):
            raise IllegalMachineStateError("BLOCK_SCOPE", "blockはBLOCKEDでのみ、BLOCKEDでは必ず保持する")
        if self.state is State.BLOCKED:
            if self.awaiting is not None:
                raise IllegalMachineStateError("BLOCKED_NO_AWAITING", "BLOCKEDはawaitingを持てない")
            if isinstance(self.procedure, HaltingForBlockProcedure):
                raise IllegalMachineStateError("BLOCKED_NO_HALT_GATE", "halt gateはBLOCKED進入前の手続きに限る")
            if (
                isinstance(self.procedure, NormalProcedure)
                and self.pending_record is not None
                and self.pending_record.kind not in (RecordKind.USER_CANCEL, RecordKind.BLOCK_INTERVENTION)
            ):
                raise IllegalMachineStateError(
                    "BLOCKED_PENDING_KIND", "BLOCKEDのpendingはUSER_CANCEL / BLOCK_INTERVENTIONに限る"
                )

    def _check_recovery_scope(self) -> None:
        if self.recovery_to is not None:
            if self.state is not State.FAILED:
                raise IllegalMachineStateError("RECOVERY_SCOPE", "recovery_toはFAILEDのみが保持する")
            if self.recovery_to not in ACTIVE_STATES or self.recovery_to is State.MERGING:
                raise IllegalMachineStateError("RECOVERY_TARGET", "recovery_toはMERGINGを除くactive stateに限る")

    def _check_return_scope(self) -> None:
        if (self.return_to is not None) != (self.state is State.AWAITING_TOOL_PERMISSION):
            raise IllegalMachineStateError(
                "RETURN_SCOPE", "return_toはAWAITING_TOOL_PERMISSIONでのみ、当該stateでは必ず保持する"
            )
        if self.return_to is not None and self.return_to not in (State.RUNNING_REVIEW, State.APPLYING_FIXES):
            raise IllegalMachineStateError("RETURN_TARGET", "return_toはRUNNING_REVIEW / APPLYING_FIXESに限る")

    def _check_procedure_shape(self) -> None:
        if not isinstance(self.procedure, NormalProcedure) and self.awaiting is not None:
            raise IllegalMachineStateError(
                "PROCEDURE_NO_AWAITING", "手続き中はawaitingを持てない（期待値は手続きが表す）"
            )
        if isinstance(self.procedure, CancellingProcedure) and self.state is State.MERGING:
            raise IllegalMachineStateError(
                "MERGING_NO_CANCELLING", "MERGINGのcancelは結果照会で扱い停止手続きを持たない"
            )
        if isinstance(self.procedure, HaltingForBlockProcedure):
            if self.state not in ACTIVE_STATES or self.state is State.MERGING:
                raise IllegalMachineStateError("HALT_GATE_STATE", "halt gateはMERGINGを除くactive stateに限る")
            if self.pending_record is not None:
                raise IllegalMachineStateError(
                    "HALT_GATE_NO_PENDING", "halt gate進入時にpendingは破棄済みでなければならない"
                )
            if self.deferred_integrity:
                raise IllegalMachineStateError("HALT_GATE_NO_DEFERRED", "halt gate中のviolationはblock集合が保持する")
        if isinstance(self.procedure, RecordingIncidentProcedure):
            if not self.deferred_integrity:
                raise IllegalMachineStateError("INCIDENT_NEEDS_DEFERRED", "incident記録中はdeferred集合が空にならない")
            if self.pending_record is not None and self.pending_record.kind is not RecordKind.INTEGRITY_INCIDENT:
                raise IllegalMachineStateError(
                    "INCIDENT_PENDING_KIND", "incident記録中のpendingはINTEGRITY_INCIDENTに限る"
                )

    def _check_deferred_scope(self) -> None:
        if not self.deferred_integrity:
            return
        if isinstance(self.procedure, (CancellingProcedure, RecordingIncidentProcedure)):
            return
        if (
            isinstance(self.procedure, NormalProcedure)
            and self.state is State.MERGING
            and self.awaiting in MERGE_OUTCOME_AWAITINGS
        ):
            return
        raise IllegalMachineStateError(
            "DEFERRED_SCOPE", "deferred_integrityはMERGINGのoutcome段階・cancel中・incident記録中のみ保持できる"
        )

    def _check_pending_home(self) -> None:
        if self.pending_record is None:
            return
        if self.state is not self.pending_record.source_state and self.state is not State.FAILED:
            raise IllegalMachineStateError("PENDING_HOME", "pendingはsource_stateまたは引継先FAILEDでのみ保持できる")
        if self.pending_record.kind is RecordKind.INTEGRITY_INCIDENT and not isinstance(
            self.procedure, RecordingIncidentProcedure
        ):
            raise IllegalMachineStateError(
                "INCIDENT_PENDING_SCOPE", "INTEGRITY_INCIDENTのpendingはincident記録中に限る"
            )
        if self.awaiting is not None and self.pending_record.kind is not RecordKind.USER_CANCEL:
            raise IllegalMachineStateError(
                "PENDING_AWAITING_EXCLUSIVE", "awaitingと共存できるpendingはUSER_CANCEL（awaiting維持）に限る"
            )
        if (
            self.state is State.FAILED
            and self.pending_record.source_state is not State.FAILED
            and self.recovery_to is not self.pending_record.source_state
        ):
            raise IllegalMachineStateError(
                "FAILED_PENDING_RECOVERY", "FAILEDが引き継ぐpendingのsource_stateはrecovery_toと一致する"
            )

    def _check_awaiting_home(self) -> None:
        if self.awaiting is None:
            return
        if self.state not in AWAITING_HOME[self.awaiting] and self.state is not State.FAILED:
            raise IllegalMachineStateError("AWAITING_HOME", "awaiting値が当該stateに対応しない")
        if self.state is State.FAILED and (
            self.recovery_to is None or self.recovery_to not in AWAITING_HOME[self.awaiting]
        ):
            raise IllegalMachineStateError(
                "FAILED_AWAITING_RECOVERY", "FAILEDが引き継ぐawaitingはrecovery_toの応答期待値でなければならない"
            )
