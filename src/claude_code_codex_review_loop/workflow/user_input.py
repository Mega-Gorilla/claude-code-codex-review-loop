# SPDX-License-Identifier: Apache-2.0
"""`AWAIT_USER`の搬送路（Phase 8 PR-2c。ADR-0018）。

`advance`が返す3つのoutcomeのうち、ユーザー入力待ちだけが応答経路を持っていなかった。
本moduleがその往復を実装する。

```
advance -> AwaitUser（request envelopeを払い出す）
        -> hostがユーザー入力をintentへ構造化し、result fileへ書く
        -> submit（USER_SUBMIT envelope）-> RecordProduced + transaction
        -> persist（PR-2bの汎用境界がそのまま転記する）
```

**意味解釈とgate semanticsはC-13が所有する**。ここが持つのは搬送路（envelope / binding /
冪等 / 転記順序 / 重複防止key）だけで、「この入力がmerge承認として十分か」は判定しない。

recordを作らない応答は`USER_INPUT_PERMISSION`の明示resumeだけで、C-06の
`validate_permission_resume`（AC-C06-04）へ委ねる。投稿もtransactionも作らない。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..domain import events as ev
from ..domain.commands import Command
from ..domain.machine import transition
from ..domain.values import Awaiting, MachineState, RecordKind, TransitionRejected
from ..identity.errors import IdentityError
from ..identity.fs_permissions import create_private_dir, verify_private_dir, write_private_text
from ..identity.permissions import (
    PermissionCheckpoint,
    PermissionResumeError,
    ResumeRequest,
    validate_permission_resume,
)
from ..schema.migrate import load_with_migration
from ..schema.projection import PROJECTION_SPECS, canonical_json, canonical_payload_hash
from ..schema.registry import validate_object
from ..schema.user_input import HOST_TRANSCRIPT_ROUTE, PERMISSION_RESUME, USER_REQUEST
from ..state import StatePaths, checkpoint_path, save_checkpoint
from ..transport.render import prepare_user_body
from .actions import UserRequestSpec, intent_key, intent_value_of, user_spec_for
from .checkpoint_view import (
    ConsumedIntent,
    PendingUserRequest,
    SectionUnavailable,
    UserRequestReceipt,
    read_user_section,
    with_consumed_intent,
    with_machine_state,
    with_user_receipt,
    with_user_request,
    without_user_request,
)
from .ports import RecordBodyPort, RecordSourcePort
from .results import ResultRejected, read_result
from .run_context import EngineStopped, RunContext
from .transaction import IssuedTransaction, ProduceRejected, produce_record, transaction_section

REQUESTS_DIR = "requests"
ENVELOPE_FILE = "request.json"
RESULT_FILE = "result.json"
USER_REQUEST_VERSION = 1


@dataclass(frozen=True)
class AwaitUser:
    """ユーザー入力待ち（新規発行または未応答requestの再提示）。

    `HostActionIssued`と同型で、hostは`envelope`を読んでユーザーへ提示し、構造化した
    intentを`result_path`へ書いてsubmitする。
    """

    awaiting: Awaiting
    request: PendingUserRequest
    envelope: dict[str, object]
    envelope_path: Path
    result_path: Path
    reissued: bool


@dataclass(frozen=True)
class UserInputAccepted:
    """ユーザー入力を受理した（nonceは消費済み）。

    `transaction`があるものはrecordを転記する経路で、以降は`persist`が扱う。permission
    resumeは`transaction`を持たない（投稿するrecordが無い）。
    """

    receipt: UserRequestReceipt
    machine_state: MachineState
    commands: tuple[Command, ...]
    transaction: IssuedTransaction | None = None


@dataclass(frozen=True)
class UserInputReplayed:
    """同一内容の再送（冪等。状態を進めず、以前と同じ結果を返す）。"""

    receipt: UserRequestReceipt


@dataclass(frozen=True)
class UserIntentAlreadyRecorded:
    """同じintentが**別経路で**既に記録されている（2件目のrecordを作らない）。

    GitHub直接comment（経路2）をC-13が受理すると、同じawaiting instanceの同じintentが
    canonical recordとして確定する。その後に届く転記submitへ「requestが古い」とだけ返すと、
    runが壊れたのか決定が済んだのかを呼び出し側が区別できない。消費済み台帳を照合して
    **どのbindingで確定したか**を返す（ADR-0018 決定9）。
    """

    consumed: ConsumedIntent


UserInputOutcome = UserInputAccepted | UserInputReplayed | UserIntentAlreadyRecorded | EngineStopped


def _ensure_private_dir(path: Path) -> None:
    if path.exists():
        verify_private_dir(path)
        return
    create_private_dir(path)


def _request_paths(request_id: str) -> tuple[str, str]:
    """envelopeとresultのrun directory相対path（区切りは`/`で固定する）。"""
    return (
        f"{REQUESTS_DIR}/{request_id}/{ENVELOPE_FILE}",
        f"{REQUESTS_DIR}/{request_id}/{RESULT_FILE}",
    )


def _build_envelope(
    spec: UserRequestSpec,
    *,
    run_id: str,
    repository: str,
    number: int,
    head_sha: str,
    request_id: str,
    nonce: str,
    result_path: str,
    since_seq: int,
    evidence: Sequence[tuple[str, str]],
) -> dict[str, object]:
    return {
        "schema_version": USER_REQUEST_VERSION,
        "run_id": run_id,
        "request_id": request_id,
        "repository": repository,
        "number": number,
        "expected_head_sha": head_sha,
        "awaiting": spec.kind,
        "nonce": nonce,
        "result_path": result_path,
        "since_seq": since_seq,
        "accepted_result_kinds": [kind.value for kind in spec.result_kinds],
        "verified_records": [
            {"comment_id": comment_id, "head_sha": record_head}
            for comment_id, record_head in evidence
        ],
    }


def issue_user_request(
    run: RunContext,
    spec: UserRequestSpec,
    *,
    paths: StatePaths,
    run_id: str,
    repository: str,
    number: int,
    head_sha: str,
    since_seq: int,
    evidence: Sequence[tuple[str, str]],
    id_source: Callable[[], str],
    issued_at: str,
) -> AwaitUser | EngineStopped:
    """新しいuser requestを発行する（envelopeを保存してからcheckpointを更新する）。"""
    request_id = id_source()
    nonce = id_source()
    envelope_rel, result_rel = _request_paths(request_id)
    envelope = _build_envelope(
        spec,
        run_id=run_id,
        repository=repository,
        number=number,
        head_sha=head_sha,
        request_id=request_id,
        nonce=nonce,
        result_path=result_rel,
        since_seq=since_seq,
        evidence=evidence,
    )
    validation = validate_object(USER_REQUEST, dict(envelope))
    if not validation.ok:
        codes = ",".join(sorted(error.code for error in validation.errors))
        return EngineStopped("request_invalid", f"USER_REQUESTが検証を通らない（{codes}）")
    request = PendingUserRequest(
        request_id=request_id,
        awaiting=spec.awaiting,
        nonce=nonce,
        expected_head_sha=head_sha,
        result_path=result_rel,
        envelope_path=envelope_rel,
        envelope_hash=canonical_payload_hash(envelope),
        since_seq=since_seq,
        issued_at=issued_at,
    )
    try:
        _ensure_private_dir(run.run_dir / REQUESTS_DIR)
        _ensure_private_dir(run.run_dir / REQUESTS_DIR / request_id)
        write_private_text(run.run_dir / envelope_rel, canonical_json(envelope))
    except IdentityError as error:
        return EngineStopped("request_write", f"user requestを保存できない: {error}")
    save_checkpoint(checkpoint_path(paths, run_id), with_user_request(run.payload, request))
    return AwaitUser(
        awaiting=spec.awaiting,
        request=request,
        envelope=envelope,
        envelope_path=run.run_dir / envelope_rel,
        result_path=run.run_dir / result_rel,
        reissued=False,
    )


def reissue_user_request(run: RunContext, request: PendingUserRequest) -> AwaitUser | EngineStopped:
    """未応答requestを**そのまま**再提示する（新しいrequestを生成しない）。"""
    path = run.run_dir / request.envelope_path
    if not path.is_file():
        return EngineStopped("request_missing", f"user request envelopeが無い: {request.envelope_path}")
    parsed = load_with_migration(USER_REQUEST, path.read_bytes())
    if not parsed.ok or parsed.payload is None:
        codes = ",".join(sorted(error.code for error in parsed.errors))
        return EngineStopped("request_invalid", f"保存済みrequestが検証を通らない（{codes}）")
    if canonical_payload_hash(parsed.payload) != request.envelope_hash:
        return EngineStopped("request_mismatch", "保存済みrequestのhashがcheckpointと一致しない")
    return AwaitUser(
        awaiting=request.awaiting,
        request=request,
        envelope=parsed.payload,
        envelope_path=path,
        result_path=run.run_dir / request.result_path,
        reissued=True,
    )


def _binding_mismatch(
    envelope: Mapping[str, object], request: PendingUserRequest
) -> EngineStopped | None:
    """submitのbinding echoを未応答requestと突き合わせる（AC-C08-05と同じ規則）。"""
    if envelope.get("request_id") != request.request_id or envelope.get("nonce") != request.nonce:
        return EngineStopped("stale_request", "submitが未応答のuser requestを指していない")
    if envelope.get("awaiting") != request.awaiting.value:
        return EngineStopped("awaiting_mismatch", "submitの待機種別が一致しない")
    if envelope.get("expected_head_sha") != request.expected_head_sha:
        return EngineStopped("head_mismatch", "submitの対象headが一致しない")
    return None


def _bound_head(kind: RecordKind, payload: Mapping[str, object], request: PendingUserRequest) -> EngineStopped | None:
    """user-input recordの対象headがrequestのheadと一致することを要求する。

    user-input recordは新しいheadを作らない（`FIX_RESULT`のようなpush後headを持たない）
    ため、`target_head_sha`も`approved_head_sha`もrequestが束ねたheadでなければならない。
    ここを緩めると、承認を別headのrecordとして作らせる余地ができる（head binding。D-031）。
    """
    spec = PROJECTION_SPECS[kind]
    if spec.head_source is None:  # pragma: no cover - 全user-input kindがhead_sourceを持つ（contract test）
        return EngineStopped("record_head_missing", f"{kind.value}が対象headを持たない")
    value = payload.get(spec.head_source)
    if not isinstance(value, str):  # pragma: no cover - record schemaがsha必須を保証する
        return EngineStopped("record_head_missing", f"resultに{spec.head_source}が無い")
    if value != request.expected_head_sha:
        return EngineStopped("record_head_mismatch", "resultの対象headがrequestのheadと一致しない")
    return None


def _dedup(
    request: PendingUserRequest,
    consumed: ConsumedIntent | None,
    *,
    run_id: str,
    kind: RecordKind,
    payload: Mapping[str, object],
) -> UserIntentAlreadyRecorded | EngineStopped | str:
    """2経路の重複防止（ADR-0018 決定7 / 8）。通れば当該intentのkeyを返す。

    `payload`は**検証済みのrecord payload**である。`USER_DECISION`のようにkindだけでは
    正規化intentが決まらない種別があるため、keyを作る前に結果を読み終えている必要がある。
    """
    key = intent_key(
        run_id=run_id,
        awaiting=request.awaiting,
        since_seq=request.since_seq,
        head_sha=request.expected_head_sha,
        kind=kind,
        intent_value=intent_value_of(kind, payload),
    )
    if consumed is None:
        return key
    if consumed.intent_key == key:
        return UserIntentAlreadyRecorded(consumed=consumed)
    # 同じ待機に対して2経路が**別のintent**を主張している。どちらがユーザーの意思かを
    # 推測せず停止する（曖昧な入力を承認として解釈しない、と同じ原則）
    return EngineStopped(
        "user_intent_conflict", f"同一instanceに別のintentが記録済み: {consumed.intent_key}"
    )


def _record_path(
    run: RunContext,
    request: PendingUserRequest,
    spec: UserRequestSpec,
    *,
    kind: RecordKind,
    consumed: ConsumedIntent | None,
    result_hash: str,
    paths: StatePaths,
    run_id: str,
    records_port: RecordSourcePort,
    body_port: RecordBodyPort,
    max_result_bytes: int,
    accepted_at: str,
    speaker: str,
    submit_hash: str,
) -> UserInputOutcome:
    """recordを作る応答（判断回答・gate質問 / 変更依頼 / merge承認・cancel）。"""
    variant = spec.variant_for(kind)
    if variant is None:
        return EngineStopped(
            "result_kind_not_allowed", f"{spec.kind}は{kind.value}を結果として受理しない"
        )
    result = read_result(
        run.run_dir, request.result_path, definition=variant.result_definition, max_bytes=max_result_bytes
    )
    if isinstance(result, ResultRejected):
        return EngineStopped(result.code, result.detail)
    if result.content_hash != result_hash:
        return EngineStopped("result_hash_mismatch", "result fileのhashがsubmitと一致しない")
    head_mismatch = _bound_head(kind, result.payload, request)
    if head_mismatch is not None:
        return head_mismatch
    if result.payload.get("input_route") != HOST_TRANSCRIPT_ROUTE:
        # 転記経路のrecordがGitHub直接comment由来を名乗ると、C-06 / C-13が受理主体を
        # 取り違える（D-031の照合対象が変わる）。経路の詐称を構造的に止める
        return EngineStopped("input_route_mismatch", "転記recordのinput_routeが転記経路でない")
    # **結果を読み終えてからkeyを作る**。`USER_DECISION`の正規化intentは回答値であり、
    # kindだけで作ったkeyでは同じdecisionへの別回答が同一intentへ潰れる
    deduped = _dedup(request, consumed, run_id=run_id, kind=kind, payload=result.payload)
    if not isinstance(deduped, str):
        return deduped
    stale = _still_awaited(run, request)
    if stale is not None:
        return stale
    prepared = prepare_user_body(
        body_port.body_for(kind, result.payload), speaker=speaker, route=HOST_TRANSCRIPT_ROUTE
    )
    produced = produce_record(
        run.machine_state,
        kind=kind,
        payload=result.payload,
        run_id=run_id,
        head_sha=request.expected_head_sha,
        body=prepared.text,
        chain=records_port.chain(run_id),
    )
    if isinstance(produced, ProduceRejected):
        return EngineStopped(produced.code, produced.detail)
    issued = produced.transaction
    machine_state, commands = produced.machine_state, produced.commands
    receipt = UserRequestReceipt(
        request_id=request.request_id,
        nonce=request.nonce,
        submit_hash=submit_hash,
        result_hash=result_hash,
        intent_key=deduped,
        result_kind=kind,
        accepted_at=accepted_at,
    )
    payload = with_machine_state(run.payload, machine_state)
    payload["transaction"] = transaction_section(issued)
    payload = with_consumed_intent(
        with_user_receipt(without_user_request(payload), receipt),
        ConsumedIntent(intent_key=deduped, binding=issued.binding, route=HOST_TRANSCRIPT_ROUTE),
    )
    save_checkpoint(checkpoint_path(paths, run_id), payload)
    return UserInputAccepted(
        receipt=receipt, machine_state=machine_state, commands=commands, transaction=issued
    )


def _permission_checkpoint(payload: Mapping[str, object]) -> PermissionCheckpoint | EngineStopped:
    """`permission` sectionから停止点を復元する（欠けていればfail closed）。"""
    section = payload.get("permission")
    if not isinstance(section, dict):
        return EngineStopped("permission_unavailable", "permission sectionが無い")
    values: dict[str, str] = {}
    for name in ("permission_id", "blocked_tool", "requested_scope", "head_sha"):
        value = section.get(name)
        if not isinstance(value, str) or not value:
            return EngineStopped("permission_unavailable", f"permission.{name}が欠如または不正")
        values[name] = value
    return PermissionCheckpoint(
        permission_id=values["permission_id"],
        blocked_tool=values["blocked_tool"],
        requested_scope=values["requested_scope"],
        head_sha=values["head_sha"],
    )


def _resume_path(
    run: RunContext,
    request: PendingUserRequest,
    spec: UserRequestSpec,
    *,
    result_hash: str,
    paths: StatePaths,
    run_id: str,
    max_result_bytes: int,
    accepted_at: str,
    submit_hash: str,
) -> UserInputOutcome:
    """recordを作らない応答（tool permissionの明示resume。AC-C06-04）。"""
    if spec.resume_event is None:  # pragma: no cover - schemaのcross-field ruleが`result_kind`
        # 不在を`USER_INPUT_PERMISSION`へ限定し、その唯一のspecが`resume_event`を持つ
        # （registry contract testで固定）。registryが変わったときの防御として残す
        return EngineStopped("resume_not_allowed", f"{spec.kind}はrecord無しの応答を受理しない")
    result = read_result(
        run.run_dir, request.result_path, definition=PERMISSION_RESUME, max_bytes=max_result_bytes
    )
    if isinstance(result, ResultRejected):
        return EngineStopped(result.code, result.detail)
    if result.content_hash != result_hash:
        return EngineStopped("result_hash_mismatch", "result fileのhashがsubmitと一致しない")
    checkpoint = _permission_checkpoint(run.payload)
    if isinstance(checkpoint, EngineStopped):
        return checkpoint
    try:
        resume = ResumeRequest(
            permission_id=str(result.payload["permission_id"]),
            tool=str(result.payload["tool"]),
            scope=str(result.payload["scope"]),
            current_head_sha=str(result.payload["current_head_sha"]),
        )
        validate_permission_resume(checkpoint, resume)
    except PermissionResumeError as error:
        return EngineStopped("permission_resume_rejected", f"resumeが停止点と一致しない: {error.reason.value}")
    try:
        machine_state, commands = transition(run.machine_state, ev.PermissionResumeValidated())
    except TransitionRejected as error:  # pragma: no cover - 同上（T-21は
        # `AWAITING_TOOL_PERMISSION` + `USER_INPUT_PERMISSION` + pending不在で必ず一致する）
        return EngineStopped("illegal_event", f"C-01がresumeを受理しない: {error}")
    receipt = UserRequestReceipt(
        request_id=request.request_id,
        nonce=request.nonce,
        submit_hash=submit_hash,
        result_hash=result_hash,
        accepted_at=accepted_at,
    )
    payload = with_user_receipt(without_user_request(with_machine_state(run.payload, machine_state)), receipt)
    save_checkpoint(checkpoint_path(paths, run_id), payload)
    return UserInputAccepted(receipt=receipt, machine_state=machine_state, commands=commands)



def _still_awaited(run: RunContext, request: PendingUserRequest) -> EngineStopped | None:
    """C-01がまだこの入力を待っているか（別経路の受理でstateが進んでいないか）。"""
    if run.machine_state.awaiting is not request.awaiting:
        return EngineStopped("request_superseded", "C-01は既にこの待機を消費している")
    if run.machine_state.pending_record is not None:
        return EngineStopped("persist_required", "永続化を待つrecordがあるため新しい入力を受理しない")
    return None


def accept_user_submit(
    envelope: Mapping[str, object],
    *,
    submit_hash: str,
    run: RunContext,
    paths: StatePaths,
    run_id: str,
    records_port: RecordSourcePort,
    body_port: RecordBodyPort,
    max_result_bytes: int,
    accepted_at: str,
    speaker: str,
) -> UserInputOutcome:
    """user-input submitを一度だけconsumeする（`HOST_ACTION` submitと同じ規則）。

    順序は`binding echo -> 冪等判定 -> 結果検証 -> 重複防止key -> C-01がまだ待っているか`。

    - 結果検証をkeyより先に置くのは、`USER_DECISION`のように**正規化intentが結果の中にある**
      種別があるためである（ADR-0018 決定7）
    - key照合を`C-01がまだ待っているか`より先に置くのは、別経路で決定済みのときに
      「requestが古い」ではなく**どのbindingで確定したか**を返すためである（決定9）
    """
    section = read_user_section(run.payload)
    if isinstance(section, SectionUnavailable):
        return EngineStopped("user_request_unavailable", section.detail)
    known = section.receipt
    if (
        known is not None
        and known.request_id == envelope.get("request_id")
        and known.nonce == envelope.get("nonce")
    ):
        if known.submit_hash == submit_hash:
            return UserInputReplayed(receipt=known)
        return EngineStopped("duplicate_mismatch", "受理済みrequestへ内容の異なるsubmitが届いた")
    request = section.pending
    if request is None:
        return EngineStopped("stale_request", "未応答のuser requestが無い")
    mismatch = _binding_mismatch(envelope, request)
    if mismatch is not None:
        return mismatch
    spec = user_spec_for(request.awaiting)
    if spec is None:  # pragma: no cover - awaitingはschemaのenumで3値に限定されている
        return EngineStopped("not_user_input", f"{request.awaiting.value}はユーザー入力待ちではない")
    # `result_kind`の有無がrecordを作る応答かを決める（不在はpermission resume。schemaの
    # cross-field ruleがその組み合わせを`USER_INPUT_PERMISSION`へ限定している）
    kind_value = envelope.get("result_kind")
    if isinstance(kind_value, str):
        return _record_path(
            run,
            request,
            spec,
            kind=RecordKind(kind_value),
            consumed=section.consumed,
            result_hash=str(envelope["result_hash"]),
            paths=paths,
            run_id=run_id,
            records_port=records_port,
            body_port=body_port,
            max_result_bytes=max_result_bytes,
            accepted_at=accepted_at,
            speaker=speaker,
            submit_hash=submit_hash,
        )
    stale = _still_awaited(run, request)
    if stale is not None:
        return stale
    return _resume_path(
        run,
        request,
        spec,
        result_hash=str(envelope["result_hash"]),
        paths=paths,
        run_id=run_id,
        max_result_bytes=max_result_bytes,
        accepted_at=accepted_at,
        submit_hash=submit_hash,
    )
