# SPDX-License-Identifier: Apache-2.0
"""resume contextの組み立て（AC-C07-01 / 02 / 06。C-07 / ADR-0013）。

PR-1〜PR-3の部品（record codec / checkpoint store / run discovery / head reconciliation /
artifact binding）を配線し、**再開の判断材料**を1つの構造化結果として返す。

- **純粋なcore**（`build_resume_context`）と**薄いI/O収集**（`observe_resume`）に分ける。
  C-06の`verify_record_chain` + `probe_known_records`と同じ構造で、判定はfixtureだけで
  決定論的に検証できる
- Phase 7の成果物は**resume context**であり、`MachineState`の完全replayではない。
  record -> C-01 eventの対応表はC-10 / C-11が持つ（Issue #12の実装契約）
- **どの段階でも推測して前進しない**。run候補が曖昧 / chainにviolation / headを観測できない /
  承認を確認できない / 再発行の可否を決められない / 直接回答が複数、はいずれも
  `ResumeStopped`として理由と**原因の値そのもの**を返す
- artifactの不一致は停止ではなく**cacheの破棄**として結果へ載せる（GitHub側が上位）
- pendingとheadの優先順位は決めない。C-01のR-P（pending保持中の明示resumeは永続化確認の
  再発行のみ）に従ってC-10が順序を決める
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, unique

from ..identity.allowlist import DecisionAllowlistState, DecisionContext, ProducerAllowlist
from ..identity.errors import IdentityError
from ..identity.record_chain import (
    ChainCheckpoint,
    ChainVerification,
    KnownRecord,
    VerifiedRecord,
    probe_known_records,
    verify_record_chain,
)
from ..transport.conversation import UnverifiedComment, fetch_comments_since
from ..transport.gh import GhContext, RepoRef, RetryPolicy
from ..transport.pull_request import UnverifiedPullRequest, get_pull_request
from .artifacts import ArtifactCheck, artifact_content_hash, read_artifact_bindings, verify_artifact_bindings
from .direct_answer import (
    DirectAnswerAmbiguous,
    DirectAnswerOutcome,
    DirectAnswerUnavailable,
    enumerate_direct_answers,
)
from .discovery import (
    RunAmbiguous,
    RunNotFound,
    RunSelected,
    RunStatus,
    RunSummary,
    RunUnavailable,
    enumerate_run_candidates,
    select_run,
)
from .paths import StatePaths
from .pending import (
    PendingAbsent,
    PendingOutcome,
    PendingUnavailable,
    evaluate_pending,
    read_transaction,
)
from .reconcile import (
    HeadReconciliation,
    HeadUnobservable,
    ReconciliationStopped,
    ResumeVerdict,
    observe_head,
    read_checkpoint_heads,
    reconcile_head,
)
from .store import CheckpointLoaded


@dataclass(frozen=True)
class ResumeObservation:
    """resumeの判断に使う観測（すべてGitHubまたはlocal cache由来の値）。

    `artifact_digest`は(run ID, 記録path) -> content hashで、coreを純粋に保つために
    注入する。直接回答を評価する場合は`decision_context`と`decision_allowlist`を対で渡す
    （期待するuser-input record種別の決定はC-10 / C-11の責務）。
    """

    repository: str
    number: int
    pull: UnverifiedPullRequest
    comments: tuple[UnverifiedComment, ...]
    summaries: tuple[RunSummary, ...]
    artifact_digest: Callable[[str, str], str | None]
    decision_context: DecisionContext | None = None
    decision_allowlist: DecisionAllowlistState | None = None
    consumed_comment_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if (self.decision_context is None) != (self.decision_allowlist is None):
            raise IdentityError("observation", "decision contextとallowlistは対で渡す")


@unique
class ResumeStage(Enum):
    """停止した段階（消費側が理由を分類するための識別子）。"""

    RUN_SELECTION = "RUN_SELECTION"
    INTEGRITY = "INTEGRITY"
    HEAD = "HEAD"
    PENDING = "PENDING"
    DIRECT_ANSWER = "DIRECT_ANSWER"


ResumeStopCause = (
    RunAmbiguous
    | RunNotFound
    | RunUnavailable
    | ChainVerification
    | HeadUnobservable
    | ReconciliationStopped
    | PendingUnavailable
    | DirectAnswerAmbiguous
    | DirectAnswerUnavailable
)


@dataclass(frozen=True)
class ResumeStopped:
    """再開できない（推測して前進しない）。causeは停止させた値そのもの。"""

    stage: ResumeStage
    detail: str
    cause: ResumeStopCause
    run_id: str | None = None


@dataclass(frozen=True)
class ResumeContext:
    """再開の判断材料。C-10がstateと組み合わせてC-01 eventを構築する。"""

    run_id: str
    repository: str
    number: int
    status: RunStatus
    head: HeadReconciliation
    records: tuple[VerifiedRecord, ...]
    next_seq: int
    pending: PendingOutcome
    artifacts: tuple[ArtifactCheck, ...]
    direct_answer: DirectAnswerOutcome | None
    checkpoint: Mapping[str, object] | None

    @property
    def verdict(self) -> ResumeVerdict:
        """head照合の判定（eventへの写像はstateにも依存する。ADR-0012 決定17）。"""
        return self.head.verdict

    @property
    def usable_artifacts(self) -> tuple[ArtifactCheck, ...]:
        """cacheとして使ってよいartifactだけ（他は破棄済みとして提示する）。"""
        return tuple(check for check in self.artifacts if check.usable)


ResumeResult = ResumeContext | ResumeStopped


def read_chain_checkpoint(payload: Mapping[str, object]) -> ChainCheckpoint | None:
    """checkpointの`conversation`からC-06のchain checkpointを作る（無ければNone）。

    high-water markが無ければchain checkpointを主張しない（fresh resumeとして扱う）。
    seq / comment ID / body hashが揃わないentryは既知recordとして数えない。seqや
    comment IDが重複するcheckpointはC-06が呼び出し誤りとして`IdentityError`にする。
    """
    conversation = payload.get("conversation")
    if not isinstance(conversation, dict):
        return None
    high_water = conversation.get("high_water_mark")
    if isinstance(high_water, bool) or not isinstance(high_water, int):
        return None
    entries = conversation.get("records")
    known: list[KnownRecord] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        seq = entry.get("seq")
        comment_id = entry.get("comment_id")
        body_hash = entry.get("body_hash")
        if isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq <= high_water:
            continue
        if not isinstance(comment_id, str) or not isinstance(body_hash, str):
            continue
        known.append(KnownRecord(seq=seq, comment_id=comment_id, body_hash=body_hash))
    return ChainCheckpoint(high_water_mark=high_water, known_records=tuple(known))


def _selected_summary(summaries: Sequence[RunSummary], run_id: str) -> RunSummary:
    for summary in summaries:
        if summary.run_id == run_id:
            return summary
    raise IdentityError("resume", f"選択したrunのsummaryが無い: {run_id}")  # pragma: no cover


def build_resume_context(observation: ResumeObservation) -> ResumeResult:
    """観測からresume contextを組み立てる（pure）。段階ごとに停止条件を持つ。"""
    selection = select_run(observation.summaries)
    if not isinstance(selection, RunSelected):
        return ResumeStopped(
            stage=ResumeStage.RUN_SELECTION,
            detail=selection.detail,
            cause=selection,
            run_id=selection.run_id if isinstance(selection, RunUnavailable) else None,
        )
    status = selection.status
    run_id = status.run_id
    summary = _selected_summary(observation.summaries, run_id)
    verification = summary.verification
    if verification is not None and not verification.is_intact:
        return ResumeStopped(
            stage=ResumeStage.INTEGRITY,
            detail=f"chainのintegrity violationが{len(verification.violations)}件ある",
            cause=verification,
            run_id=run_id,
        )
    records = verification.records if verification is not None else ()
    payload = summary.checkpoint.payload if isinstance(summary.checkpoint, CheckpointLoaded) else None

    observed = observe_head(observation.pull)
    if isinstance(observed, HeadUnobservable):
        return ResumeStopped(
            stage=ResumeStage.HEAD, detail=observed.detail, cause=observed, run_id=run_id
        )
    reconciliation = reconcile_head(
        observed,
        records=records,
        heads=read_checkpoint_heads(payload or {}),
        checkpoint_state=status.checkpoint_state,
    )
    if isinstance(reconciliation, ReconciliationStopped):
        return ResumeStopped(
            stage=ResumeStage.HEAD, detail=reconciliation.detail, cause=reconciliation, run_id=run_id
        )

    pending: PendingOutcome = PendingAbsent()
    if payload is not None:
        transaction = read_transaction(payload)
        if isinstance(transaction, PendingUnavailable):
            return ResumeStopped(
                stage=ResumeStage.PENDING,
                detail=transaction.detail,
                cause=transaction,
                run_id=run_id,
            )
        if transaction is not None:
            pending = evaluate_pending(transaction, run_id=run_id, records=records)
            if isinstance(pending, PendingUnavailable):
                return ResumeStopped(
                    stage=ResumeStage.PENDING, detail=pending.detail, cause=pending, run_id=run_id
                )

    def _digest(path: str) -> str | None:
        return observation.artifact_digest(run_id, path)

    artifacts = verify_artifact_bindings(
        read_artifact_bindings(payload or {}),
        approved_head_sha=observed.head_sha,
        records=records,
        digest=_digest,
    )

    answer: DirectAnswerOutcome | None = None
    if observation.decision_context is not None and observation.decision_allowlist is not None:
        answer = enumerate_direct_answers(
            observation.comments,
            allowlist=observation.decision_allowlist,
            context=observation.decision_context,
            consumed_comment_ids=observation.consumed_comment_ids,
            after=records[-1].created_at if records else None,
        )
        if isinstance(answer, DirectAnswerAmbiguous | DirectAnswerUnavailable):
            detail = (
                f"有効な直接回答が{len(answer.decisions)}件ある"
                if isinstance(answer, DirectAnswerAmbiguous)
                else answer.detail
            )
            return ResumeStopped(
                stage=ResumeStage.DIRECT_ANSWER, detail=detail, cause=answer, run_id=run_id
            )

    return ResumeContext(
        run_id=run_id,
        repository=observation.repository,
        number=observation.number,
        status=status,
        head=reconciliation,
        records=records,
        next_seq=status.max_seq + 1,
        pending=pending,
        artifacts=artifacts,
        direct_answer=answer,
        checkpoint=payload,
    )


def observe_resume(
    context: GhContext,
    repo: RepoRef,
    number: int,
    *,
    paths: StatePaths,
    producers: ProducerAllowlist,
    policy: RetryPolicy,
    max_pages: int,
    cursor: str | None = None,
    decision_context: DecisionContext | None = None,
    decision_allowlist: DecisionAllowlistState | None = None,
    consumed_comment_ids: frozenset[str] = frozenset(),
) -> ResumeObservation:
    """GitHubとstate rootから観測を集める（I/O。判定は行わない）。

    取得 -> run候補の列挙 -> 候補ごとに既知recordのprobeとchain検証、までを行う。
    `max_pages`等の既定値は持たない（設定解決はC-12）。
    """
    pull = get_pull_request(context, repo, number, policy=policy)
    fetched = fetch_comments_since(context, repo, number, cursor, policy=policy, max_pages=max_pages)
    comments = fetched.comments
    candidates = enumerate_run_candidates(paths, comments, repository=repo.slug, number=number)
    present = frozenset(comment.comment_id for comment in comments)
    local = {run.run_id: run.result for run in candidates.local}
    summaries: list[RunSummary] = []
    for run_id in candidates.run_ids:
        result = local.get(run_id)
        payload = result.payload if isinstance(result, CheckpointLoaded) else None
        chain_checkpoint = read_chain_checkpoint(payload) if payload is not None else None
        probes = (
            probe_known_records(
                context, repo, chain_checkpoint, present_comment_ids=present, policy=policy
            )
            if chain_checkpoint is not None
            else {}
        )
        summaries.append(
            RunSummary(
                run_id=run_id,
                verification=verify_record_chain(
                    comments,
                    run_id=run_id,
                    detection_head=pull.head_sha,
                    producers=producers,
                    checkpoint=chain_checkpoint,
                    probes=probes,
                ),
                checkpoint=result,
            )
        )

    def _digest(run_id: str, path: str) -> str | None:
        return artifact_content_hash(paths.runs_dir / run_id, path)

    return ResumeObservation(
        repository=repo.slug,
        number=number,
        pull=pull,
        comments=comments,
        summaries=tuple(summaries),
        artifact_digest=_digest,
        decision_context=decision_context,
        decision_allowlist=decision_allowlist,
        consumed_comment_ids=consumed_comment_ids,
    )
