# SPDX-License-Identifier: Apache-2.0
"""C-01が返すcommand。記述であり、C-01は実行しない。

command列の順序は決定論的で、条件分岐の意味を持たない（条件で結果が分かれる処理は、
必ず結果eventを受けて次のruleが判断する）。実行componentは旧設計3.8のとおり。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique

from .values import OpaqueBinding, PendingRecord, RecordKind


@unique
class CodexPurpose(Enum):
    """Codex起動のtyped purpose（判断1）。"""

    CODE_REVIEW = "CODE_REVIEW"
    CLARIFICATION = "CLARIFICATION"
    DECISION_VERDICT = "DECISION_VERDICT"


@unique
class HostAction(Enum):
    """active hostへの作業依頼の種別。"""

    APPLY_FINDINGS = "APPLY_FINDINGS"
    DRAFT_DECISION_REQUEST = "DRAFT_DECISION_REQUEST"
    DRAFT_DECISION_BRIEF = "DRAFT_DECISION_BRIEF"
    RECORD_DECISION = "RECORD_DECISION"
    REVISE_DECISION_REQUEST = "REVISE_DECISION_REQUEST"
    ANSWER_GATE_QUESTION = "ANSWER_GATE_QUESTION"


@dataclass(frozen=True)
class RequestCodexReview:
    """fresh reviewerの起動（C-09）。"""

    purpose: CodexPurpose


@dataclass(frozen=True)
class RequestHostAction:
    """active hostへの作業依頼（C-08）。"""

    action: HostAction


@dataclass(frozen=True)
class PersistRecord:
    """canonical recordの投稿と検証。冪等であることをC-05へ要求する。"""

    kind: RecordKind
    binding: OpaqueBinding


@dataclass(frozen=True)
class CheckCi:
    """対象headのCI確認（C-12）。"""


@dataclass(frozen=True)
class GenerateReport:
    """final reporterの起動（C-12）。"""


@dataclass(frozen=True)
class HaltRun:
    """active process treeの停止とcheckpoint保存（C-03 / C-08）。cancelまたはblock attemptへbindする。"""

    binding: OpaqueBinding


@dataclass(frozen=True)
class VerifyMergePreconditions:
    """merge直前の全条件再検証（C-13）。"""


@dataclass(frozen=True)
class ExecuteMerge:
    """merge実行（C-13）。発行経路はpreconditions一致eventの消費を伴う単一ruleに限る。"""


@dataclass(frozen=True)
class QueryMergeOutcome:
    """merge結果の照会（C-13）。"""


@dataclass(frozen=True)
class InvalidateApprovals:
    """review / merge承認の失効。冪等であることをC-07へ要求する。"""


@dataclass(frozen=True)
class RecordIntegrityIncident:
    """INTEGRITY_INCIDENT recordの作成依頼（作成はC-06 / C-07、投稿はPersistRecord経由）。

    payloadはdeferred集合と監査参照で、C-01がMachineStateから決定論的に構成する（判断12）。
    """

    violation_bindings: tuple[OpaqueBinding, ...]
    audit: PendingRecord | None


Command = (
    RequestCodexReview
    | RequestHostAction
    | PersistRecord
    | CheckCi
    | GenerateReport
    | HaltRun
    | VerifyMergePreconditions
    | ExecuteMerge
    | QueryMergeOutcome
    | InvalidateApprovals
    | RecordIntegrityIncident
)
