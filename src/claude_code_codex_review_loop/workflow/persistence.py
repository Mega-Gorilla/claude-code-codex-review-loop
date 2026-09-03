# SPDX-License-Identifier: Apache-2.0
"""`PersistRecord`の実行（Phase 8。ADR-0010 / ADR-0017）。

C-01が`pending_record`へ置いた**任意のrecord**をGitHubへ永続化し、検証してから
`*Verified` eventでstateを進める。host actionの結果専用ではない: C-09以降が作る
`REVIEW_RESULT` / `FINAL_REPORT` / decision系も同じ経路を通る**汎用の境界**である。

順序（変えるとcrash windowで重複recordを作る）:

```
pending_record -> transaction読込 -> chain gate -> evaluate_pending（C-07）
  -> ensure_comment_posted（search-first -> post -> read-after-write）
  -> chain再検証で当該bindingのrecordを引く -> event組み立て -> transition
  -> transactionを消してcheckpointを保存
```

「投稿済みか」の判定はC-07の`evaluate_pending`へ委ね、ここで独自判定しない。投稿は
C-05の`ensure_comment_posted`が入口でsearch-firstを行うため、重複防止は二重に効く。

設定値（retry policy、検索窓、page上限）は**すべて引数**で受け取る。既定値の解決はC-12。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..domain import events as ev
from ..domain.commands import Command
from ..domain.machine import transition
from ..domain.values import (
    IntegrityEvidenceRef,
    MachineState,
    OpaqueBinding,
    OpaqueRef,
    RecordEvidence,
    RecordKind,
    TransitionRejected,
)
from ..identity.record_chain import ChainVerification, VerifiedRecord
from ..state import (
    PendingAlreadyPosted,
    PendingReissueRequired,
    PendingTransaction,
    PendingUnavailable,
    StatePaths,
    checkpoint_path,
    evaluate_pending,
    read_transaction,
    save_checkpoint,
)
from ..transport.conversation import ensure_comment_posted
from ..transport.gh import GhContext, RepoRef, RetryPolicy, TransportError
from .actions import ActionRegistryError
from .checkpoint_view import (
    SectionUnavailable,
    read_recorded_violations,
    with_recorded_violations,
    with_verified_machine_state,
)
from .ports import RecordEventPort, RecordSourcePort
from .run_context import EngineStopped, RunContext, load_run


@dataclass(frozen=True)
class RecordPersisted:
    """recordを永続化し、検証してstateを進めた。"""

    record: VerifiedRecord
    machine_state: MachineState
    commands: tuple[Command, ...]
    posted: bool


@dataclass(frozen=True)
class IntegrityDetected:
    """chainのviolationをC-01へ入力した（推測して再投稿しない）。"""

    violations: tuple[IntegrityEvidenceRef, ...]
    machine_state: MachineState
    commands: tuple[Command, ...]


@dataclass(frozen=True)
class PersistFailed:
    """bounded retryが尽きたため、C-01へ`RunFailed`を入力した。"""

    detail: str
    machine_state: MachineState
    commands: tuple[Command, ...]


PersistOutcome = RecordPersisted | IntegrityDetected | PersistFailed | EngineStopped


def _apply(
    run: RunContext, event: ev.Event, *, paths: StatePaths, run_id: str
) -> tuple[MachineState, tuple[Command, ...]] | EngineStopped:
    """C-01へeventを入力し、結果のstateをcheckpointへ保存する。

    **読み戻せない状態は保存しない**。C-01が返す状態にはcheckpointがまだ表現しない
    付随値を持つものがあり、黙って落とすと次のresumeが復元できないcheckpointになる。
    その場合は保存せず停止する（検出自体はchain検証が冪等に再現するため失われない）。
    """
    try:
        machine_state, commands = transition(run.machine_state, event)
    except (TransitionRejected, ev.IllegalEventError) as error:
        return EngineStopped("illegal_event", f"C-01が{type(event).__name__}を受理しない: {error}")
    payload = with_verified_machine_state(run.payload, machine_state)
    if isinstance(payload, SectionUnavailable):  # pragma: no cover - 保存できない状態は読めず、
        # この入口（読み込めたcheckpoint + integrity検出 / RunFailed）からは到達しない。
        # 表現範囲を広げるPhaseがここを踏むまで、防御として残す
        return EngineStopped("state_not_persistable", payload.detail)
    save_checkpoint(checkpoint_path(paths, run_id), payload)
    return machine_state, commands


def _detect_violations(
    run: RunContext,
    violations: tuple[IntegrityEvidenceRef, ...],
    *,
    paths: StatePaths,
    run_id: str,
) -> IntegrityDetected | EngineStopped:
    """violationを1件ずつC-01へ入力する（集合への追加はC-01がunionする）。"""
    current = run
    machine_state = run.machine_state
    collected: list[Command] = []
    for violation in violations:
        applied = _apply(
            current,
            ev.RecordIntegrityViolationDetected(evidence=violation),
            paths=paths,
            run_id=run_id,
        )
        if isinstance(applied, EngineStopped):
            return applied
        machine_state, commands = applied
        # **順に蓄積する**。最初の検出だけがhalt gateへ入り`HaltRun`を発行するため、
        # 上書きすると停止命令が呼び出し側へ届かない
        collected.extend(commands)
        current = RunContext(
            payload=current.payload, machine_state=machine_state, run_dir=current.run_dir
        )
    return IntegrityDetected(
        violations=violations, machine_state=machine_state, commands=tuple(collected)
    )


def _unknown_violations(
    run: RunContext,
    violations: tuple[IntegrityEvidenceRef, ...],
    *,
    recorded: Sequence[str],
) -> tuple[IntegrityEvidenceRef, ...]:
    """まだC-01が受理していないviolationだけを返す（ADR-0024 決定1）。

    incident recordは**まさにchainが壊れているときに投稿するrecord**である。violationは
    `verify_record_chain`がliveのGitHubから毎回再導出するため、壊れたchainは壊れたままで
    あり、`is_intact`だけでgateするとincident recordは永久に投稿できずrunがterminalへ
    到達しない。

    そこで許すのは次の2条件が揃う場合**だけ**である。

    1. 永続化を待っているのが`INTEGRITY_INCIDENT` recordであること
    2. chainのviolationが**すべてC-01の既知**であること。既知とは`deferred_integrity`
       （未記録）と`recorded`（記録済みの台帳）の和である

    **記録済みを差し引くのが要点**である。C-01は記録済みviolationを`deferred_integrity`から
    外すが、chainからは消えない。差し引かないと記録済みを「新しい検出」として再入力し、
    部分記録（I-VR）のrunが記録と再検出を往復して終わらない。

    「runが`RecordingIncidentProcedure`にいること」を別途検査しないのは、C-01の
    `INCIDENT_PENDING_SCOPE`不変条件が「`INTEGRITY_INCIDENT`のpendingはincident記録中に
    限る」を`MachineState`の構築時点で強制しており、条件1がそれを含意するためである
    （その依存はtestで固定する）。

    新しいviolationは従来どおり検出が優先される。ここを緩めると、記録すべきviolationを
    取りこぼしたままterminalへ進み得る（行き止まりより悪い）。
    """
    pending = run.machine_state.pending_record
    if pending is None or pending.kind is not RecordKind.INTEGRITY_INCIDENT:
        return violations
    known = {ref.binding.value for ref in run.machine_state.deferred_integrity} | set(recorded)
    return tuple(violation for violation in violations if violation.binding.value not in known)


def _integrity_gate(
    run: RunContext,
    chain: ChainVerification,
    *,
    paths: StatePaths,
    run_id: str,
) -> IntegrityDetected | EngineStopped | None:
    """未知のviolationがあればC-01へ入力する（無ければNoneで先へ進む）。

    投稿の前後で同じgateを通す。片方だけだと投稿はできるが検証で止まる。台帳を読めない
    ときは既知集合が痩せて記録と再検出を往復するため、**推測せず停止する**。
    """
    recorded = read_recorded_violations(run.payload)
    if isinstance(recorded, SectionUnavailable):  # pragma: no cover - CHECKPOINT schemaが
        # `incident_record.recorded_bindings`をopaque文字列のarrayへ限定しており、読み込めた
        # checkpointの台帳は壊れていない。readerが直和を返すのはschemaを通っていないpayloadを
        # 受け取る呼び出しがあるためで、`state_not_persistable`と同じ防御として残す
        return EngineStopped("incident_ledger_unavailable", recorded.detail)
    unknown = _unknown_violations(run, chain.violations, recorded=recorded)
    if not unknown:
        return None
    return _detect_violations(run, unknown, paths=paths, run_id=run_id)


def _post(
    directive: PendingReissueRequired,
    *,
    context: GhContext,
    repo: RepoRef,
    number: int,
    policy: RetryPolicy,
    search_since: str | None,
    search_attempts: int,
    search_backoff_seconds: float,
    search_max_pages: int,
) -> str | None:
    """完成形の本文をそのまま投稿する（失敗理由があれば返す）。

    戻り値（`PostVerified` / `PostHashMismatch`）でここは分岐しない。read-after-writeで
    本文hashが違う場合は改変の疑いであり、**推測せずC-06のchain検証へ委ねる**（検証済み
    chainに現れないか、本文がtransactionと一致しない形で必ず捕まる）。
    """
    try:
        ensure_comment_posted(
            context,
            repo,
            number,
            directive.body,
            search_since=search_since,
            search_attempts=search_attempts,
            search_backoff_seconds=search_backoff_seconds,
            search_max_pages=search_max_pages,
            policy=policy,
        )
    except TransportError as error:
        return f"投稿がbounded retryで完了しない: {error}"
    return None


def _with_recorded(
    payload: Mapping[str, object], event: ev.Event
) -> dict[str, object] | SectionUnavailable:
    """検証済みincident recordが記録したviolationを台帳へunionする（ADR-0024 決定5）。

    値はC-06が構成・検証した`recorded_bindings`そのもので、C-08は解釈せず台帳へ写す。
    """
    if not isinstance(event, ev.IntegrityIncidentVerified):
        return dict(payload)
    return with_recorded_violations(payload, [binding.value for binding in event.recorded_bindings])


def _verify_and_advance(
    run: RunContext,
    transaction: PendingTransaction,
    *,
    records_port: RecordSourcePort,
    event_port: RecordEventPort,
    paths: StatePaths,
    run_id: str,
    posted: bool,
) -> PersistOutcome:
    """投稿後のchainを検証し、当該recordのeventでstateを進める。"""
    chain = records_port.chain(run_id)
    detected = _integrity_gate(run, chain, paths=paths, run_id=run_id)
    if detected is not None:
        return detected
    record = next((item for item in chain.records if item.key == transaction.binding), None)
    if record is None:
        return EngineStopped(
            "record_unverified", f"投稿したrecordが検証済みchainに現れない: {transaction.binding}"
        )
    if record.body_hash != transaction.body_hash:
        return EngineStopped("record_body_mismatch", "検証済みrecordの本文がtransactionと一致しない")
    evidence = RecordEvidence(
        kind=record.kind,
        binding=OpaqueBinding(transaction.binding),
        ref=OpaqueRef(record.comment_id),
    )
    try:
        event = event_port.event_for(evidence, record)
    except (ActionRegistryError, ev.IllegalEventError) as error:
        return EngineStopped("event_unavailable", f"eventを組み立てられない: {error}")
    try:
        machine_state, commands = transition(run.machine_state, event)
    except (TransitionRejected, ev.IllegalEventError) as error:
        return EngineStopped("illegal_event", f"C-01が{type(event).__name__}を受理しない: {error}")
    # **検証済みincident recordが記録したviolationを台帳へ**（決定5）。次のcycleが
    # 記録済みを「新しい検出」として再入力しないための唯一の記憶である
    ledger = _with_recorded(run.payload, event)
    if isinstance(ledger, SectionUnavailable):  # pragma: no cover - CHECKPOINT schemaが
        # `incident_record.recorded_bindings`をopaque文字列のarrayへ限定しており、読み込めた
        # checkpointの台帳は壊れていない。readerが直和を返すのはschemaを通っていないpayloadを
        # 受け取る呼び出しがあるためで、ここは`state_not_persistable`と同じ防御として残す
        return EngineStopped("incident_ledger_unavailable", ledger.detail)
    payload = with_verified_machine_state(ledger, machine_state)
    if isinstance(payload, SectionUnavailable):  # pragma: no cover - PR-3aで到達可能な
        # 全非terminal stateが表現できるようになった（round-trip testが固定）。表現範囲を
        # 広げるPhaseがここを踏むまで、`_apply`と同じ防御として残す
        return EngineStopped("state_not_persistable", payload.detail)
    payload.pop("transaction", None)
    save_checkpoint(checkpoint_path(paths, run_id), payload)
    return RecordPersisted(
        record=record, machine_state=machine_state, commands=commands, posted=posted
    )


def persist(
    *,
    paths: StatePaths,
    run_id: str,
    repository: str,
    number: int,
    context: GhContext,
    repo: RepoRef,
    records_port: RecordSourcePort,
    event_port: RecordEventPort,
    policy: RetryPolicy,
    search_since: str | None,
    search_attempts: int,
    search_backoff_seconds: float,
    search_max_pages: int,
) -> PersistOutcome:
    """`pending_record`を1件だけ永続化して検証する（1回のpersistで1 record）。"""
    run = load_run(paths, run_id=run_id, repository=repository, number=number)
    if isinstance(run, EngineStopped):
        return run
    pending = run.machine_state.pending_record
    if pending is None:
        return EngineStopped("no_pending_record", "永続化を待つrecordが無い")
    transaction = read_transaction(run.payload)
    if isinstance(transaction, PendingUnavailable):
        return EngineStopped("transaction_unavailable", transaction.detail)
    if transaction is None:
        return EngineStopped("transaction_missing", "pending recordに対応するtransactionが無い")
    if transaction.binding != pending.binding.value or transaction.kind is not pending.kind:
        return EngineStopped("transaction_mismatch", "transactionがpending recordと一致しない")
    if transaction.body_hash is None:
        # 完成本文hashが無いと、投稿したrecordが意図した本文かを検証できない。schema上
        # optionalなのは既存fieldの制約を強化しないためで（ADR-0013 決定9）、producerが
        # 省略してよい意味ではない（ADR-0014 決定21）。**投稿する前に**停止する
        return EngineStopped(
            "body_hash_missing", "transactionに完成本文hashが無い（投稿前にfail closedする）"
        )

    chain = records_port.chain(run_id)
    detected = _integrity_gate(run, chain, paths=paths, run_id=run_id)
    if detected is not None:
        return detected

    outcome = evaluate_pending(transaction, run_id=run_id, records=chain.records)
    if isinstance(outcome, PendingUnavailable):
        return EngineStopped("pending_unavailable", outcome.detail)
    posted = False
    if isinstance(outcome, PendingReissueRequired):
        failure = _post(
            outcome,
            context=context,
            repo=repo,
            number=number,
            policy=policy,
            search_since=search_since,
            search_attempts=search_attempts,
            search_backoff_seconds=search_backoff_seconds,
            search_max_pages=search_max_pages,
        )
        if failure is not None:
            applied = _apply(run, ev.RunFailed(), paths=paths, run_id=run_id)
            if isinstance(applied, EngineStopped):
                return applied
            return PersistFailed(detail=failure, machine_state=applied[0], commands=applied[1])
        posted = True
    elif not isinstance(outcome, PendingAlreadyPosted):  # pragma: no cover - 直和は3値で網羅
        return EngineStopped("pending_unexpected", f"想定外のpending: {type(outcome).__name__}")

    return _verify_and_advance(
        run,
        transaction,
        records_port=records_port,
        event_port=event_port,
        paths=paths,
        run_id=run_id,
        posted=posted,
    )
