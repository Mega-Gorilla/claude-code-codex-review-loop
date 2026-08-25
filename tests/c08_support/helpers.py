# SPDX-License-Identifier: Apache-2.0
"""C-08 step engine testの共有helper。

engineへ渡すport（fake）と、run directory・checkpoint・result fileの用意を集約する。
時刻とIDは製品codeが生成源を持たないため、testからも固定値を注入する。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from c02_support.helpers import REPRESENTATIVE
from c05_support.helpers import make_context, make_policy, read_state, seed_state
from c06_support.helpers import HEAD, PRODUCER, seed_dict
from c07_support.helpers import (
    NUMBER,
    REPOSITORY,
    RUN,
    chain_comments_of,
    checkpoint_payload,
    state_paths,
    verified_chain,
)

from claude_code_codex_review_loop.domain.events import Event
from claude_code_codex_review_loop.domain.values import (
    Awaiting,
    IntegrityEvidenceRef,
    MachineState,
    OpaqueBinding,
    OpaqueFingerprint,
    OpaqueRef,
    OpaqueSnapshot,
    PendingRecord,
    Progress,
    ProgressReport,
    RecordEvidence,
    RecordKind,
    State,
)
from claude_code_codex_review_loop.identity import ProducerAllowlist, verify_record_chain
from claude_code_codex_review_loop.identity.fs_permissions import replace_private_text
from claude_code_codex_review_loop.identity.record_chain import ChainVerification, VerifiedRecord
from claude_code_codex_review_loop.schema import SchemaKind
from claude_code_codex_review_loop.state import StatePaths, checkpoint_path, run_directory, save_checkpoint
from claude_code_codex_review_loop.transport import RepoRef
from claude_code_codex_review_loop.transport.conversation import UnverifiedComment, fetch_comments_since
from claude_code_codex_review_loop.transport.gh import GhContext, RetryPolicy
from claude_code_codex_review_loop.workflow import (
    RESULT_VARIANTS,
    IssuedTransaction,
    build_event,
    issue_transaction,
    transaction_section,
    with_machine_state,
)
from claude_code_codex_review_loop.workflow.ports import ActionContext

ISSUED_AT = "2026-08-25T09:00:00Z"
ACCEPTED_AT = "2026-08-25T09:05:00Z"
SPEAKER = "Claude Code"
MODEL = "claude-opus-5"
NEW_HEAD = "b" * 40
MAX_RESULT_BYTES = 65_536
RETRY_BUDGET = 2
# recordの公開本文（C-05のprepare_public_bodyが作る形。persist testでは固定値で足りる）
RECORD_BODY = "**Claude Code**（model: claude-opus-5）" + chr(10) * 2 + "回答です。"


class FakeIds:
    """`id_source`のfake（呼ばれた順に安定したIDを返す）。"""

    def __init__(self, prefix: str = "id") -> None:
        self.prefix = prefix
        self.issued: list[str] = []

    def __call__(self) -> str:
        value = f"{self.prefix}-{len(self.issued) + 1}"
        self.issued.append(value)
        return value


@dataclass
class FakePayloadPort:
    """action payloadを供給する（C-10 / C-11が本実装を持つ）。"""

    payload: Mapping[str, object] = field(default_factory=lambda: {"round": 1, "finding_ids": ["F-1"]})
    calls: list[ActionContext] = field(default_factory=list)

    def payload_for(self, context: ActionContext) -> Mapping[str, object]:
        self.calls.append(context)
        return self.payload


@dataclass
class FakeEvidencePort:
    """actionの根拠recordを供給する。"""

    records: Sequence[VerifiedRecord] = ()

    def evidence_for(self, context: ActionContext) -> Sequence[VerifiedRecord]:
        return self.records


@dataclass
class FakeRecordSource:
    """当該runのchain検証結果（C-06の出力そのもの）。"""

    records: Sequence[VerifiedRecord] = ()
    violations: tuple[IntegrityEvidenceRef, ...] = ()

    def chain(self, run_id: str) -> ChainVerification:
        records = tuple(self.records)
        max_seq = max((record.seq for record in records), default=0)
        return ChainVerification(
            records=records,
            violations=self.violations,
            max_seq=max_seq,
            assurance_high_water=max_seq,
        )


@dataclass
class GithubBackedRecords:
    """GitHubから取得してC-06のchain検証を通すport（adapterの本実装と同じ経路）。

    fixtureへ検証結果を直書きせず、**製品関数だけ**でchainを作る。投稿の前後で結果が
    変わるため、persistの「投稿 -> 再検証」を実際の経路で確かめられる。
    """

    context: GhContext
    repo: RepoRef
    number: int
    policy: RetryPolicy
    detection_head: str = HEAD
    max_pages: int = 5

    def chain(self, run_id: str) -> ChainVerification:
        fetched = fetch_comments_since(
            self.context, self.repo, self.number, None, policy=self.policy, max_pages=self.max_pages
        )
        return verify_record_chain(
            fetched.comments,
            run_id=run_id,
            detection_head=self.detection_head,
            producers=ProducerAllowlist(logins=frozenset({PRODUCER})),
            checkpoint=None,
            probes={},
        )


@dataclass
class FakeRecordEvents:
    """検証済みrecordからC-01 eventを作るport（本実装はC-10 / C-11）。

    host actionの結果はregistryの`build_event`をそのまま使う（1対1の対応がある）。
    `extra_event_inputs`（`ProgressReport` / `head`）はC-08が作らない値なのでここで供給する。
    """

    report: ProgressReport = field(
        default_factory=lambda: ProgressReport(
            progress=Progress.CONTINUE,
            head=OpaqueRef("head-audit"),
            counter_snapshot=OpaqueSnapshot("snap-1"),
            fingerprint=OpaqueFingerprint("fp-1"),
        )
    )
    head: OpaqueRef = OpaqueRef("head-audit")
    events: Mapping[RecordKind, Event] = field(default_factory=dict)

    def event_for(self, evidence: RecordEvidence, record: VerifiedRecord) -> Event:
        override = self.events.get(record.kind)
        if override is not None:
            return override
        variant = RESULT_VARIANTS[record.kind]
        inputs: dict[str, object] = {}
        for name in variant.extra_event_inputs:
            inputs[name] = self.report if name == "report" else self.head
        return build_event(variant, evidence, inputs)


@dataclass
class FakeBodyPort:
    """検証済みpayloadから公開本文を作る（kindごとの表現はC-10 / C-11の領域）。"""

    text: str = "修正を適用しました。"

    def body_for(self, kind: RecordKind, payload: Mapping[str, object]) -> str:
        return f"{self.text}（{kind.value}）"


def review_records() -> tuple[VerifiedRecord, ...]:
    """seq=1のREVIEW_RESULTだけを持つ検証済みchain（APPLY_FINDINGSの根拠）。"""
    return verified_chain([RecordKind.REVIEW_RESULT]).records


def machine_state(
    state: State = State.APPLYING_FIXES,
    awaiting: Awaiting | None = Awaiting.HOST_APPLY_FINDINGS,
) -> MachineState:
    return MachineState(state=state, awaiting=awaiting)


@dataclass(frozen=True)
class EngineEnv:
    """engineへ渡すpathと、seedしたcheckpointの位置。"""

    paths: StatePaths
    run_dir: Path
    checkpoint: Path


def seed(
    tmp_path: Path,
    *,
    state: MachineState | None = None,
    extra: Mapping[str, object] | None = None,
) -> EngineEnv:
    """state rootとcheckpointを用意する（checkpointは製品のsave経路で書く）。"""
    paths = state_paths(tmp_path)
    payload = checkpoint_payload()
    if state is not None:
        payload = with_machine_state(payload, state)
    if extra is not None:
        payload.update(extra)
    save_checkpoint(checkpoint_path(paths, RUN), payload)
    return EngineEnv(
        paths=paths, run_dir=run_directory(paths, RUN), checkpoint=checkpoint_path(paths, RUN)
    )


@dataclass(frozen=True)
class PersistEnv:
    """`persist`を呼ぶための一式（GitHub側の状態とcheckpointをseed済み）。"""

    paths: StatePaths
    directory: Path
    context: GhContext
    repo: RepoRef
    policy: RetryPolicy
    issued: IssuedTransaction
    posted: tuple[UnverifiedComment, ...]

    def kwargs(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "paths": self.paths,
            "run_id": RUN,
            "repository": REPOSITORY,
            "number": NUMBER,
            "context": self.context,
            "repo": self.repo,
            "records_port": GithubBackedRecords(
                context=self.context, repo=self.repo, number=NUMBER, policy=self.policy
            ),
            "event_port": FakeRecordEvents(),
            "policy": self.policy,
            "search_since": None,
            "search_attempts": 1,
            "search_backoff_seconds": 0.0,
            "search_max_pages": 5,
        }
        values.update(overrides)
        return values

    def comment_count(self) -> int:
        comments = read_state(self.directory)["comments"]
        assert isinstance(comments, list)
        return len(comments)


def gate_answer_payload(head: str = HEAD) -> dict[str, object]:
    payload = dict(REPRESENTATIVE[SchemaKind.GATE_ANSWER])
    payload["target_head_sha"] = head
    return payload


def persist_env(
    tmp_path: Path,
    *,
    state: MachineState | None = None,
    kind: RecordKind = RecordKind.GATE_ANSWER,
    payload: Mapping[str, object] | None = None,
    body: str = RECORD_BODY,
    with_transaction: bool = True,
    scenario: str = "ok",
    timeout_seconds: float = 30.0,
) -> PersistEnv:
    """seq=1のREVIEW_RESULTを投稿済みにし、seq=2のtransactionを中断中として置く。"""
    directory = tmp_path / "gh"
    directory.mkdir(parents=True, exist_ok=True)
    posted = chain_comments_of([RecordKind.REVIEW_RESULT])
    seed_state(directory, comments=[seed_dict(comment, issue=NUMBER) for comment in posted])
    context = make_context(directory, scenario=scenario, timeout_seconds=timeout_seconds)
    policy = make_policy()
    issued = issue_transaction(
        kind=kind,
        payload=gate_answer_payload() if payload is None else payload,
        run_id=RUN,
        head_sha=HEAD,
        body=body,
        records=verified_chain([RecordKind.REVIEW_RESULT]).records,
    )
    assert isinstance(issued, IssuedTransaction), issued
    machine_state = (
        state
        if state is not None
        else MachineState(
            state=State.READY_FOR_HUMAN_MERGE,
            pending_record=PendingRecord(
                kind=kind,
                binding=OpaqueBinding(issued.binding),
                source_state=State.READY_FOR_HUMAN_MERGE,
            ),
        )
    )
    paths = state_paths(tmp_path)
    checkpoint = with_machine_state(checkpoint_payload(), machine_state)
    if with_transaction:
        checkpoint["transaction"] = transaction_section(issued)
    save_checkpoint(checkpoint_path(paths, RUN), checkpoint)
    return PersistEnv(
        paths=paths,
        directory=directory,
        context=context,
        repo=RepoRef(owner="owner", name="repo"),
        policy=policy,
        issued=issued,
        posted=posted,
    )


def fix_result_payload(*, pre: str = HEAD, pushed: str = NEW_HEAD) -> dict[str, object]:
    """代表的なFIX_RESULT payload（corpusのrepresentativeをheadだけ差し替える）。"""
    payload = dict(REPRESENTATIVE[SchemaKind.FIX_RESULT])
    payload["pre_head_sha"] = pre
    payload["pushed_head_sha"] = pushed
    return payload


def failure_payload(*, category: str = "TRANSIENT", action_kind: str = "APPLY_FINDINGS") -> dict[str, object]:
    payload = dict(REPRESENTATIVE[SchemaKind.HOST_FAILURE])
    payload["error_category"] = category
    payload["action_kind"] = action_kind
    return payload


def write_result(run_dir: Path, relative: str, payload: Mapping[str, object]) -> str:
    """result fileを作成者限定で書き、その内容hashを返す（既存があれば置き換える）。"""
    text = json.dumps(payload, ensure_ascii=False)
    target = run_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    replace_private_text(target, text)
    return hashlib.sha256(target.read_bytes()).hexdigest()


def submit_payload(
    *,
    action_id: str,
    nonce: str,
    result_hash: str,
    action_kind: str = "APPLY_FINDINGS",
    outcome: str = "COMPLETED",
    result_kind: str | None = "FIX_RESULT",
    error_category: str | None = None,
    run_id: str = RUN,
    head: str = HEAD,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 2,
        "run_id": run_id,
        "action_id": action_id,
        "action_kind": action_kind,
        "expected_head_sha": head,
        "nonce": nonce,
        "result_hash": result_hash,
        "outcome": outcome,
    }
    if result_kind is not None:
        payload["result_kind"] = result_kind
    if error_category is not None:
        payload["error_category"] = error_category
    return payload


def raw(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


__all__ = [
    "ACCEPTED_AT",
    "HEAD",
    "ISSUED_AT",
    "MAX_RESULT_BYTES",
    "MODEL",
    "NEW_HEAD",
    "NUMBER",
    "REPOSITORY",
    "RETRY_BUDGET",
    "RUN",
    "SPEAKER",
    "EngineEnv",
    "PersistEnv",
    "FakeBodyPort",
    "FakeEvidencePort",
    "FakeIds",
    "FakePayloadPort",
    "FakeRecordEvents",
    "GithubBackedRecords",
    "FakeRecordSource",
    "failure_payload",
    "fix_result_payload",
    "gate_answer_payload",
    "machine_state",
    "persist_env",
    "raw",
    "review_records",
    "seed",
    "submit_payload",
    "write_result",
]
