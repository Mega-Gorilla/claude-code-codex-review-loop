# SPDX-License-Identifier: Apache-2.0
"""C-01へ入力されるevent。

evidenceは他component（C-05〜C-13）が検証済みの監査値であり、C-01は等価比較のみを行う。
検証済みrecordのVERIFIED系eventは、evidence.kindがevent種別と一致することを構築時に検査する
（遷移先・command列はeventから注入できない。AC-C01-06 / R4）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .values import (
    BlockResolutionEvidence,
    DomainError,
    IntegrityEvidenceRef,
    OpaqueBinding,
    OpaqueRef,
    ProgressReport,
    RecordEvidence,
    RecordKind,
)


class IllegalEventError(DomainError):
    """eventの構築時検査の違反（kind不一致など）。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# initialize専用
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightOk:
    """preflight検証の成功（C-07 / C-08）。"""


@dataclass(frozen=True)
class PreflightNg:
    """preflight検証の失敗。"""


# ---------------------------------------------------------------------------
# canonical record lifecycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordProduced:
    """agent / Controllerがrecord本文を生成した（永続化前）。"""

    kind: RecordKind
    binding: OpaqueBinding


@dataclass(frozen=True)
class _VerifiedEvent:
    """検証済みrecordのVERIFIED系eventの共通形。"""

    evidence: RecordEvidence

    EXPECTED_KIND: ClassVar[RecordKind]

    def __post_init__(self) -> None:
        if self.evidence.kind is not type(self).EXPECTED_KIND:
            raise IllegalEventError(
                "EVIDENCE_KIND",
                f"{type(self).__name__}はkind={type(self).EXPECTED_KIND.value}のevidenceのみを受ける",
            )


@dataclass(frozen=True)
class ReviewBlockingVerified(_VerifiedEvent):
    """review結果: blocking finding（REVIEW_ROUND消費）。"""

    report: ProgressReport
    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.REVIEW_RESULT


@dataclass(frozen=True)
class ReviewApprovedVerified(_VerifiedEvent):
    """review結果: 承認。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.REVIEW_RESULT


@dataclass(frozen=True)
class FixResultVerified(_VerifiedEvent):
    """fix結果（同一round完了。REVIEW_ROUNDの判定のみで二重計上しない）。"""

    report: ProgressReport
    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.FIX_RESULT


@dataclass(frozen=True)
class ClarificationQuestionVerified(_VerifiedEvent):
    """coderからの逆質問（CLARIFICATION_TURN消費）。"""

    report: ProgressReport
    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.CLARIFICATION_QUESTION


@dataclass(frozen=True)
class ClarificationConfirmedVerified(_VerifiedEvent):
    """clarification回答: CONFIRMED（loop終了結果）。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.CLARIFICATION_ANSWER


@dataclass(frozen=True)
class ClarificationRevisedVerified(_VerifiedEvent):
    """clarification回答: REVISED（loop終了結果）。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.CLARIFICATION_ANSWER


@dataclass(frozen=True)
class ClarificationWithdrawnVerified(_VerifiedEvent):
    """clarification回答: WITHDRAWN（loop終了結果）。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.CLARIFICATION_ANSWER


@dataclass(frozen=True)
class ClarificationEscalatedVerified(_VerifiedEvent):
    """clarification回答: ESCALATED（loop終了結果）。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.CLARIFICATION_ANSWER


@dataclass(frozen=True)
class DecisionRequestVerified(_VerifiedEvent):
    """decision requestの投稿確認（draft / revised）。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.DECISION_REQUEST


@dataclass(frozen=True)
class VerdictAskUserVerified(_VerifiedEvent):
    """verdict: ASK_USER（loop終了結果）。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.DECISION_VERDICT


@dataclass(frozen=True)
class VerdictProceedVerified(_VerifiedEvent):
    """verdict: PROCEED_WITH_RECORD（loop終了結果）。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.DECISION_VERDICT


@dataclass(frozen=True)
class VerdictResubmitVerified(_VerifiedEvent):
    """verdict: REVISE_AND_RESUBMIT（同一fingerprintのCLARIFICATION_TURNとして共通counterを消費）。"""

    report: ProgressReport
    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.DECISION_VERDICT


@dataclass(frozen=True)
class DecisionBriefVerified(_VerifiedEvent):
    """decision briefの投稿確認。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.DECISION_BRIEF


@dataclass(frozen=True)
class DecisionRecordVerified(_VerifiedEvent):
    """decision recordの投稿確認。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.DECISION_RECORD


@dataclass(frozen=True)
class UserDecisionVerified(_VerifiedEvent):
    """ユーザー判断record（2経路合流）。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.USER_DECISION


@dataclass(frozen=True)
class ExternalDependencyVerified(_VerifiedEvent):
    """外部依存の検出record（headはblock文脈の監査値）。"""

    head: OpaqueRef
    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.EXTERNAL_DEPENDENCY


@dataclass(frozen=True)
class ToolPermissionBlocked(_VerifiedEvent):
    """tool permissionによる停止record。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.PERMISSION_BLOCK


@dataclass(frozen=True)
class CiTimeoutRecorded(_VerifiedEvent):
    """bounded CI waitのtimeout record。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.CI_TIMEOUT


@dataclass(frozen=True)
class CiCodeFailureVerified(_VerifiedEvent):
    """CIのcode failure record（REVIEW_ROUND消費）。"""

    report: ProgressReport
    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.CI_CODE_FAILURE


@dataclass(frozen=True)
class ReportVerified(_VerifiedEvent):
    """final reportの投稿確認。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.FINAL_REPORT


@dataclass(frozen=True)
class GateQuestionVerified(_VerifiedEvent):
    """merge gateでのユーザー質問record（2経路合流）。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.GATE_QUESTION


@dataclass(frozen=True)
class GateAnswerVerified(_VerifiedEvent):
    """gate質問へのhost回答record。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.GATE_ANSWER


@dataclass(frozen=True)
class GateChangesVerified(_VerifiedEvent):
    """merge gateでの追加変更依頼record（2経路合流）。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.GATE_CHANGES


@dataclass(frozen=True)
class MergeApprovalVerified(_VerifiedEvent):
    """merge承認record（allowlist完全一致検証済み。D-031）。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.MERGE_APPROVAL


@dataclass(frozen=True)
class UserCancelVerified(_VerifiedEvent):
    """cancel intentのcanonical検証（evidence.bindingをcancel attempt bindingとして再利用する）。"""

    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.USER_CANCEL


@dataclass(frozen=True)
class IntegrityIncidentVerified(_VerifiedEvent):
    """INTEGRITY_INCIDENT recordの投稿・確認・検証の完了。

    recorded_bindingsは当該recordが記録したviolation bindingの集合（C-06が構成・検証）。
    """

    recorded_bindings: tuple[OpaqueBinding, ...]
    EXPECTED_KIND: ClassVar[RecordKind] = RecordKind.INTEGRITY_INCIDENT


# ---------------------------------------------------------------------------
# record以外のevent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixStarted:
    """hostがfinding対応へ着手（C-08）。"""


@dataclass(frozen=True)
class PermissionResumeValidated:
    """Permission IDとheadの再検証を伴う明示resume（C-08）。"""


@dataclass(frozen=True)
class CiSucceeded:
    """対象headのCI成功（C-12）。"""


@dataclass(frozen=True)
class CiInfraFailure:
    """CI基盤起因の失敗（C-12）。"""


@dataclass(frozen=True)
class CiResumeRequested:
    """WAITING_CIからの明示resume（C-08）。"""


@dataclass(frozen=True)
class ReportFailed:
    """report生成の失敗（C-12）。"""


@dataclass(frozen=True)
class ReporterRetryRequested:
    """reporter再実行の指示（C-08）。"""


@dataclass(frozen=True)
class MergePreconditionsOk:
    """merge直前再検証: 全条件一致（C-13）。"""


@dataclass(frozen=True)
class MergePreconditionMismatch:
    """merge直前再検証: 不一致（head変更以外）。"""


@dataclass(frozen=True)
class MergeConfirmed:
    """merge結果照会: 完了を確認（C-13）。"""


@dataclass(frozen=True)
class MergeNotExecutedConfirmed:
    """merge結果照会: 未実行を確認（C-13）。"""


@dataclass(frozen=True)
class MergeOutcomeUnknown:
    """merge結果照会: 成否不明（C-13）。"""


@dataclass(frozen=True)
class HeadChangedExternally:
    """外部からのhead更新の検出（C-07）。"""


@dataclass(frozen=True)
class CancellationCompleted:
    """process停止とcheckpoint保存の完了。

    対話cancelはattempt_binding（cancel attemptと一致）を、緊急停止はrun / checkpointへの
    bindを検証したemergency_evidenceを、いずれか一方だけ持つ。
    """

    attempt_binding: OpaqueBinding | None = None
    emergency_evidence: OpaqueRef | None = None

    def __post_init__(self) -> None:
        if (self.attempt_binding is None) == (self.emergency_evidence is None):
            raise IllegalEventError(
                "CANCEL_COMPLETION_ORIGIN", "attempt_bindingとemergency_evidenceは排他でいずれか一方が必要"
            )


@dataclass(frozen=True)
class BlockHaltCompleted:
    """integrity halt gateのprocess停止とcheckpoint保存の完了（block bindingと一致）。"""

    block_binding: OpaqueBinding


@dataclass(frozen=True)
class RecordIntegrityViolationDetected:
    """canonical record整合性違反の検出（C-06。AC-C06-06〜08）。"""

    evidence: IntegrityEvidenceRef


@dataclass(frozen=True)
class RunFailed:
    """bounded retry後の失敗（各層）。"""


@dataclass(frozen=True)
class ResumeValidated:
    """resume preflightの成功（C-07）。"""


@dataclass(frozen=True)
class ResumeFallbackRequired:
    """head変更等によるfallback resumeの要求（C-07）。"""


@dataclass(frozen=True)
class ResumeSameHeadValidated:
    """merge失敗後の同一head・全条件再確認（C-07 / C-13）。"""


@dataclass(frozen=True)
class BlockResolvedLimitRaised:
    """limit設定がcounter snapshot超に引き上げられたことの検証（C-10 / C-11）。"""

    resolution: BlockResolutionEvidence


@dataclass(frozen=True)
class BlockResolvedIntervention:
    """BLOCK_INTERVENTION recordのcanonical検証（C-06 / C-11。2経路合流）。

    canonical検証済みのBLOCK_INTERVENTION record自体を必ず伴う（recordなしの解消は
    構築できない。AC-C01-11のcanonical record gate）。
    """

    resolution: BlockResolutionEvidence

    def __post_init__(self) -> None:
        record = self.resolution.record
        if record is None or record.kind is not RecordKind.BLOCK_INTERVENTION:
            raise IllegalEventError(
                "INTERVENTION_RECORD", "解消evidenceはBLOCK_INTERVENTION recordのcanonical検証を必ず伴う"
            )


@dataclass(frozen=True)
class IntegrityRestoredValidated:
    """canonical chainの整合性復元と同一chainの再検証（C-06 / C-07）。"""

    resolution: BlockResolutionEvidence


@dataclass(frozen=True)
class IntegritySalvageEstablished:
    """明示salvageによる新baseline確立の検証（C-07。供給はPhase 14以降）。"""

    resolution: BlockResolutionEvidence


PreflightEvent = PreflightOk | PreflightNg

Event = (
    RecordProduced
    | ReviewBlockingVerified
    | ReviewApprovedVerified
    | FixResultVerified
    | ClarificationQuestionVerified
    | ClarificationConfirmedVerified
    | ClarificationRevisedVerified
    | ClarificationWithdrawnVerified
    | ClarificationEscalatedVerified
    | DecisionRequestVerified
    | VerdictAskUserVerified
    | VerdictProceedVerified
    | VerdictResubmitVerified
    | DecisionBriefVerified
    | DecisionRecordVerified
    | UserDecisionVerified
    | ExternalDependencyVerified
    | ToolPermissionBlocked
    | CiTimeoutRecorded
    | CiCodeFailureVerified
    | ReportVerified
    | GateQuestionVerified
    | GateAnswerVerified
    | GateChangesVerified
    | MergeApprovalVerified
    | UserCancelVerified
    | IntegrityIncidentVerified
    | FixStarted
    | PermissionResumeValidated
    | CiSucceeded
    | CiInfraFailure
    | CiResumeRequested
    | ReportFailed
    | ReporterRetryRequested
    | MergePreconditionsOk
    | MergePreconditionMismatch
    | MergeConfirmed
    | MergeNotExecutedConfirmed
    | MergeOutcomeUnknown
    | HeadChangedExternally
    | CancellationCompleted
    | BlockHaltCompleted
    | RecordIntegrityViolationDetected
    | RunFailed
    | ResumeValidated
    | ResumeFallbackRequired
    | ResumeSameHeadValidated
    | BlockResolvedLimitRaised
    | BlockResolvedIntervention
    | IntegrityRestoredValidated
    | IntegritySalvageEstablished
)
