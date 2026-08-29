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

from claude_code_codex_review_loop.domain._rules_workflow import BLOCKED_CONTINUATIONS
from claude_code_codex_review_loop.domain.events import BlockResolvedIntervention, Event
from claude_code_codex_review_loop.domain.values import (
    Awaiting,
    BlockContext,
    BlockResolutionEvidence,
    Budget,
    CancellingProcedure,
    ExternalDependencyBlock,
    HaltingForBlockProcedure,
    IntegrityEvidenceRef,
    MachineState,
    OpaqueBinding,
    OpaqueFingerprint,
    OpaqueRef,
    OpaqueSnapshot,
    PendingRecord,
    Progress,
    ProgressBlock,
    ProgressReport,
    RecordEvidence,
    RecordIntegrityBlock,
    RecordKind,
    State,
)
from claude_code_codex_review_loop.identity import ProducerAllowlist, verify_record_chain
from claude_code_codex_review_loop.identity.fs_permissions import replace_private_text
from claude_code_codex_review_loop.identity.record_chain import ChainVerification, VerifiedRecord
from claude_code_codex_review_loop.process import (
    JobObjectRef,
    ProcessGroupRef,
    StopError,
    StopMethod,
    StopResult,
    TreeRef,
)
from claude_code_codex_review_loop.schema import SchemaKind
from claude_code_codex_review_loop.schema.projection import PROJECTION_SPECS
from claude_code_codex_review_loop.schema.user_input import HOST_TRANSCRIPT_ROUTE
from claude_code_codex_review_loop.state import StatePaths, checkpoint_path, run_directory, save_checkpoint
from claude_code_codex_review_loop.transport import RepoRef
from claude_code_codex_review_loop.transport.conversation import UnverifiedComment, fetch_comments_since
from claude_code_codex_review_loop.transport.gh import GhContext, RetryPolicy
from claude_code_codex_review_loop.workflow import (
    RESULT_VARIANTS,
    IssuedTransaction,
    block_spec_for,
    build_event,
    issue_transaction,
    transaction_section,
    user_spec_for,
    with_active_trees,
    with_machine_state,
)
from claude_code_codex_review_loop.workflow.ports import ActionContext

ISSUED_AT = "2026-08-25T09:00:00Z"
ACCEPTED_AT = "2026-08-25T09:05:00Z"
SPEAKER = "Claude Code"
USER_SPEAKER = "User"
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


def _port_mapped_event(
    evidence: RecordEvidence, record: VerifiedRecord, block: BlockContext | None
) -> Event:
    """`RecordEvidence`だけでは作れないeventを組み立てる（写像はportの責務）。

    `BLOCK_INTERVENTION`の解消evidenceは**解除対象blockへの参照**（`target_block_binding`）と
    record自身のbindingを分離して持つ（AC-C01-11）。対象bindingはcanonical projectionから
    取れるが、C-01の完全一致照合は**blockが持つ値**（head / reason / budget / counter
    snapshot / fingerprint）まで要求する。これらはC-10 / C-11が所有する値で**C-08は作らない**
    ため、portが供給する。fakeでは呼び出し側がblockを渡してその位置を示す。
    """
    if record.kind is not RecordKind.BLOCK_INTERVENTION:  # pragma: no cover - 他にport_mappedは無い
        raise AssertionError(f"port_mappedの写像が未定義: {record.kind.value}")
    target = record.projection.target
    assert target is not None, "BLOCK_INTERVENTIONのprojectionは解除対象を必ず含む"
    assert block is not None, "解消evidenceの組み立てにはblockの値が要る（C-10 / C-11が所有）"
    progress = block if isinstance(block, ProgressBlock) else None
    return BlockResolvedIntervention(
        resolution=BlockResolutionEvidence(
            target_block_binding=OpaqueBinding(target),
            head=block.head,
            record=evidence,
            reason=None if progress is None else progress.reason,
            budget=None if progress is None else progress.budget,
            counter_snapshot=None if progress is None else progress.counter_snapshot,
            fingerprint=None if progress is None else progress.fingerprint,
        )
    )


@dataclass
class FakeRecordEvents:
    """検証済みrecordからC-01 eventを作るport（本実装はC-10 / C-11）。

    host actionの結果はregistryの`build_event`をそのまま使う（1対1の対応がある）。
    `extra_event_inputs`（`ProgressReport` / `head`）はC-08が作らない値なのでここで供給する。

    **port_mappedなvariant**（`BLOCK_INTERVENTION`）は`build_event`では作れない。eventが
    受け取るのは`BlockResolutionEvidence`であって`RecordEvidence`ではないためで、写像を
    供給するのがまさにこのportの役目である（ADR-0017 決定2 / ADR-0023 決定7）。
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
    # port_mappedな写像に要るblock（C-10 / C-11が所有する値の供給元）
    block: BlockContext | None = None

    def event_for(self, evidence: RecordEvidence, record: VerifiedRecord) -> Event:
        override = self.events.get(record.kind)
        if override is not None:
            return override
        variant = RESULT_VARIANTS[record.kind]
        if variant.port_mapped:
            return _port_mapped_event(evidence, record, self.block)
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


# ---------------------------------------------------------------------------
# `AWAIT_USER`搬送路（Phase 8 PR-2c）
# ---------------------------------------------------------------------------

# awaitingごとの滞在state（C-01の`AWAITING_HOME`と一致させる）
USER_AWAITING_STATES: Mapping[Awaiting, State] = {
    Awaiting.USER_INPUT_DECISION: State.AWAITING_USER_DECISION,
    Awaiting.USER_INPUT_GATE: State.READY_FOR_HUMAN_MERGE,
    Awaiting.USER_INPUT_PERMISSION: State.AWAITING_TOOL_PERMISSION,
}

PERMISSION_SECTION: Mapping[str, object] = {
    "permission_id": "perm-1",
    "blocked_tool": "Bash(git push)",
    "requested_scope": "push once",
    "head_sha": HEAD,
}


def user_machine_state(awaiting: Awaiting) -> MachineState:
    """当該awaitingで待機しているMachineState（不変条件を満たす最小形）。"""
    state = USER_AWAITING_STATES[awaiting]
    # AWAITING_TOOL_PERMISSIONはreturn_toを必ず持つ（C-01のRETURN_SCOPE不変条件）
    return_to = State.RUNNING_REVIEW if state is State.AWAITING_TOOL_PERMISSION else None
    return MachineState(state=state, awaiting=awaiting, return_to=return_to)


def user_record_payload(
    kind: RecordKind,
    *,
    head: str = HEAD,
    route: str = HOST_TRANSCRIPT_ROUTE,
    target_block_binding: str | None = None,
) -> dict[str, object]:
    """user-input recordの代表payload（対象head・入力経路・解除対象blockだけ差し替える）。"""
    payload = dict(REPRESENTATIVE[SchemaKind(kind.value)])
    head_source = PROJECTION_SPECS[kind].head_source
    assert head_source is not None
    payload[head_source] = head
    payload["input_route"] = route
    if target_block_binding is not None:
        payload["target_block_binding"] = target_block_binding
    return payload


def permission_resume_payload(**overrides: object) -> dict[str, object]:
    payload = dict(REPRESENTATIVE[SchemaKind.PERMISSION_RESUME])
    payload["permission_id"] = PERMISSION_SECTION["permission_id"]
    payload["tool"] = PERMISSION_SECTION["blocked_tool"]
    payload["scope"] = PERMISSION_SECTION["requested_scope"]
    payload["current_head_sha"] = PERMISSION_SECTION["head_sha"]
    payload.update(overrides)
    return payload


def user_submit_payload(
    *,
    request_id: str,
    nonce: str,
    result_hash: str,
    awaiting: Awaiting | None = Awaiting.USER_INPUT_GATE,
    result_kind: str | None = "GATE_QUESTION",
    run_id: str = RUN,
    head: str = HEAD,
    block_binding: str | None = None,
) -> dict[str, object]:
    """user-input submitの代表envelope。

    待機の識別子は直和（ADR-0023）。`block_binding`を渡すとblock介入待ちのsubmitになり、
    `awaiting`は載らない（schemaのcross-field ruleが一方だけを要求する）。
    """
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "request_id": request_id,
        "expected_head_sha": head,
        "nonce": nonce,
        "result_hash": result_hash,
    }
    if block_binding is not None:
        payload["block_binding"] = block_binding
    elif awaiting is not None:
        payload["awaiting"] = awaiting.value
    if result_kind is not None:
        payload["result_kind"] = result_kind
    return payload


@dataclass(frozen=True)
class UserEnv:
    """`AWAIT_USER`の往復を通すための一式（GitHub側とcheckpointをseed済み）。"""

    paths: StatePaths
    run_dir: Path
    directory: Path
    context: GhContext
    repo: RepoRef
    policy: RetryPolicy
    # 待機の識別子は直和（ADR-0023）。blockの介入待ちはawaitingを持たない
    awaiting: Awaiting | None
    records: tuple[VerifiedRecord, ...]
    block: BlockContext | None = None

    def advance_kwargs(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "paths": self.paths,
            "run_id": RUN,
            "repository": REPOSITORY,
            "number": NUMBER,
            "head_sha": HEAD,
            "payload_port": FakePayloadPort(),
            "evidence_port": FakeEvidencePort(self.evidence()),
            "records_port": FakeRecordSource(records=self.records),
            "id_source": FakeIds("req"),
            "issued_at": ISSUED_AT,
        }
        values.update(overrides)
        return values

    def submit_kwargs(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "paths": self.paths,
            "run_id": RUN,
            "repository": REPOSITORY,
            "number": NUMBER,
            "records_port": FakeRecordSource(records=self.records),
            "body_port": FakeBodyPort(text="ユーザー入力"),
            "max_result_bytes": MAX_RESULT_BYTES,
            "retry_budget": RETRY_BUDGET,
            "accepted_at": ACCEPTED_AT,
            "speaker": SPEAKER,
            "model": MODEL,
            "user_speaker": USER_SPEAKER,
        }
        values.update(overrides)
        return values

    def persist_kwargs(self, **overrides: object) -> dict[str, object]:
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
            "event_port": FakeRecordEvents(block=self.block),
            "policy": self.policy,
            "search_since": None,
            "search_attempts": 1,
            "search_backoff_seconds": 0.0,
            "search_max_pages": 5,
        }
        values.update(overrides)
        return values

    def evidence(self) -> tuple[VerifiedRecord, ...]:
        """当該待機の根拠として許可されるrecordだけを渡す（registryが値域を決める）。"""
        if self.block is not None:
            block_spec = block_spec_for(self.block)
            # 介入を受け付けないblockには提示する根拠が無い（requestも出ない）
            kinds = () if block_spec is None else block_spec.evidence_kinds
        else:
            assert self.awaiting is not None
            spec = user_spec_for(self.awaiting)
            assert spec is not None
            kinds = spec.evidence_kinds
        return tuple(record for record in self.records if record.kind in kinds)

    def comments(self) -> list[object]:
        entries = read_state(self.directory)["comments"]
        assert isinstance(entries, list)
        return entries


def user_env(
    tmp_path: Path,
    *,
    awaiting: Awaiting | None = Awaiting.USER_INPUT_GATE,
    seeded: Sequence[RecordKind] = (RecordKind.FINAL_REPORT,),
    state: MachineState | None = None,
    extra: Mapping[str, object] | None = None,
    scenario: str = "ok",
    timeout_seconds: float = 30.0,
    block: BlockContext | None = None,
) -> UserEnv:
    """GitHubへseq=1..nを投稿済みにし、当該待機で止まっているcheckpointを用意する。

    `block`を渡すと`BLOCKED`のrunになる（`awaiting`はNoneになり、識別子はblock binding）。
    """
    directory = tmp_path / "gh"
    directory.mkdir(parents=True, exist_ok=True)
    posted = chain_comments_of(list(seeded))
    seed_state(directory, comments=[seed_dict(comment, issue=NUMBER) for comment in posted])
    paths = state_paths(tmp_path)
    if state is not None:
        machine_state = state
    elif block is not None:
        machine_state = blocked_machine_state(block)
    else:
        assert awaiting is not None
        machine_state = user_machine_state(awaiting)
    payload = with_machine_state(checkpoint_payload(), machine_state)
    if awaiting is Awaiting.USER_INPUT_PERMISSION:
        payload["permission"] = dict(PERMISSION_SECTION)
    if extra is not None:
        payload.update(extra)
    save_checkpoint(checkpoint_path(paths, RUN), payload)
    return UserEnv(
        paths=paths,
        run_dir=run_directory(paths, RUN),
        directory=directory,
        context=make_context(directory, scenario=scenario, timeout_seconds=timeout_seconds),
        repo=RepoRef(owner="owner", name="repo"),
        policy=make_policy(),
        awaiting=None if block is not None else awaiting,
        records=verified_chain(list(seeded)).records,
        block=block,
    )


# ---------------------------------------------------------------------------
# 停止手続き（Phase 8 PR-3a）
# ---------------------------------------------------------------------------

GRACE_SECONDS = 1.5
TREE_PID = 4242
TREE_PGID = 4242
JOB_NAME = "cc-review-tree-1"


def process_group_ref(pid: int = TREE_PID, pgid: int = TREE_PGID) -> ProcessGroupRef:
    return ProcessGroupRef(pid=pid, pgid=pgid)


def job_object_ref(pid: int = TREE_PID, job_name: str = JOB_NAME) -> JobObjectRef:
    return JobObjectRef(pid=pid, job_name=job_name)


@dataclass
class FakeStopPort:
    """process tree停止のfake（実processを起動しない。実停止はC-03のtestが担保する）。

    `fails`に入れたrefは`StopError`を投げる。`calls`で冪等性（同じrefへの再要求）を観測する。
    """

    method: StopMethod = StopMethod.GRACEFUL
    fails: frozenset[TreeRef] = field(default_factory=frozenset)
    calls: list[tuple[TreeRef, float]] = field(default_factory=list)

    def stop(self, ref: TreeRef, grace_seconds: float) -> StopResult:
        self.calls.append((ref, grace_seconds))
        if ref in self.fails:
            raise StopError("stop", f"treeを停止できない: {ref}")
        return StopResult(method=self.method, graceful_requested=True)


def halt_env(
    tmp_path: Path,
    *,
    state: MachineState,
    trees: Sequence[TreeRef] = (),
    extra: Mapping[str, object] | None = None,
) -> EngineEnv:
    """停止手続き中のcheckpointを用意する（treeの台帳も置く）。"""
    paths = state_paths(tmp_path)
    payload = with_machine_state(checkpoint_payload(), state)
    payload = with_active_trees(payload, list(trees))
    if extra is not None:
        payload.update(extra)
    save_checkpoint(checkpoint_path(paths, RUN), payload)
    return EngineEnv(
        paths=paths, run_dir=run_directory(paths, RUN), checkpoint=checkpoint_path(paths, RUN)
    )


def halt_kwargs(env: EngineEnv, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "paths": env.paths,
        "run_id": RUN,
        "repository": REPOSITORY,
        "number": NUMBER,
        "stop_port": FakeStopPort(),
        "grace_seconds": GRACE_SECONDS,
    }
    values.update(overrides)
    return values


def cancelling(state: State = State.APPLYING_FIXES, binding: str = "cr:run-1:1:cancel") -> MachineState:
    """cancel停止手続き中のMachineState。"""
    return MachineState(
        state=state, procedure=CancellingProcedure(attempt_binding=OpaqueBinding(binding))
    )


def halting_for_block(
    state: State = State.APPLYING_FIXES, binding: str = "iv:gap:run-1:2"
) -> MachineState:
    """integrity halt gate中のMachineState。"""
    violation = IntegrityEvidenceRef(
        binding=OpaqueBinding(binding), descriptor=OpaqueRef("gap"), head=OpaqueRef(HEAD)
    )
    return MachineState(
        state=state,
        procedure=HaltingForBlockProcedure(
            block=RecordIntegrityBlock(violations=(violation,)),
            attempt_binding=OpaqueBinding(binding),
        ),
    )


def progress_block(continuation: str = "FIX_RESULT") -> ProgressBlock:
    return ProgressBlock(
        binding=OpaqueBinding("cr:run-1:1:progress"),
        head=OpaqueRef(HEAD),
        continuation=BLOCKED_CONTINUATIONS[continuation],
        reason=Progress.NO_PROGRESS,
        budget=Budget.REVIEW_ROUND,
        counter_snapshot=OpaqueSnapshot("snap-1"),
        fingerprint=OpaqueFingerprint("fp-1"),
    )


def limit_block() -> ProgressBlock:
    """限度到達の膠着block。

    出口は**limit引き上げ**（B-LR）であって介入ではない。C-01はこのblockで
    `BLOCK_INTERVENTION`を受理しないため（P-22は`NO_PROGRESS`限定）、C-08は介入requestを
    出さず`Blocked`のまま返す。
    """
    return ProgressBlock(
        binding=OpaqueBinding("cr:run-1:1:limit"),
        head=OpaqueRef(HEAD),
        continuation=BLOCKED_CONTINUATIONS["FIX_RESULT"],
        reason=Progress.LIMIT_REACHED,
        budget=Budget.REVIEW_ROUND,
        counter_snapshot=OpaqueSnapshot("snap-1"),
        fingerprint=OpaqueFingerprint("fp-1"),
    )


def external_block() -> ExternalDependencyBlock:
    return ExternalDependencyBlock(
        binding=OpaqueBinding("cr:run-1:1:external"),
        head=OpaqueRef(HEAD),
        continuation=BLOCKED_CONTINUATIONS["EXTERNAL_DEPENDENCY"],
        evidence=RecordEvidence(
            kind=RecordKind.EXTERNAL_DEPENDENCY,
            binding=OpaqueBinding("cr:run-1:1:external"),
            ref=OpaqueRef("c-1"),
        ),
    )


def blocked_machine_state(block: BlockContext) -> MachineState:
    """当該blockで止まっている`BLOCKED`のMachineState（介入requestの起点）。"""
    return MachineState(state=State.BLOCKED, block=block)


def integrity_block(binding: str = "iv:gap:run-1:2") -> RecordIntegrityBlock:
    return RecordIntegrityBlock(
        violations=(
            IntegrityEvidenceRef(
                binding=OpaqueBinding(binding), descriptor=OpaqueRef("gap"), head=OpaqueRef(HEAD)
            ),
        )
    )

__all__ = [
    "ACCEPTED_AT",
    "EngineEnv",
    "FakeBodyPort",
    "FakeEvidencePort",
    "FakeIds",
    "FakePayloadPort",
    "FakeRecordEvents",
    "FakeRecordSource",
    "FakeStopPort",
    "GRACE_SECONDS",
    "GithubBackedRecords",
    "HEAD",
    "ISSUED_AT",
    "JOB_NAME",
    "MAX_RESULT_BYTES",
    "MODEL",
    "NEW_HEAD",
    "NUMBER",
    "PERMISSION_SECTION",
    "PersistEnv",
    "REPOSITORY",
    "RETRY_BUDGET",
    "RUN",
    "SPEAKER",
    "TREE_PGID",
    "TREE_PID",
    "USER_AWAITING_STATES",
    "USER_SPEAKER",
    "UserEnv",
    "blocked_machine_state",
    "cancelling",
    "external_block",
    "failure_payload",
    "fix_result_payload",
    "gate_answer_payload",
    "halt_env",
    "halt_kwargs",
    "halting_for_block",
    "integrity_block",
    "limit_block",
    "job_object_ref",
    "machine_state",
    "permission_resume_payload",
    "persist_env",
    "process_group_ref",
    "progress_block",
    "raw",
    "review_records",
    "seed",
    "submit_payload",
    "user_env",
    "user_machine_state",
    "user_record_payload",
    "user_submit_payload",
    "write_result",
]
