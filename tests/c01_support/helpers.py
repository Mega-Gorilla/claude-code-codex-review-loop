# SPDX-License-Identifier: Apache-2.0
"""C-01受入test（節8のR / C / W / X / M / I / Z系列）の共有factoryと系列runner。"""

from __future__ import annotations

from claude_code_codex_review_loop.domain import MachineState, State, initialize, transition
from claude_code_codex_review_loop.domain import events as ev
from claude_code_codex_review_loop.domain.commands import Command
from claude_code_codex_review_loop.domain.values import (
    Awaiting,
    HaltingForBlockProcedure,
    IntegrityEvidenceRef,
    OpaqueBinding,
    OpaqueFingerprint,
    OpaqueRef,
    OpaqueSnapshot,
    Progress,
    ProgressReport,
    RecordEvidence,
    RecordIntegrityBlock,
    RecordKind,
    canonicalize_integrity,
)

HEAD = OpaqueRef("head-1")


def binding(value: str) -> OpaqueBinding:
    return OpaqueBinding(value)


def evidence(kind: RecordKind, bind: str) -> RecordEvidence:
    return RecordEvidence(kind=kind, binding=binding(bind), ref=OpaqueRef(f"ref-{bind}"))


def report(
    progress: Progress = Progress.CONTINUE,
    snapshot: str = "snap-1",
    fingerprint: str = "fp-1",
) -> ProgressReport:
    return ProgressReport(
        progress=progress,
        head=HEAD,
        counter_snapshot=OpaqueSnapshot(snapshot),
        fingerprint=OpaqueFingerprint(fingerprint),
    )


def violation(bind: str = "v-1", descriptor: str = "hash-mismatch") -> IntegrityEvidenceRef:
    return IntegrityEvidenceRef(binding=binding(bind), descriptor=OpaqueRef(descriptor), head=HEAD)


def halt_gate(*binds: str, attempt: str | None = None) -> HaltingForBlockProcedure:
    """halt gate procedure。attempt bindingは既定で**最初に渡した違反**（＝検出順の1件目）。

    violation集合はcanonical order（binding昇順）へ正規化されるため、attempt bindingが
    集合の先頭とは限らない状態をtestから作れる。
    """
    refs = tuple(violation(bind) for bind in (binds or ("v-1",)))
    return HaltingForBlockProcedure(
        block=RecordIntegrityBlock(canonicalize_integrity(refs)),
        attempt_binding=binding(attempt) if attempt is not None else refs[0].binding,
    )


def names(commands: tuple[Command, ...]) -> tuple[str, ...]:
    return tuple(type(c).__name__ for c in commands)


def run(ms: MachineState, *events: ev.Event) -> tuple[MachineState, tuple[Command, ...]]:
    """event列を順に適用し、最終stateと最後のcommand列を返す。"""
    commands: tuple[Command, ...] = ()
    for event in events:
        ms, commands = transition(ms, event)
    return ms, commands


def start() -> MachineState:
    ms, _ = initialize(ev.PreflightOk())
    return ms


def produced_verified(
    ms: MachineState, kind: RecordKind, bind: str, verified: ev.Event
) -> tuple[MachineState, tuple[Command, ...]]:
    """PRODUCED -> 冪等persist -> VERIFIEDの一巡を実行する。"""
    ms, commands = transition(ms, ev.RecordProduced(kind, binding(bind)))
    assert names(commands) == ("PersistRecord",)
    return transition(ms, verified)


# --- 代表的な位置までの系列（testの前提構築に使う） ---


def to_changes_requested(ms: MachineState | None = None, bind: str = "rv-1") -> MachineState:
    ms = ms if ms is not None else start()
    blocking = ev.ReviewBlockingVerified(evidence(RecordKind.REVIEW_RESULT, bind), report())
    ms, _ = produced_verified(ms, RecordKind.REVIEW_RESULT, bind, blocking)
    assert ms.state is State.CHANGES_REQUESTED
    return ms


def to_applying_fixes(ms: MachineState | None = None) -> MachineState:
    ms = to_changes_requested(ms)
    ms, _ = transition(ms, ev.FixStarted())
    assert ms.state is State.APPLYING_FIXES
    return ms


def to_waiting_ci(bind: str = "rv-1") -> MachineState:
    ms = start()
    ms, _ = produced_verified(
        ms, RecordKind.REVIEW_RESULT, bind, ev.ReviewApprovedVerified(evidence(RecordKind.REVIEW_RESULT, bind))
    )
    assert ms.state is State.WAITING_CI
    return ms


def to_generating_report() -> MachineState:
    ms, _ = transition(to_waiting_ci(), ev.CiSucceeded())
    assert ms.state is State.GENERATING_REPORT
    return ms


def to_gate() -> MachineState:
    ms = to_generating_report()
    ms, _ = produced_verified(
        ms, RecordKind.FINAL_REPORT, "rp-1", ev.ReportVerified(evidence(RecordKind.FINAL_REPORT, "rp-1"))
    )
    assert ms.state is State.READY_FOR_HUMAN_MERGE
    return ms


def to_merging() -> MachineState:
    ms = to_gate()
    ms, commands = produced_verified(
        ms, RecordKind.MERGE_APPROVAL, "ap-1", ev.MergeApprovalVerified(evidence(RecordKind.MERGE_APPROVAL, "ap-1"))
    )
    assert ms.state is State.MERGING and names(commands) == ("VerifyMergePreconditions",)
    return ms


def to_merge_outcome(origin: Awaiting = Awaiting.MERGE_OUTCOME_EXECUTE) -> MachineState:
    """MERGINGのoutcome照会待ちへ（originはEXECUTE / CANCEL / FAILURE）。"""
    ms = to_merging()
    if origin is Awaiting.MERGE_OUTCOME_EXECUTE:
        ms, _ = transition(ms, ev.MergePreconditionsOk())
    elif origin is Awaiting.MERGE_OUTCOME_CANCEL:
        ms, _ = produced_verified(
            ms, RecordKind.USER_CANCEL, "cx-1", ev.UserCancelVerified(evidence(RecordKind.USER_CANCEL, "cx-1"))
        )
    else:
        ms, _ = transition(ms, ev.RunFailed())
    assert ms.awaiting is origin
    return ms


def to_progress_blocked(progress: Progress = Progress.LIMIT_REACHED) -> MachineState:
    """review round上限のBLOCKED（ProgressBlock）へ。"""
    ms = start()
    ms, commands = produced_verified(
        ms,
        RecordKind.REVIEW_RESULT,
        "rv-b",
        ev.ReviewBlockingVerified(evidence(RecordKind.REVIEW_RESULT, "rv-b"), report(progress)),
    )
    assert ms.state is State.BLOCKED and commands == ()
    return ms
