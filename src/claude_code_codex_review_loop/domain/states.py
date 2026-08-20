# SPDX-License-Identifier: Apache-2.0
"""可視の17 stateとその分類。

正本はtarget experienceの「State model」節。分類（terminal / resumable / active）は
Phase 1計画の節4に従う。
"""

from enum import Enum, unique


@unique
class State(Enum):
    """workflowの可視state（17値）。"""

    RUNNING_REVIEW = "RUNNING_REVIEW"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    APPLYING_FIXES = "APPLYING_FIXES"
    CLARIFYING_REVIEW = "CLARIFYING_REVIEW"
    REVIEWING_DECISION_REQUEST = "REVIEWING_DECISION_REQUEST"
    AWAITING_USER_DECISION = "AWAITING_USER_DECISION"
    AWAITING_TOOL_PERMISSION = "AWAITING_TOOL_PERMISSION"
    WAITING_CI = "WAITING_CI"
    GENERATING_REPORT = "GENERATING_REPORT"
    READY_FOR_HUMAN_MERGE = "READY_FOR_HUMAN_MERGE"
    MERGING = "MERGING"
    MERGED = "MERGED"
    MERGE_FAILED = "MERGE_FAILED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REPORT_FAILED = "REPORT_FAILED"


TERMINAL_STATES: frozenset[State] = frozenset({State.MERGED, State.CANCELLED})

RESUMABLE_STATES: frozenset[State] = frozenset(
    {
        State.WAITING_CI,
        State.AWAITING_USER_DECISION,
        State.AWAITING_TOOL_PERMISSION,
        State.READY_FOR_HUMAN_MERGE,
        State.BLOCKED,
        State.FAILED,
        State.REPORT_FAILED,
        State.MERGE_FAILED,
    }
)

ACTIVE_STATES: frozenset[State] = frozenset(State) - TERMINAL_STATES - RESUMABLE_STATES
