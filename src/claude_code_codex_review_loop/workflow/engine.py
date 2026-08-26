# SPDX-License-Identifier: Apache-2.0
"""step engine: `advance`と`submit`（Phase 8。implementation plan Section 2 / ADR-0015）。

Controller CLIはClaude Code sessionの子processであり、親のLLM turnを呼び戻せない。engineは
Claudeを起動せず、`advance`で次にすべきことを返し、active hostが自分のcontextで実行して
`submit`で結果を返す。**同一runに対する制御経路はこの2つだけ**である（AC-C08-03）。

`advance`が返すのは`HOST_ACTION`（agentへの依頼）か`AWAIT_USER`（ユーザーへの依頼）で、
どちらも同じ形（envelopeの払い出し -> 未完了なら再提示）で扱う。`submit`は両者の応答を
**同一のentry point**で受け、envelopeの構造（`action_id` / `request_id`の排他）で判別する
（ADR-0018）。ユーザー入力側の実装は`user_input`にある。

「pure」はprocess / CLIに依存しないという意味で、I/Oが無いという意味ではない。値の供給元は
port（`ports`）、path・ID・時刻・上限値は引数で受け取り、engine自身は既定値を持たない。

順序（ADR-0015。crash windowで重複を作らないための不変条件）:

- `advance`: envelopeの保存 -> checkpointの保存 -> hostへ返却
- `submit`: 受理 -> **receiptとrecord transactionを同じcheckpoint更新で保存** ->
  （投稿以降は`persistence`のPersistRecord実行）

`submit`は`RecordProduced`までを扱う。GitHubへの投稿・検証と`*Verified` eventの組み立ては
C-01の`PersistRecord`実行（`persistence`）が行うため、`advance`はpending recordがある間
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
from ..domain.values import Awaiting, MachineState, PendingRecord, RecordKind, TransitionRejected
from ..errors import ErrorCategory
from ..identity.errors import IdentityError
from ..identity.fs_permissions import create_private_dir, verify_private_dir, write_private_text
from ..policy.redaction import redact
from ..schema.action import HOST_ACTION, HOST_FAILURE, SUBMIT
from ..schema.envelope import MAX_SUBMIT_RECEIPTS
from ..schema.migrate import load_with_migration
from ..schema.projection import PROJECTION_SPECS, canonical_json, canonical_payload_hash
from ..schema.registry import SchemaDefinition, parse_json_object, validate_object
from ..schema.user_input import USER_SUBMIT
from ..schema.validate import ValidationResult
from ..state import (
    StatePaths,
    checkpoint_path,
    save_checkpoint,
)
from ..transport.render import prepare_public_body
from .actions import ActionSpec, spec_for_awaiting, spec_for_kind, user_spec_for
from .checkpoint_view import (
    PendingAction,
    PendingUserRequest,
    SectionUnavailable,
    SubmitReceipt,
    find_receipt,
    next_attempt,
    read_pending_action,
    read_receipts,
    read_user_section,
    with_machine_state,
    with_new_logical_action,
    with_receipt,
    with_retry_attempt,
    without_pending_action,
)
from .ports import (
    ActionContext,
    ActionPayloadPort,
    EvidencePort,
    RecordBodyPort,
    RecordSourcePort,
    RequestContext,
    UserRequestContext,
)
from .results import ResultRejected, read_result
from .run_context import EngineStopped, RunContext, load_run
from .transaction import IssuedTransaction, ProduceRejected, produce_record, transaction_section
from .user_input import (
    AwaitUser,
    UserInputAccepted,
    UserInputReplayed,
    UserIntentAlreadyRecorded,
    accept_user_submit,
    issue_user_request,
    reissue_user_request,
)

ACTIONS_DIR = "actions"
ENVELOPE_FILE = "action.json"
RESULT_FILE = "result.json"
HOST_ACTION_VERSION = 2
SUBMIT_VERSION = 2

_USER_INPUT_AWAITINGS = frozenset(
    {Awaiting.USER_INPUT_DECISION, Awaiting.USER_INPUT_GATE, Awaiting.USER_INPUT_PERMISSION}
)


@dataclass(frozen=True)
class HostActionIssued:
    """active hostが実行する`HOST_ACTION`（新規発行または未完了actionの再提示）。"""

    action: PendingAction
    envelope: dict[str, object]
    envelope_path: Path
    result_path: Path
    reissued: bool


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


SubmitOutcome = (
    SubmitAccepted
    | SubmitReplayed
    | UserInputAccepted
    | UserInputReplayed
    | UserIntentAlreadyRecorded
    | EngineStopped
)

# submit envelopeの判別key。`HOST_ACTION`への応答は`action_id`を、`AWAIT_USER`への応答は
# `request_id`を必須で持ち、**両者は互いに素**である（contract testで固定する）。片方の
# 定義で試して失敗したらもう片方、という推測経路は作らない（ADR-0018 決定5）
_HOST_SUBMIT_KEY = "action_id"
_USER_SUBMIT_KEY = "request_id"
_MAX_SUBMIT_BYTES = max(SUBMIT.max_input_bytes, USER_SUBMIT.max_input_bytes)


def _classify_submit(raw: bytes) -> tuple[SchemaDefinition, dict[str, object]] | EngineStopped:
    """submit envelopeを構造で判別する（曖昧ならfail closed）。"""
    parsed = parse_json_object(raw, max_input_bytes=_MAX_SUBMIT_BYTES)
    if isinstance(parsed, ValidationResult):
        codes = ",".join(sorted(error.code for error in parsed.errors))
        return EngineStopped("submit_invalid", f"submitを読めない（stage={parsed.stage}, {codes}）")
    host = _HOST_SUBMIT_KEY in parsed
    user = _USER_SUBMIT_KEY in parsed
    if host == user:
        return EngineStopped(
            "submit_unclassified",
            f"submitが{_HOST_SUBMIT_KEY} / {_USER_SUBMIT_KEY}のちょうど一方を持たない",
        )
    return (SUBMIT if host else USER_SUBMIT), parsed


def _ensure_private_dir(path: Path) -> None:
    if path.exists():
        verify_private_dir(path)
        return
    create_private_dir(path)


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
    evidence_kinds: tuple[RecordKind, ...],
    label: str,
    port: EvidencePort,
    context: RequestContext,
) -> tuple[tuple[str, str], ...] | EngineStopped:
    """同梱する検証済みrecord（AC-C08-07）。許可kind・**対象head**・seq昇順を検査する。

    headを検査しないと、`expected_head_sha`が別headのenvelopeへ、その head を対象に
    していない根拠を同梱できてしまう（head bindingの迂回）。actionは特定headへbindされ、
    その head を対象に検証されたrecordだけが根拠になり得るため、不一致はfail closedで
    停止する。head跨ぎの根拠が要るactionが将来現れた場合は、registryの明示的な規則として
    追加する（暗黙に通さない）。
    """
    records = tuple(port.evidence_for(context))
    for record in records:
        if record.kind not in evidence_kinds:
            return EngineStopped(
                "evidence_kind", f"{label}の根拠に使えないrecord種別: {record.kind.value}"
            )
        if record.head_sha != context.head_sha:
            return EngineStopped(
                "evidence_head", f"根拠recordの対象headがactionのheadと一致しない: {record.comment_id}"
            )
    if [record.seq for record in records] != sorted(record.seq for record in records):
        return EngineStopped("evidence_order", "根拠recordがseq昇順でない")
    return tuple((record.comment_id, record.head_sha) for record in records)


def _issue_action(
    loaded: RunContext,
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
    evidence = _evidence_of(spec.evidence_kinds, spec.kind, evidence_port, context)
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
    # retryは同じlogical actionなのでledgerを保ち、fresh actionでは入れ替える
    # （前のlogical actionのreceiptを持ち越さない。ADR-0015 決定22）
    updated = (
        with_retry_attempt(loaded.payload, action)
        if previous is not None
        else with_new_logical_action(loaded.payload, action)
    )
    save_checkpoint(checkpoint_path(paths, run_id), updated)
    return HostActionIssued(
        action=action,
        envelope=envelope,
        envelope_path=loaded.run_dir / envelope_rel,
        result_path=loaded.run_dir / result_rel,
        reissued=False,
    )


def _reissue(loaded: RunContext, action: PendingAction) -> HostActionIssued | EngineStopped:
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


def _describes_current_instance(
    request: PendingUserRequest, *, awaiting: Awaiting, head_sha: str, since_seq: int
) -> bool:
    """未応答requestが**今の待機**を指しているか（ADR-0018 決定12）。

    3つとも一致しなければ再提示しない。

    - `awaiting`: C-01が別の入力を待っていれば、そのrequestはもう答えられない
    - `head_sha`: headが動けばrecordのbind先が変わる（head binding）
    - `since_seq`: awaiting instanceの識別子。ここがずれたまま再提示すると、前instanceの
      消費済みintentが**次の入力を重複と判定して飲み込む**（決定10で未応答requestを残す
      契約のため、同種awaitingへ再到達したときに現れる）

    一致しないrequestは停止理由ではなく、**現在のinstanceのrequestを新規発行する**契機で
    ある。sectionごと入れ替わるので、前instanceのreceiptと消費済みintentも残らない。
    """
    return (
        request.awaiting is awaiting
        and request.expected_head_sha == head_sha
        and request.since_seq == since_seq
    )


def _await_user(
    loaded: RunContext,
    awaiting: Awaiting,
    *,
    paths: StatePaths,
    run_id: str,
    repository: str,
    number: int,
    head_sha: str,
    records_port: RecordSourcePort,
    evidence_port: EvidencePort,
    id_source: Callable[[], str],
    issued_at: str,
) -> AdvanceOutcome:
    """ユーザー入力待ちのrequestを返す（**現在のinstanceの**未応答があれば再提示する）。

    chain検証は再提示にも先立って行う。requestを発行した後にchainが壊れることがあり、
    pendingがあるからと素通しすると、壊れたchainの上で判断を求めることになる
    （ADR-0018 決定13）。
    """
    spec = user_spec_for(awaiting)
    if spec is None:  # pragma: no cover - 呼び出し元が3値のawaitingでのみ入る
        return EngineStopped("not_user_input", f"{awaiting.value}はユーザー入力待ちではない")
    chain = records_port.chain(run_id)
    if not chain.is_intact:
        # 壊れたchainの上でユーザーへ判断を求めない。提示する根拠recordの正当性が
        # 確かめられておらず、承認をそこへbindできない（integrityの解消が先）
        return EngineStopped("chain_violation", f"chainにviolationがある（{len(chain.violations)}件）")
    section = read_user_section(loaded.payload)
    if isinstance(section, SectionUnavailable):
        return EngineStopped("user_request_unavailable", section.detail)
    pending = section.pending
    if pending is not None and _describes_current_instance(
        pending, awaiting=awaiting, head_sha=head_sha, since_seq=chain.max_seq
    ):
        return reissue_user_request(loaded, pending)
    context = UserRequestContext(
        awaiting=awaiting, run_id=run_id, repository=repository, number=number, head_sha=head_sha
    )
    evidence = _evidence_of(spec.evidence_kinds, spec.kind, evidence_port, context)
    if isinstance(evidence, EngineStopped):
        return evidence
    return issue_user_request(
        loaded,
        spec,
        paths=paths,
        run_id=run_id,
        repository=repository,
        number=number,
        head_sha=head_sha,
        since_seq=chain.max_seq,
        evidence=evidence,
        id_source=id_source,
        issued_at=issued_at,
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
    records_port: RecordSourcePort,
    id_source: Callable[[], str],
    issued_at: str,
) -> AdvanceOutcome:
    """次に実行すべきことを1つだけ返す（1回のadvanceで1 action）。

    未完了actionがある間は**新しいactionを発行しない**。既にsubmitを受理した
    （receiptがある）未完了actionは、retryできる失敗として次のattemptを発行する。
    """
    loaded = load_run(paths, run_id=run_id, repository=repository, number=number)
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
        receipt = find_receipt(receipts, action_id=pending.action_id, nonce=pending.nonce)
        if receipt is None:
            return _reissue(loaded, pending)
        # receiptがあるのに未完了actionが残るのは「retryできる失敗」の場合だけ。他の組合せ
        # （migrationで持ち上げたCOMPLETED、非retryableな失敗）は、この不変条件が成立して
        # いないことを意味するので、次のattemptを作らず停止する（完了済みactionの再実行を
        # 防ぐ。ADR-0015 決定24）
        if receipt.outcome != "FAILED" or receipt.error_category is not ErrorCategory.TRANSIENT:
            return EngineStopped(
                "attempt_not_retryable",
                f"受理済みsubmit（{receipt.outcome}）を持つactionを再試行しない: {pending.action_id}",
            )
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
        return _await_user(
            loaded,
            awaiting,
            paths=paths,
            run_id=run_id,
            repository=repository,
            number=number,
            head_sha=head_sha,
            records_port=records_port,
            evidence_port=evidence_port,
            id_source=id_source,
            issued_at=issued_at,
        )
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
    loaded: RunContext,
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
    produced = produce_record(
        loaded.machine_state,
        kind=receipt.result_kind,
        payload=result.payload,
        run_id=run_id,
        head_sha=head_sha,
        body=prepared.text,
        chain=records_port.chain(run_id),
    )
    if isinstance(produced, ProduceRejected):
        return EngineStopped(produced.code, produced.detail)
    payload = with_machine_state(loaded.payload, produced.machine_state)
    payload["transaction"] = transaction_section(produced.transaction)
    payload = without_pending_action(with_receipt(payload, receipt))
    save_checkpoint(checkpoint_path(paths, run_id), payload)
    return SubmitAccepted(
        receipt=receipt,
        machine_state=produced.machine_state,
        commands=produced.commands,
        transaction=produced.transaction,
    )


def _failed(
    loaded: RunContext,
    action: PendingAction,
    receipt: SubmitReceipt,
    *,
    paths: StatePaths,
    run_id: str,
    max_result_bytes: int,
    retry_budget: int,
    ledger_size: int,
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
    # budgetは呼び出し側が決めるが、ledgerがcheckpointへ収まらない大きさにはしない
    # （schemaのmax_itemsと同じ境界でretryを打ち切る）
    retryable = (
        receipt.error_category is ErrorCategory.TRANSIENT
        and action.attempt < retry_budget
        and ledger_size + 1 < MAX_SUBMIT_RECEIPTS
    )
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
    user_speaker: str,
) -> SubmitOutcome:
    """hostのsubmitを一度だけconsumeする（AC-C08-05）。

    受理済みattemptの**同一内容**の再送は以前と同じ結果を返し（冪等）、内容の異なる
    再送は停止する。判定はsubmit envelope全体のcanonical hash（`submit_hash`）で行う。
    """
    classified = _classify_submit(raw)
    if isinstance(classified, EngineStopped):
        return classified
    definition, _ = classified
    parsed = load_with_migration(definition, raw)
    if not parsed.ok or parsed.payload is None:
        codes = ",".join(sorted(error.code for error in parsed.errors))
        return EngineStopped("submit_invalid", f"submitが検証を通らない（stage={parsed.stage}, {codes}）")
    envelope = parsed.payload
    if envelope.get("run_id") != run_id:
        return EngineStopped("run_mismatch", "submitのrun IDが一致しない")
    loaded = load_run(paths, run_id=run_id, repository=repository, number=number)
    if isinstance(loaded, EngineStopped):
        return loaded
    if definition is USER_SUBMIT:
        return accept_user_submit(
            envelope,
            submit_hash=canonical_payload_hash(envelope),
            run=loaded,
            paths=paths,
            run_id=run_id,
            records_port=records_port,
            body_port=body_port,
            max_result_bytes=max_result_bytes,
            accepted_at=accepted_at,
            speaker=user_speaker,
        )
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
        ledger_size=len(receipts),
    )
