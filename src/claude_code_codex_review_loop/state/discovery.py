# SPDX-License-Identifier: Apache-2.0
"""resume対象runの候補列挙と決定論的な選択（C-07。ADR-0012）。

resumeは「どのrunを再開するのか」を最初に決める必要がある。ここは2段に分ける。

1. **候補の列挙**（`enumerate_run_candidates`）: state root配下のrun directoryと、
   GitHub側commentのmarkerが名乗るrun IDを集める。markerの解析はC-06の
   `parse_record_marker`へ委譲し、**構造parseの結果を候補の名前にしか使わない**
   （actor / chainを検証していないため、判断根拠にはできない）
2. **選択**（`select_run`）: 検証済みchain（`ChainVerification`）とcheckpointの読込結果
   だけを見て、**非terminalな候補がちょうど1つの場合だけ**選ぶ。0件・2件以上・
   判断できない状態は理由つきで停止する（推測して前進しない）

terminal判定に使うのは構造的signalだけで、eventへ変換して状態機械を回さない
（対応表はC-10 / C-11。Issue #12の実装契約）。chainにviolationがある場合はGitHub側の
signalを使わず、checkpointのstateだけで判定する（壊れた系列を根拠に終端と決めない）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..domain.states import TERMINAL_STATES, State
from ..domain.values import RecordKind
from ..identity.errors import IdentityError
from ..identity.record_chain import ChainPayload, ChainVerification
from ..identity.record_chain import parse_record_marker as _parse_marker
from ..schema.projection import ProjectionError, validate_run_id
from ..transport.conversation import UnverifiedComment
from ..transport.marker import MARKER_TOKEN
from .paths import CHECKPOINT_FILE_NAME, StatePaths
from .store import CheckpointLoaded, CheckpointLoadResult, CheckpointMissing, load_checkpoint

# GitHub側の構造的な終端signal（検証済みchainでのみ使う）
_TERMINAL_LAST_KINDS: Final = frozenset({RecordKind.FINAL_REPORT})
_TERMINAL_ANY_KINDS: Final = frozenset({RecordKind.USER_CANCEL})


class RunDiscoveryError(IdentityError):
    """候補の構成が呼び出し側の誤り（どの情報源からも観測されていない候補等）。"""


@dataclass(frozen=True)
class LocalRun:
    """state root配下のrun directoryと、そのcheckpointの読込結果。"""

    run_id: str
    path: Path
    result: CheckpointLoadResult


@dataclass(frozen=True)
class GithubRun:
    """GitHub側markerが名乗るrun（**未検証**。候補の名前としてのみ使う）。"""

    run_id: str
    record_count: int
    max_seq: int


@dataclass(frozen=True)
class RunCandidates:
    """列挙結果。unattributable_markersは解析できずrunへ帰属できないmarkerの件数。"""

    run_ids: tuple[str, ...]
    local: tuple[LocalRun, ...]
    github: tuple[GithubRun, ...]
    unattributable_markers: int
    unrelated_local: tuple[str, ...]


def _is_inside_runs(entry: Path, runs_dir: Path) -> bool:
    """entryが`runs/`直下の実体か（symlink等でstate root外を指していないか）。"""
    return entry.resolve().parent == runs_dir


def discover_local_runs(paths: StatePaths) -> tuple[LocalRun, ...]:
    """state root配下のrun directoryを**作成せずに**列挙する（run ID昇順）。

    run IDとして不正な名前のentryと、解決先が`runs/`の外になるentry（symlink等）は
    対象外にする。`runs/`直下の異物をrunと解釈せず、state root外のfileをcheckpointとして
    読まない（`state.paths`のcontainment契約と揃える）。checkpointの読込結果は
    `load_checkpoint`の直和をそのまま保持し、読めないcheckpointを「無いもの」に
    丸めない（silent repair禁止）。
    """
    if not paths.runs_dir.is_dir():  # pragma: no cover - prepare_state_rootが作成済み
        return ()
    runs: list[LocalRun] = []
    for entry in sorted(paths.runs_dir.iterdir()):
        if not entry.is_dir() or not _is_inside_runs(entry, paths.runs_dir):
            continue
        try:
            run_id = validate_run_id(entry.name)
        except ProjectionError:
            continue
        checkpoint = entry / CHECKPOINT_FILE_NAME
        runs.append(LocalRun(run_id=run_id, path=checkpoint, result=load_checkpoint(checkpoint)))
    return tuple(runs)


def _marker_payloads(comments: tuple[UnverifiedComment, ...]) -> tuple[list[ChainPayload], int]:
    """予約tokenを含むcommentを構造parseし、解析できたpayloadと不能件数を返す。"""
    payloads: list[ChainPayload] = []
    unattributable = 0
    for comment in comments:
        if MARKER_TOKEN not in comment.body.upper():
            continue
        parsed = _parse_marker(comment)
        if isinstance(parsed, str):
            unattributable += 1
            continue
        payloads.append(parsed)
    return payloads, unattributable


def enumerate_github_runs(comments: tuple[UnverifiedComment, ...]) -> tuple[tuple[GithubRun, ...], int]:
    """commentのmarkerからrun候補を集約する（run ID昇順）と、帰属不能markerの件数。"""
    payloads, unattributable = _marker_payloads(comments)
    counts: dict[str, list[int]] = {}
    for payload in payloads:
        entry = counts.setdefault(payload.run, [0, 0])
        entry[0] += 1
        entry[1] = max(entry[1], payload.seq)
    runs = tuple(
        GithubRun(run_id=run_id, record_count=count, max_seq=max_seq)
        for run_id, (count, max_seq) in sorted(counts.items())
    )
    return runs, unattributable


def _matches_target(result: CheckpointLoadResult, *, repository: str, number: int) -> bool:
    """checkpointが対象repository / 番号のものか（読めた場合だけ判定できる）。"""
    if not isinstance(result, CheckpointLoaded):
        return False
    payload = result.payload
    return payload.get("repository") == repository and payload.get("number") == number


def enumerate_run_candidates(
    paths: StatePaths,
    comments: tuple[UnverifiedComment, ...],
    *,
    repository: str,
    number: int,
) -> RunCandidates:
    """local / GitHubの両側からrun候補を集める。

    候補は「GitHub側markerが名乗るrun」と「checkpointが対象repository / 番号を指すrun」の
    和集合。どちらにも該当しないlocal runは`unrelated_local`として件数を残すだけで候補に
    しない（別repositoryのrunや読めないcheckpointが、無関係なresumeを止めないため）。
    """
    local = discover_local_runs(paths)
    github, unattributable = enumerate_github_runs(comments)
    github_ids = {run.run_id for run in github}
    candidates = set(github_ids)
    unrelated: list[str] = []
    for run in local:
        if run.run_id in github_ids or _matches_target(run.result, repository=repository, number=number):
            candidates.add(run.run_id)
        else:
            unrelated.append(run.run_id)
    return RunCandidates(
        run_ids=tuple(sorted(candidates)),
        local=local,
        github=github,
        unattributable_markers=unattributable,
        unrelated_local=tuple(unrelated),
    )


@dataclass(frozen=True)
class RunSummary:
    """1 run分の検証済み観測。少なくとも一方の情報源が要る。"""

    run_id: str
    verification: ChainVerification | None = None
    checkpoint: CheckpointLoadResult | None = None

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        if self.verification is None and self.checkpoint is None:
            raise RunDiscoveryError("summary", f"どの情報源からも観測されていない候補: {self.run_id}")


@dataclass(frozen=True)
class RunStatus:
    """候補ごとの判定結果（選択されなかった候補も同じ形で提示する）。"""

    run_id: str
    terminal: bool
    terminal_reason: str | None
    max_seq: int
    intact: bool
    checkpoint_state: State | None


@dataclass(frozen=True)
class RunSelected:
    """再開対象が一意に決まった。"""

    status: RunStatus
    considered: tuple[RunStatus, ...]


@dataclass(frozen=True)
class RunAmbiguous:
    """非terminalな候補が複数ある（推測せず停止し、候補を提示する）。"""

    candidates: tuple[RunStatus, ...]
    detail: str


@dataclass(frozen=True)
class RunNotFound:
    """再開できる候補が無い（全てterminal、または候補自体が無い）。"""

    considered: tuple[RunStatus, ...]
    detail: str


@dataclass(frozen=True)
class RunUnavailable:
    """候補のcheckpointが読めない等、判断材料が揃わない（fresh resumeへ迂回しない）。"""

    run_id: str
    detail: str


RunSelection = RunSelected | RunAmbiguous | RunNotFound | RunUnavailable


def _checkpoint_state(result: CheckpointLoadResult | None) -> State | None:
    """checkpointが記録するstate（読めない・未記録ならNone）。"""
    if not isinstance(result, CheckpointLoaded):
        return None
    section = result.payload.get("state")
    if not isinstance(section, dict):
        return None
    value = section.get("state")
    return State(value) if isinstance(value, str) else None


def _terminal_reason(verification: ChainVerification | None, state: State | None) -> str | None:
    """終端と判定できる構造的signal（無ければNone）。"""
    if state is not None and state in TERMINAL_STATES:
        return f"checkpointのstateが終端（{state.value}）"
    if verification is None or not verification.is_intact:
        # violationのあるchainを根拠に終端と決めない（検証済みsignalだけを使う）
        return None
    kinds = {record.kind for record in verification.records}
    terminal_any = sorted(kind.value for kind in kinds & _TERMINAL_ANY_KINDS)
    if terminal_any:
        return f"終端recordが投稿済み（{','.join(terminal_any)}）"
    if verification.records and verification.records[-1].kind in _TERMINAL_LAST_KINDS:
        return f"最終recordが{verification.records[-1].kind.value}"
    return None


def _status(summary: RunSummary) -> RunStatus:
    state = _checkpoint_state(summary.checkpoint)
    reason = _terminal_reason(summary.verification, state)
    return RunStatus(
        run_id=summary.run_id,
        terminal=reason is not None,
        terminal_reason=reason,
        max_seq=0 if summary.verification is None else summary.verification.max_seq,
        intact=summary.verification.is_intact if summary.verification is not None else False,
        checkpoint_state=state,
    )


def select_run(summaries: tuple[RunSummary, ...]) -> RunSelection:
    """検証済み観測から再開対象を決める（非terminalがちょうど1つの場合だけ選ぶ）。

    checkpointが存在するのに読めない候補は、cacheとの照合ができず「壊れたcheckpointを
    無いものとして扱う」ことになるため、選択せず`RunUnavailable`で停止する。
    """
    for summary in summaries:
        result = summary.checkpoint
        if result is not None and not isinstance(result, CheckpointLoaded | CheckpointMissing):
            return RunUnavailable(
                run_id=summary.run_id,
                detail=f"checkpointを解釈できない（{type(result).__name__}）",
            )
    statuses = tuple(sorted((_status(summary) for summary in summaries), key=lambda item: item.run_id))
    resumable = tuple(status for status in statuses if not status.terminal)
    if len(resumable) == 1:
        return RunSelected(status=resumable[0], considered=statuses)
    if len(resumable) > 1:
        return RunAmbiguous(
            candidates=resumable,
            detail=f"再開できるrun候補が{len(resumable)}件ある（{','.join(s.run_id for s in resumable)}）",
        )
    return RunNotFound(
        considered=statuses,
        detail="再開できるrun候補が無い" if statuses else "run候補が観測されていない",
    )
