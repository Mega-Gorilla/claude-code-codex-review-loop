# SPDX-License-Identifier: Apache-2.0
"""step engine: `advance`と`submit`（Phase 8。implementation plan Section 2 / ADR-0015）。

Controller CLIはClaude Code sessionの子processであり、親のLLM turnを呼び戻せない。engineは
Claudeを起動せず、`advance`で次の`HOST_ACTION`を返し、active hostが自分のcontextで実行して
`submit`で結果を返す。**同一runに対する制御経路はこの2つだけ**である（AC-C08-03）。

「pure」はprocess / CLIに依存しないという意味で、I/Oが無いという意味ではない。値の供給元は
port（`ports`）、path・ID・時刻・上限値は引数で受け取り、engine自身は既定値を持たない。

順序（ADR-0015。crash windowで重複を作らないための不変条件）:

- `advance`: action envelopeの保存 -> checkpointの保存 -> hostへ返却
- `submit`: 受理 -> **submit receiptとrecord transactionを同じcheckpoint更新で保存** ->
  （投稿以降はPR-2bのPersistRecord実行）

本PRは`RecordProduced`までを扱う。GitHubへの投稿・検証と`*Verified` eventの組み立ては
C-01の`PersistRecord`実行として後続PRが行うため、`advance`はpending recordがある間
`PersistRequired`を返す。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..domain import events as ev
from ..domain.commands import Command
from ..domain.machine import transition
from ..domain.states import TERMINAL_STATES, State
from ..domain.values import Awaiting, MachineState, OpaqueBinding, PendingRecord, RecordKind, TransitionRejected
from ..errors import ErrorCategory
from ..identity.errors import IdentityError
from ..identity.fs_permissions import create_private_dir, verify_private_dir, write_private_text
from ..policy.redaction import redact
from ..schema.action import HOST_ACTION, HOST_FAILURE, SUBMIT
from ..schema.migrate import load_with_migration
from ..schema.projection import PROJECTION_SPECS, canonical_json, canonical_payload_hash
from ..schema.registry import validate_object
from ..state import (
    CheckpointLoaded,
    StatePaths,
    checkpoint_path,
    load_checkpoint,
    run_directory,
    save_checkpoint,
)
from ..transport.render import prepare_public_body
from .actions import ActionSpec, spec_for_awaiting, spec_for_kind
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
from .ports import ActionContext, ActionPayloadPort, EvidencePort, RecordBodyPort, RecordSourcePort
from .results import ResultRejected, read_result
from .transaction import IssuedTransaction, TransactionUnavailable, issue_transaction, transaction_section

ACTIONS_DIR = "actions"
ENVELOPE_FILE = "action.json"
RESULT_FILE = "result.json"
HOST_ACTION_VERSION = 2
SUBMIT_VERSION = 2

_USER_INPUT_AWAITINGS = frozenset(
    {Awaiting.USER_INPUT_DECISION, Awaiting.USER_INPUT_GATE, Awaiting.USER_INPUT_PERMISSION}
)


@dataclass(frozen=True)
class EngineStopped:
    """進められない（推測して進めない）。codeは診断とtestの安定した識別子。"""

    code: str
    detail: str


@dataclass(frozen=True)
class HostActionIssued:
    """active hostが実行する`HOST_ACTION`（新規発行または未完了actionの再提示）。"""

    action: PendingAction
    envelope: dict[str, object]
    envelope_path: Path
    result_path: Path
    reissued: bool


@dataclass(frozen=True)
class AwaitUser:
    """ユーザー入力待ち。搬送路（envelopeとuser-input submit）は後続PRが実装する。"""

    awaiting: Awaiting


@dataclass(frozen=True)
class PersistRequired:
    """受理済み結果の永続化待ち（C-01の`PersistRecord`。実行は後続PR）。"""

    record: PendingRecord


@dataclass(frozen=True)
class Terminal:
    """terminal state（新しいactionを発行しない）。"""

    state: State


AdvanceOutcome = HostActionIssued | AwaitUser | PersistRequired | Terminal | EngineStopped


@dataclass(frozen=True)
class SubmitAccepted:
    """submitを受理した（nonceは消費済み）。"""

    receipt: SubmitReceipt
    machine_state: MachineState
    commands: tuple[Command, ...]
    transaction: IssuedTransaction | None = None
    retry_available: bool = False
    failure_summary: str | None = None


@dataclass(frozen=True)
class SubmitReplayed:
    """同一内容の再送（冪等。状態を進めず、以前と同じ結果を返す）。"""

    receipt: SubmitReceipt


SubmitOutcome = SubmitAccepted | SubmitReplayed | EngineStopped


@dataclass(frozen=True)
class _Loaded:
    """checkpointと、そこから復元した状態。"""

    payload: dict[str, object]
    machine_state: MachineState
    run_dir: Path


def _ensure_private_dir(path: Path) -> None:
    if path.exists():
        verify_private_dir(path)
        return
    create_private_dir(path)


def _load(
    paths: StatePaths, *, run_id: str, repository: str, number: int
) -> _Loaded | EngineStopped:
    """checkpointを読み、runの同一性とMachineStateの復元まで済ませる。"""
    result = load_checkpoint(checkpoint_path(paths, run_id))
    if not isinstance(result, CheckpointLoaded):
        return EngineStopped("checkpoint_unavailable", f"checkpointを読めない: {type(result).__name__}")
    payload = result.payload
    if payload.get("run_id") != run_id:
        return EngineStopped("run_mismatch", "checkpointのrun IDが一致しない")
    if payload.get("repository") != repository or payload.get("number") != number:
        return EngineStopped("target_mismatch", "checkpointのrepository / 番号が一致しない")
    machine_state = read_machine_state(payload)
    if isinstance(machine_state, SectionUnavailable):
        return EngineStopped("state_unavailable", machine_state.detail)
    return _Loaded(payload=payload, machine_state=machine_state, run_dir=run_directory(paths, run_id))


def _action_paths(run_dir: Path, action_id: str) -> tuple[str, str]:
    """envelopeとresultのrun directory相対path（区切りは`/`で固定する）。"""
    return (
        f"{ACTIONS_DIR}/{action_id}/{ENVELOPE_FILE}",
        f"{ACTIONS_DIR}/{action_id}/{RESULT_FILE}",
    )


def _build_envelope(
    spec: ActionSpec,
    *,
    run_id: str,
    repository: str,
    number: int,
    head_sha: str,
    action_id: str,
    nonce: str,
    result_path: str,
    payload: Mapping[str, object],
    evidence: Sequence[tuple[str, str]],
) -> dict[str, object]:
    body = dict(payload)
    return {
        "schema_version": HOST_ACTION_VERSION,
        "run_id": run_id,
        "action_id": action_id,
        "action_kind": spec.kind,
        "repository": repository,
        "number": number,
        "expected_head_sha": head_sha,
        "payload_hash": canonical_payload_hash(body),
        "nonce": nonce,
        "result_path": result_path,
        "verified_records": [
            {"comment_id": comment_id, "head_sha": record_head} for comment_id, record_head in evidence
        ],
        "payload": body,
    }


def _evidence_of(
    spec: ActionSpec, port: EvidencePort, context: ActionContext
) -> tuple[tuple[str, str], ...] | EngineStopped:
    """同梱する検証済みrecord（AC-C08-07）。許可kindとseq昇順を検査する。"""
    records = tuple(port.evidence_for(context))
    for record in records:
        if record.kind not in spec.evidence_kinds:
            return EngineStopped(
                "evidence_kind", f"{spec.kind}の根拠に使えないrecord種別: {record.kind.value}"
            )
    if [record.seq for record in records] != sorted(record.seq for record in records):
        return EngineStopped("evidence_order", "根拠recordがseq昇順でない")
    return tuple((record.comment_id, record.head_sha) for record in records)


def _issue_action(
    loaded: _Loaded,
    spec: ActionSpec,
    *,
    paths: StatePaths,
    run_id: str,
    repository: str,
    number: int,
    head_sha: str,
    payload_port: ActionPayloadPort,
    evidence_port: EvidencePort,
    id_source: Callable[[], str],
    issued_at: str,
    previous: PendingAction | None,
) -> HostActionIssued | EngineStopped:
    """新しいactionを発行する（`previous`があれば同じlogical actionの次のattempt）。"""
    context = ActionContext(
        action=spec.action, run_id=run_id, repository=repository, number=number, head_sha=head_sha
    )
    evidence = _evidence_of(spec, evidence_port, context)
    if isinstance(evidence, EngineStopped):
        return evidence
    action_id = id_source()
    nonce = id_source()
    envelope_rel, result_rel = _action_paths(loaded.run_dir, action_id)
    envelope = _build_envelope(
        spec,
        run_id=run_id,
        repository=repository,
        number=number,
        head_sha=head_sha,
        action_id=action_id,
        nonce=nonce,
        result_path=result_rel,
        payload=payload_port.payload_for(context),
        evidence=evidence,
    )
    validation = validate_object(HOST_ACTION, dict(envelope))
    if not validation.ok:
        codes = ",".join(sorted(error.code for error in validation.errors))
        return EngineStopped("envelope_invalid", f"HOST_ACTIONが検証を通らない（{codes}）")
    envelope_hash = canonical_payload_hash(envelope)
    action = (
        next_attempt(
            previous,
            action_id=action_id,
            nonce=nonce,
            result_path=result_rel,
            envelope_path=envelope_rel,
            envelope_hash=envelope_hash,
            issued_at=issued_at,
        )
        if previous is not None
        else PendingAction(
            action_id=action_id,
            action_kind=spec.kind,
            nonce=nonce,
            expected_head_sha=head_sha,
            result_path=result_rel,
            envelope_path=envelope_rel,
            envelope_hash=envelope_hash,
            correlation_id=id_source(),
            attempt=1,
            issued_at=issued_at,
        )
    )
    try:
        _ensure_private_dir(loaded.run_dir / ACTIONS_DIR)
        _ensure_private_dir(loaded.run_dir / ACTIONS_DIR / action_id)
        write_private_text(loaded.run_dir / envelope_rel, canonical_json(envelope))
    except IdentityError as error:
        return EngineStopped("envelope_write", f"action envelopeを保存できない: {error}")
    save_checkpoint(checkpoint_path(paths, run_id), with_pending_action(loaded.payload, action))
    return HostActionIssued(
        action=action,
        envelope=envelope,
        envelope_path=loaded.run_dir / envelope_rel,
        result_path=loaded.run_dir / result_rel,
        reissued=False,
    )


def _reissue(loaded: _Loaded, action: PendingAction) -> HostActionIssued | EngineStopped:
    """未完了actionを**そのまま**再提示する（新しいactionを生成しない。ADR-0014 決定22）。"""
    path = loaded.run_dir / action.envelope_path
    if not path.is_file():
        return EngineStopped("envelope_missing", f"action envelopeが無い: {action.envelope_path}")
    envelope = load_with_migration(HOST_ACTION, path.read_bytes())
    if not envelope.ok or envelope.payload is None:
        codes = ",".join(sorted(error.code for error in envelope.errors))
        return EngineStopped("envelope_invalid", f"保存済みenvelopeが検証を通らない（{codes}）")
    if canonical_payload_hash(envelope.payload) != action.envelope_hash:
        return EngineStopped("envelope_mismatch", "保存済みenvelopeのhashがcheckpointと一致しない")
    return HostActionIssued(
        action=action,
        envelope=envelope.payload,
        envelope_path=path,
        result_path=loaded.run_dir / action.result_path,
        reissued=True,
    )


def advance(
    *,
    paths: StatePaths,
    run_id: str,
    repository: str,
    number: int,
    head_sha: str,
    payload_port: ActionPayloadPort,
    evidence_port: EvidencePort,
    id_source: Callable[[], str],
    issued_at: str,
) -> AdvanceOutcome:
    """次に実行すべきことを1つだけ返す（1回のadvanceで1 action）。

    未完了actionがある間は**新しいactionを発行しない**。既にsubmitを受理した
    （receiptがある）未完了actionは、retryできる失敗として次のattemptを発行する。
    """
    loaded = _load(paths, run_id=run_id, repository=repository, number=number)
    if isinstance(loaded, EngineStopped):
        return loaded
    machine_state = loaded.machine_state
    if machine_state.state in TERMINAL_STATES:
        return Terminal(state=machine_state.state)
    if machine_state.pending_record is not None:
        return PersistRequired(record=machine_state.pending_record)
    receipts = read_receipts(loaded.payload)
    if isinstance(receipts, SectionUnavailable):  # pragma: no cover - schema検証がreceiptの形を保証する
        return EngineStopped("host_action_unavailable", receipts.detail)
    pending = read_pending_action(loaded.payload)
    if isinstance(pending, SectionUnavailable):
        return EngineStopped("host_action_unavailable", pending.detail)
    if pending is not None:
        spec = spec_for_kind(pending.action_kind)
        if spec is None:  # pragma: no cover - action_kindはschemaのenumで限定されている
            return EngineStopped("unknown_action_kind", f"未知のaction種別: {pending.action_kind}")
        if find_receipt(receipts, action_id=pending.action_id, nonce=pending.nonce) is None:
            return _reissue(loaded, pending)
        return _issue_action(
            loaded,
            spec,
            paths=paths,
            run_id=run_id,
            repository=repository,
            number=number,
            head_sha=pending.expected_head_sha,
            payload_port=payload_port,
            evidence_port=evidence_port,
            id_source=id_source,
            issued_at=issued_at,
            previous=pending,
        )
    awaiting = machine_state.awaiting
    if awaiting is None:
        return EngineStopped("no_awaiting", f"{machine_state.state.value}は応答待ちの状態でない")
    if awaiting in _USER_INPUT_AWAITINGS:
        return AwaitUser(awaiting=awaiting)
    spec = spec_for_awaiting(awaiting)
    if spec is None:
        return EngineStopped("not_host_action", f"{awaiting.value}はhost actionではない")
    return _issue_action(
        loaded,
        spec,
        paths=paths,
        run_id=run_id,
        repository=repository,
        number=number,
        head_sha=head_sha,
        payload_port=payload_port,
        evidence_port=evidence_port,
        id_source=id_source,
        issued_at=issued_at,
        previous=None,
    )


def _binding_mismatch(envelope: Mapping[str, object], action: PendingAction) -> EngineStopped | None:
    """submitのbinding echoを未完了actionと突き合わせる（AC-C08-05）。"""
    if envelope.get("action_id") != action.action_id or envelope.get("nonce") != action.nonce:
        return EngineStopped("stale_action", "submitが未完了actionを指していない")
    if envelope.get("action_kind") != action.action_kind:
        return EngineStopped("kind_mismatch", "submitのaction種別が一致しない")
    if envelope.get("expected_head_sha") != action.expected_head_sha:
        return EngineStopped("head_mismatch", "submitの対象headが一致しない")
    return None


_ACTION_BOUND_HEAD_SOURCE = "target_head_sha"


def _record_head(
    kind: RecordKind, payload: Mapping[str, object], action: PendingAction
) -> str | EngineStopped:
    """recordのmarkerに載る対象head（payloadのhead fieldが正本）。

    `target_head_sha`を持つrecordは、そのactionが束ねられたheadを対象にしなければ
    ならない（head bindingを迂回して別headのrecordを作らせない）。`FIX_RESULT`だけは
    `pushed_head_sha`＝**新しいhead**を対象にするため、この制約を課さない。
    head自体の正当性（PRのadvertised headとの一致）はC-06 / C-10が検証する。
    """
    spec = PROJECTION_SPECS[kind]
    if spec.head_source is None:  # pragma: no cover - 全result variantがhead_sourceを持つ（contract test）
        return action.expected_head_sha
    value = payload.get(spec.head_source)
    if not isinstance(value, str):  # pragma: no cover - result schemaがsha必須を保証する
        return EngineStopped("record_head_missing", f"resultに{spec.head_source}が無い")
    if spec.head_source == _ACTION_BOUND_HEAD_SOURCE and value != action.expected_head_sha:
        return EngineStopped("record_head_mismatch", "resultの対象headがactionのheadと一致しない")
    return value


def _receipt_of(
    envelope: Mapping[str, object], *, submit_hash: str, accepted_at: str
) -> SubmitReceipt:
    kind = envelope.get("result_kind")
    category = envelope.get("error_category")
    return SubmitReceipt(
        action_id=str(envelope["action_id"]),
        nonce=str(envelope["nonce"]),
        outcome=str(envelope["outcome"]),
        submit_hash=submit_hash,
        result_hash=str(envelope["result_hash"]),
        result_kind=RecordKind(kind) if isinstance(kind, str) else None,
        error_category=ErrorCategory(category) if isinstance(category, str) else None,
        accepted_at=accepted_at,
    )


def _completed(
    loaded: _Loaded,
    action: PendingAction,
    receipt: SubmitReceipt,
    spec: ActionSpec,
    *,
    paths: StatePaths,
    run_id: str,
    records_port: RecordSourcePort,
    body_port: RecordBodyPort,
    max_result_bytes: int,
    speaker: str,
    model: str,
) -> SubmitOutcome:
    """成功submit: 結果を検証し、`RecordProduced`までstateを進めてtransactionを保存する。"""
    if receipt.result_kind is None:  # pragma: no cover - schemaのcross-field ruleが保証する
        return EngineStopped("result_kind_missing", "COMPLETEDにresult_kindが無い")
    variant = spec.variant_for(receipt.result_kind)
    if variant is None:
        return EngineStopped(
            "result_kind_not_allowed",
            f"{spec.kind}は{receipt.result_kind.value}を結果として受理しない",
        )
    result = read_result(
        loaded.run_dir,
        action.result_path,
        definition=variant.result_definition,
        max_bytes=max_result_bytes,
    )
    if isinstance(result, ResultRejected):
        return EngineStopped(result.code, result.detail)
    if result.content_hash != receipt.result_hash:
        return EngineStopped("result_hash_mismatch", "result fileのhashがsubmitと一致しない")
    head_sha = _record_head(receipt.result_kind, result.payload, action)
    if isinstance(head_sha, EngineStopped):
        return head_sha
    prepared = prepare_public_body(
        body_port.body_for(receipt.result_kind, result.payload), speaker=speaker, model=model
    )
    issued = issue_transaction(
        kind=receipt.result_kind,
        payload=result.payload,
        run_id=run_id,
        head_sha=head_sha,
        body=prepared.text,
        records=records_port.verified_records(run_id),
    )
    if isinstance(issued, TransactionUnavailable):
        return EngineStopped("transaction_unavailable", issued.detail)
    try:
        machine_state, commands = transition(
            loaded.machine_state,
            ev.RecordProduced(kind=receipt.result_kind, binding=OpaqueBinding(issued.binding)),
        )
    except (TransitionRejected, ev.IllegalEventError) as error:
        return EngineStopped("illegal_event", f"C-01が結果を受理しない: {error}")
    payload = with_machine_state(loaded.payload, machine_state)
    payload["transaction"] = transaction_section(issued)
    payload = without_pending_action(with_receipt(payload, receipt))
    save_checkpoint(checkpoint_path(paths, run_id), payload)
    return SubmitAccepted(
        receipt=receipt, machine_state=machine_state, commands=commands, transaction=issued
    )


def _failed(
    loaded: _Loaded,
    action: PendingAction,
    receipt: SubmitReceipt,
    *,
    paths: StatePaths,
    run_id: str,
    max_result_bytes: int,
    retry_budget: int,
) -> SubmitOutcome:
    """失敗submit: 失敗詳細を受理し、retryできるかを決める（ADR-0015）。

    `FAILED`は「hostが結果を出せなかった」に限る。permission停止・外部依存・質問・
    判断依頼は`COMPLETED`のresult variantであり、ここへは来ない。
    """
    result = read_result(
        loaded.run_dir, action.result_path, definition=HOST_FAILURE, max_bytes=max_result_bytes
    )
    if isinstance(result, ResultRejected):
        return EngineStopped(result.code, result.detail)
    if result.content_hash != receipt.result_hash:
        return EngineStopped("result_hash_mismatch", "失敗詳細fileのhashがsubmitと一致しない")
    envelope_category = receipt.error_category.value if receipt.error_category is not None else None
    if result.payload.get("error_category") != envelope_category:
        return EngineStopped("failure_mismatch", "失敗詳細のerror_categoryがsubmitと一致しない")
    summary = redact(str(result.payload.get("summary", ""))).text
    retryable = receipt.error_category is ErrorCategory.TRANSIENT and action.attempt < retry_budget
    payload = with_receipt(loaded.payload, receipt)
    if retryable:
        # 未完了actionは残す。次のadvanceが同じlogical actionの次のattemptを発行する
        save_checkpoint(checkpoint_path(paths, run_id), payload)
        return SubmitAccepted(
            receipt=receipt,
            machine_state=loaded.machine_state,
            commands=(),
            retry_available=True,
            failure_summary=summary,
        )
    try:
        machine_state, commands = transition(loaded.machine_state, ev.RunFailed())
    except TransitionRejected as error:
        return EngineStopped("illegal_event", f"C-01が失敗を受理しない: {error}")
    payload = without_pending_action(with_machine_state(payload, machine_state))
    save_checkpoint(checkpoint_path(paths, run_id), payload)
    return SubmitAccepted(
        receipt=receipt, machine_state=machine_state, commands=commands, failure_summary=summary
    )


def submit(
    raw: bytes,
    *,
    paths: StatePaths,
    run_id: str,
    repository: str,
    number: int,
    records_port: RecordSourcePort,
    body_port: RecordBodyPort,
    max_result_bytes: int,
    retry_budget: int,
    accepted_at: str,
    speaker: str,
    model: str,
) -> SubmitOutcome:
    """hostのsubmitを一度だけconsumeする（AC-C08-05）。

    受理済みattemptの**同一内容**の再送は以前と同じ結果を返し（冪等）、内容の異なる
    再送は停止する。判定はsubmit envelope全体のcanonical hash（`submit_hash`）で行う。
    """
    parsed = load_with_migration(SUBMIT, raw)
    if not parsed.ok or parsed.payload is None:
        codes = ",".join(sorted(error.code for error in parsed.errors))
        return EngineStopped("submit_invalid", f"submitが検証を通らない（stage={parsed.stage}, {codes}）")
    envelope = parsed.payload
    if envelope.get("run_id") != run_id:
        return EngineStopped("run_mismatch", "submitのrun IDが一致しない")
    loaded = _load(paths, run_id=run_id, repository=repository, number=number)
    if isinstance(loaded, EngineStopped):
        return loaded
    receipts = read_receipts(loaded.payload)
    if isinstance(receipts, SectionUnavailable):  # pragma: no cover - schema検証がreceiptの形を保証する
        return EngineStopped("host_action_unavailable", receipts.detail)
    submit_hash = canonical_payload_hash(envelope)
    known = find_receipt(
        receipts, action_id=str(envelope.get("action_id")), nonce=str(envelope.get("nonce"))
    )
    if known is not None:
        if known.submit_hash == submit_hash:
            return SubmitReplayed(receipt=known)
        return EngineStopped("duplicate_mismatch", "受理済みattemptへ内容の異なるsubmitが届いた")
    pending = read_pending_action(loaded.payload)
    if isinstance(pending, SectionUnavailable):
        return EngineStopped("host_action_unavailable", pending.detail)
    if pending is None:
        return EngineStopped("stale_action", "未完了のactionが無い")
    mismatch = _binding_mismatch(envelope, pending)
    if mismatch is not None:
        return mismatch
    spec = spec_for_kind(pending.action_kind)
    if spec is None:  # pragma: no cover - 発行時に検証済みで、checkpointは検証を通っている
        return EngineStopped("unknown_action_kind", f"未知のaction種別: {pending.action_kind}")
    reissued = _reissue(loaded, pending)
    if isinstance(reissued, EngineStopped):
        return reissued
    receipt = _receipt_of(envelope, submit_hash=submit_hash, accepted_at=accepted_at)
    if receipt.outcome == "COMPLETED":
        return _completed(
            loaded,
            pending,
            receipt,
            spec,
            paths=paths,
            run_id=run_id,
            records_port=records_port,
            body_port=body_port,
            max_result_bytes=max_result_bytes,
            speaker=speaker,
            model=model,
        )
    return _failed(
        loaded,
        pending,
        receipt,
        paths=paths,
        run_id=run_id,
        max_result_bytes=max_result_bytes,
        retry_budget=retry_budget,
    )
