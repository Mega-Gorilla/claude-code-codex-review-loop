# SPDX-License-Identifier: Apache-2.0
"""C-08 active host protocolとstep engine（Phase 8）。

Controller CLIはClaude Code sessionの子processであり、親のLLM turnを呼び戻せない。
そのためcore engineはClaudeを起動せず、`advance`で次の`HOST_ACTION`を返し、active hostが
自分のcontextで実行して`submit`で結果を返す**step engine**とする（implementation plan
Section 2の制御反転）。この構造は全workflowの前提で、覆すと全体の書き直しになる。

**同一runに対する制御経路は`advance`と`submit`の2つだけ**である（AC-C08-03）。本packageは
他にstateを進めるentry pointを公開しない。registryの正本はADR-0014、engineはADR-0015。
adapterとprocess境界（AC-C08-04 / 06）は後続PRが追加する。
"""

from .actions import (
    ACTION_SPECS,
    RESULT_VARIANTS,
    ActionRegistryError,
    ActionSpec,
    ResultVariant,
    spec_for,
    spec_for_awaiting,
    spec_for_kind,
)
from .checkpoint_view import (
    PendingAction,
    SectionUnavailable,
    SubmitReceipt,
    find_receipt,
    next_attempt,
    read_machine_state,
    read_pending_action,
    read_receipts,
    with_machine_state,
    with_pending_action,
    with_receipt,
    without_pending_action,
)
from .engine import (
    AdvanceOutcome,
    AwaitUser,
    EngineStopped,
    HostActionIssued,
    PersistRequired,
    SubmitAccepted,
    SubmitOutcome,
    SubmitReplayed,
    Terminal,
    advance,
    submit,
)
from .ports import ActionContext, ActionPayloadPort, EvidencePort, RecordBodyPort, RecordSourcePort
from .results import ResultAccepted, ResultOutcome, ResultRejected, read_result
from .transaction import (
    IssuedTransaction,
    TransactionOutcome,
    TransactionUnavailable,
    issue_transaction,
    next_sequence,
    transaction_section,
)

__all__ = [
    "ACTION_SPECS",
    "RESULT_VARIANTS",
    "ActionContext",
    "ActionPayloadPort",
    "ActionRegistryError",
    "ActionSpec",
    "AdvanceOutcome",
    "AwaitUser",
    "EngineStopped",
    "EvidencePort",
    "HostActionIssued",
    "IssuedTransaction",
    "PendingAction",
    "PersistRequired",
    "RecordBodyPort",
    "RecordSourcePort",
    "ResultAccepted",
    "ResultOutcome",
    "ResultRejected",
    "ResultVariant",
    "SectionUnavailable",
    "SubmitAccepted",
    "SubmitOutcome",
    "SubmitReceipt",
    "SubmitReplayed",
    "Terminal",
    "TransactionOutcome",
    "TransactionUnavailable",
    "advance",
    "find_receipt",
    "issue_transaction",
    "next_attempt",
    "next_sequence",
    "read_machine_state",
    "read_pending_action",
    "read_receipts",
    "read_result",
    "spec_for",
    "spec_for_awaiting",
    "spec_for_kind",
    "submit",
    "transaction_section",
    "with_machine_state",
    "with_pending_action",
    "with_receipt",
    "without_pending_action",
]
