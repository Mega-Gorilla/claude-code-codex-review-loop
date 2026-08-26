# SPDX-License-Identifier: Apache-2.0
"""checkpointの`state` / `host_action` / `user_request` sectionのreaderとwriter。

Phase 8。ADR-0015（host action）とADR-0018（`AWAIT_USER`）。

schema検証を通ったcheckpointでは解釈できない形にならないが、解釈できない場合に「無い」へ
丸めると、未完了actionの取りこぼしと重複発行の余地を作る。C-07の`read_transaction`と同じく
**構造化直和**で提示する（silent repair禁止）。

writerはpureで、受け取ったpayloadを変更せず新しいdictを返す。保存は呼び出し側が
`state.save_checkpoint`（schema検証 -> atomic replace）で行う。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from ..domain._ruledefs import BlockKind, ProcedureKind
from ..domain.states import State
from ..domain.values import (
    NORMAL,
    Awaiting,
    BlockContext,
    HaltingForBlockProcedure,
    IllegalMachineStateError,
    IntegrityEvidenceRef,
    MachineState,
    OpaqueBinding,
    OpaqueRef,
    PendingRecord,
    Procedure,
    RecordIntegrityBlock,
    RecordKind,
)
from ..errors import ErrorCategory

_SECTION = "host_action"
_USER_SECTION = "user_request"


@dataclass(frozen=True)
class SectionUnavailable:
    """sectionを解釈できない（推測して進めない）。"""

    detail: str


@dataclass(frozen=True)
class PendingAction:
    """未完了の`HOST_ACTION`（1 attempt = 1 action ID = 1 nonce）。"""

    action_id: str
    action_kind: str
    nonce: str
    expected_head_sha: str
    result_path: str
    envelope_path: str
    envelope_hash: str
    correlation_id: str
    attempt: int
    issued_at: str | None = None


@dataclass(frozen=True)
class SubmitReceipt:
    """受理済みsubmitの記録。同一内容の再送を冪等に扱う判定材料。"""

    action_id: str
    nonce: str
    outcome: str
    submit_hash: str
    result_hash: str
    result_kind: RecordKind | None = None
    error_category: ErrorCategory | None = None
    accepted_at: str | None = None


def _section_of(payload: Mapping[str, object]) -> dict[str, object] | SectionUnavailable | None:
    section = payload.get(_SECTION)
    if section is None:
        return None
    if not isinstance(section, dict):
        return SectionUnavailable(detail="host_actionがobjectでない")
    return section


def _text(source: Mapping[str, object], name: str) -> str | None:
    value = source.get(name)
    return value if isinstance(value, str) and value else None


def read_pending_action(
    payload: Mapping[str, object],
) -> PendingAction | SectionUnavailable | None:
    """未完了actionを読む（無ければNone）。

    `correlation_id` / `attempt`はv2で追加したoptional fieldで、**不在は「単一attemptの
    logical action」を意味する**（v1から移行したcheckpointにだけ現れる）。この既定は
    schemaの意味論であり、壊れた値の埋め合わせではない。
    """
    section = _section_of(payload)
    if section is None or isinstance(section, SectionUnavailable):
        return section
    pending = section.get("pending")
    if pending is None:
        return None
    if not isinstance(pending, dict):
        return SectionUnavailable(detail="host_action.pendingがobjectでない")
    values: dict[str, str] = {}
    for name in (
        "action_id",
        "action_kind",
        "nonce",
        "expected_head_sha",
        "result_path",
        "envelope_path",
        "envelope_hash",
    ):
        value = _text(pending, name)
        if value is None:
            return SectionUnavailable(detail=f"host_action.pendingの{name}が欠如または不正")
        values[name] = value
    attempt = pending.get("attempt", 1)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        return SectionUnavailable(detail="host_action.pendingのattemptが1以上の整数でない")
    return PendingAction(
        action_id=values["action_id"],
        action_kind=values["action_kind"],
        nonce=values["nonce"],
        expected_head_sha=values["expected_head_sha"],
        result_path=values["result_path"],
        envelope_path=values["envelope_path"],
        envelope_hash=values["envelope_hash"],
        correlation_id=_text(pending, "correlation_id") or values["action_id"],
        attempt=attempt,
        issued_at=_text(pending, "issued_at"),
    )


def _receipt_of(entry: object) -> SubmitReceipt | SectionUnavailable:
    if not isinstance(entry, dict):
        return SectionUnavailable(detail="host_action.receiptsの要素がobjectでない")
    values: dict[str, str] = {}
    for name in ("action_id", "nonce", "outcome", "submit_hash", "result_hash"):
        value = _text(entry, name)
        if value is None:
            return SectionUnavailable(detail=f"host_action.receiptsの{name}が欠如または不正")
        values[name] = value
    kind_value = _text(entry, "result_kind")
    category_value = _text(entry, "error_category")
    try:
        kind = RecordKind(kind_value) if kind_value is not None else None
        category = ErrorCategory(category_value) if category_value is not None else None
    except ValueError:
        return SectionUnavailable(detail="host_action.receiptsに未知のresult_kind / error_category")
    return SubmitReceipt(
        action_id=values["action_id"],
        nonce=values["nonce"],
        outcome=values["outcome"],
        submit_hash=values["submit_hash"],
        result_hash=values["result_hash"],
        result_kind=kind,
        error_category=category,
        accepted_at=_text(entry, "accepted_at"),
    )


def read_receipts(
    payload: Mapping[str, object],
) -> tuple[SubmitReceipt, ...] | SectionUnavailable:
    """受理済みsubmitのledgerを読む（無ければ空）。"""
    section = _section_of(payload)
    if isinstance(section, SectionUnavailable):
        return section
    if section is None:
        return ()
    entries = section.get("receipts")
    if entries is None:
        return ()
    if not isinstance(entries, list):
        return SectionUnavailable(detail="host_action.receiptsがlistでない")
    receipts: list[SubmitReceipt] = []
    for entry in entries:
        receipt = _receipt_of(entry)
        if isinstance(receipt, SectionUnavailable):
            return receipt
        receipts.append(receipt)
    return tuple(receipts)


def find_receipt(
    receipts: Sequence[SubmitReceipt], *, action_id: str, nonce: str
) -> SubmitReceipt | None:
    """attemptを一意に指すreceipt（1 action ID + nonce = 1 attempt = 1 receipt）。"""
    for receipt in receipts:
        if receipt.action_id == action_id and receipt.nonce == nonce:
            return receipt
    return None


def read_machine_state(
    payload: Mapping[str, object],
) -> MachineState | SectionUnavailable:
    """`state` sectionからC-01のMachineStateを復元する。

    `MachineState`は組合せ不変条件を構築時に検証するため、保存されていない付随値を
    要するstate（`BLOCKED`のblock context等）は復元できず、その場合は
    `SectionUnavailable`になる。blockはC-06のchain検証で毎回再導出する値であり、
    ここで既定値を埋めると不変条件が壊れる（ADR-0011）。
    """
    section = payload.get("state")
    if section is None:
        return SectionUnavailable(detail="stateが保存されていない")
    if not isinstance(section, dict):
        return SectionUnavailable(detail="stateがobjectでない")
    state_value = _text(section, "state")
    if state_value is None:
        return SectionUnavailable(detail="state.stateが欠如または不正")
    awaiting_value = _text(section, "awaiting")
    return_value = _text(section, "return_to")
    recovery_value = _text(section, "recovery_to")
    pending = section.get("pending_record")
    if pending is not None and not isinstance(pending, dict):
        return SectionUnavailable(detail="state.pending_recordがobjectでない")
    try:
        record = _pending_record_of(pending) if pending is not None else None
        machine_state = MachineState(
            state=State(state_value),
            procedure=_procedure_of(section.get("procedure")),
            awaiting=Awaiting(awaiting_value) if awaiting_value is not None else None,
            pending_record=record,
            deferred_integrity=_violations_of(section.get("deferred_integrity")),
            return_to=State(return_value) if return_value is not None else None,
            recovery_to=State(recovery_value) if recovery_value is not None else None,
            block=_block_of(section.get("block")),
        )
    except (ValueError, TypeError, IllegalMachineStateError) as error:
        return SectionUnavailable(detail=f"stateからMachineStateを復元できない: {error}")
    return machine_state


def _violation_of(entry: object) -> IntegrityEvidenceRef:
    if not isinstance(entry, dict):
        raise TypeError("violationがobjectでない")
    binding = _text(entry, "binding")
    descriptor = _text(entry, "descriptor")
    head = _text(entry, "head")
    if binding is None or descriptor is None or head is None:
        raise ValueError("violationのbinding / descriptor / headが欠如または不正")
    return IntegrityEvidenceRef(
        binding=OpaqueBinding(binding), descriptor=OpaqueRef(descriptor), head=OpaqueRef(head)
    )


def _violations_of(section: object) -> tuple[IntegrityEvidenceRef, ...]:
    if section is None:
        return ()
    if not isinstance(section, list):
        raise TypeError("violation列がlistでない")
    return tuple(_violation_of(entry) for entry in section)


def _procedure_of(section: object) -> Procedure:
    """`procedure`を復元する（未対応の種別はerrorにして`NORMAL`へ丸めない）。"""
    if section is None:
        return NORMAL
    if not isinstance(section, dict):
        raise TypeError("state.procedureがobjectでない")
    kind = _text(section, "kind")
    if kind == ProcedureKind.NORMAL.value:
        return NORMAL
    if kind == ProcedureKind.HALTING_FOR_BLOCK.value:
        attempt = _text(section, "attempt_binding")
        if attempt is None:
            raise ValueError("halt gateのattempt_bindingが欠如している")
        return HaltingForBlockProcedure(
            block=RecordIntegrityBlock(violations=_violations_of(section.get("violations"))),
            attempt_binding=OpaqueBinding(attempt),
        )
    # CANCELLING / RECORDING_INCIDENTの保存はこれらを発行するPhaseが追加する（fail closed）
    raise ValueError(f"未対応のprocedure種別: {kind}")


def _block_of(section: object) -> BlockContext | None:
    """`block`を復元する（RECORD_INTEGRITY以外はそれを発行するPhaseが追加する）。"""
    if section is None:
        return None
    if not isinstance(section, dict):
        raise TypeError("state.blockがobjectでない")
    kind = _text(section, "kind")
    if kind == BlockKind.RECORD_INTEGRITY.value:
        return RecordIntegrityBlock(violations=_violations_of(section.get("violations")))
    raise ValueError(f"未対応のblock種別: {kind}")


def _pending_record_of(section: Mapping[str, object]) -> PendingRecord:
    """`state.pending_record`を復元する（値の不正はValueErrorで呼び出し側へ返す）。"""
    kind = _text(section, "kind")
    binding = _text(section, "binding")
    source = _text(section, "source_state")
    if kind is None or binding is None or source is None:
        raise ValueError("pending_recordのkind / binding / source_stateが欠如または不正")
    return PendingRecord(
        kind=RecordKind(kind), binding=OpaqueBinding(binding), source_state=State(source)
    )


def with_machine_state(
    payload: Mapping[str, object], machine_state: MachineState
) -> dict[str, object]:
    """`state` sectionへMachineStateを書く（section内の他fieldは保つ）。"""
    updated = dict(payload)
    section = updated.get("state")
    values: dict[str, object] = dict(section) if isinstance(section, dict) else {}
    # 次の状態に無い付随値は**必ず消す**。残すと前の状態の値が混ざり、round-trip検証が
    # 正当な遷移まで拒否する（halt完了・block解消・deferred消費で消えるfieldがある）
    for name in (
        "awaiting",
        "return_to",
        "recovery_to",
        "pending_record",
        "procedure",
        "block",
        "deferred_integrity",
    ):
        values.pop(name, None)
    values["state"] = machine_state.state.value
    if machine_state.awaiting is not None:
        values["awaiting"] = machine_state.awaiting.value
    if machine_state.return_to is not None:
        values["return_to"] = machine_state.return_to.value
    if machine_state.recovery_to is not None:
        values["recovery_to"] = machine_state.recovery_to.value
    if machine_state.pending_record is not None:
        record = machine_state.pending_record
        values["pending_record"] = {
            "kind": record.kind.value,
            "binding": record.binding.value,
            "source_state": record.source_state.value,
        }
    if isinstance(machine_state.procedure, HaltingForBlockProcedure):
        values["procedure"] = {
            "kind": ProcedureKind.HALTING_FOR_BLOCK.value,
            "attempt_binding": machine_state.procedure.attempt_binding.value,
            "violations": _violation_entries(machine_state.procedure.block.violations),
        }
    if isinstance(machine_state.block, RecordIntegrityBlock):
        values["block"] = {
            "kind": BlockKind.RECORD_INTEGRITY.value,
            "violations": _violation_entries(machine_state.block.violations),
        }
    if machine_state.deferred_integrity:
        values["deferred_integrity"] = _violation_entries(machine_state.deferred_integrity)
    updated["state"] = values
    return updated


def _violation_entries(
    violations: Sequence[IntegrityEvidenceRef],
) -> list[dict[str, object]]:
    return [
        {
            "binding": violation.binding.value,
            "descriptor": violation.descriptor.value,
            "head": violation.head.value,
        }
        for violation in violations
    ]


def with_verified_machine_state(
    payload: Mapping[str, object], machine_state: MachineState
) -> dict[str, object] | SectionUnavailable:
    """**そのまま読み戻せることを確認してから**stateを書いたpayloadを返す。

    C-01が返す状態には、まだcheckpointが表現できない付随値（`CancellingProcedure`や
    `ProgressBlock`等）を持つものがある。それを黙って落として保存すると、次のresumeが
    復元できないcheckpointになる。書く前に往復させ、一致しなければ**保存しない**
    （ADR-0017。表現できる範囲は、それを発行するPhaseがadditiveに広げる）。
    """
    updated = with_machine_state(payload, machine_state)
    restored = read_machine_state(updated)
    if isinstance(restored, SectionUnavailable):
        return restored
    if restored != machine_state:
        return SectionUnavailable(
            detail="MachineStateをそのまま読み戻せない（checkpointが表現しない付随値がある）"
        )
    return updated


def _with_section(payload: Mapping[str, object], section: dict[str, object]) -> dict[str, object]:
    updated = dict(payload)
    if section:
        updated[_SECTION] = section
    else:
        updated.pop(_SECTION, None)
    return updated


def _current_section(payload: Mapping[str, object]) -> dict[str, object]:
    section = payload.get(_SECTION)
    return dict(section) if isinstance(section, dict) else {}


def _pending_section(action: PendingAction) -> dict[str, object]:
    pending: dict[str, object] = {
        "action_id": action.action_id,
        "action_kind": action.action_kind,
        "nonce": action.nonce,
        "expected_head_sha": action.expected_head_sha,
        "result_path": action.result_path,
        "envelope_path": action.envelope_path,
        "envelope_hash": action.envelope_hash,
        "correlation_id": action.correlation_id,
        "attempt": action.attempt,
    }
    if action.issued_at is not None:
        pending["issued_at"] = action.issued_at
    return pending


def with_retry_attempt(payload: Mapping[str, object], action: PendingAction) -> dict[str, object]:
    """同じlogical actionの次のattemptを置く（**receipt ledgerを保つ**）。

    過去attemptのreceiptを残すことで、遅れて届いた同一submitを冪等に扱える。
    """
    section = _current_section(payload)
    section["pending"] = _pending_section(action)
    return _with_section(payload, section)


def with_new_logical_action(payload: Mapping[str, object], action: PendingAction) -> dict[str, object]:
    """新しいlogical actionを置く（**receipt ledgerを入れ替える**）。

    ledgerはlogical action 1件分だけを保持し、attempt数（retry budget）で有界にする
    （ADR-0015 決定22）。前のlogical actionのreceiptを持ち越すとrun全体で単調に増え、
    checkpointのsize上限へ向かって伸びる。

    入れ替えの代償は、**前のlogical actionへの遅れた再送がstaleになる**ことである。
    その時点でそのactionの結果は永続化済みで、workflowは次の作業へ進んでいるため、
    「同一submitの再送は冪等」の適用範囲外として扱う。
    """
    section = _current_section(payload)
    section.pop("receipts", None)
    section["pending"] = _pending_section(action)
    return _with_section(payload, section)


def without_pending_action(payload: Mapping[str, object]) -> dict[str, object]:
    """未完了actionを外す（actionが完了したとき。receiptは残す）。"""
    section = _current_section(payload)
    section.pop("pending", None)
    return _with_section(payload, section)


def with_receipt(payload: Mapping[str, object], receipt: SubmitReceipt) -> dict[str, object]:
    """receiptをledgerへ追加する（同じattemptを二重に積まない）。"""
    section = _current_section(payload)
    entries = section.get("receipts")
    ledger: list[object] = list(entries) if isinstance(entries, list) else []
    entry: dict[str, object] = {
        "action_id": receipt.action_id,
        "nonce": receipt.nonce,
        "outcome": receipt.outcome,
        "submit_hash": receipt.submit_hash,
        "result_hash": receipt.result_hash,
    }
    if receipt.result_kind is not None:
        entry["result_kind"] = receipt.result_kind.value
    if receipt.error_category is not None:
        entry["error_category"] = receipt.error_category.value
    if receipt.accepted_at is not None:
        entry["accepted_at"] = receipt.accepted_at
    ledger.append(entry)
    section["receipts"] = ledger
    return _with_section(payload, section)


def next_attempt(action: PendingAction, *, action_id: str, nonce: str, result_path: str,
                 envelope_path: str, envelope_hash: str, issued_at: str) -> PendingAction:
    """同じlogical actionの次のattempt（新しいaction IDとnonceを持つ）。"""
    return replace(
        action,
        action_id=action_id,
        nonce=nonce,
        result_path=result_path,
        envelope_path=envelope_path,
        envelope_hash=envelope_hash,
        attempt=action.attempt + 1,
        issued_at=issued_at,
    )


# ---------------------------------------------------------------------------
# `user_request` section（Phase 8 PR-2c。ADR-0018）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingUserRequest:
    """未完了の`AWAIT_USER` request（1 request = 1 nonce = 1 応答）。"""

    request_id: str
    awaiting: Awaiting
    nonce: str
    expected_head_sha: str
    result_path: str
    envelope_path: str
    envelope_hash: str
    since_seq: int
    issued_at: str | None = None


@dataclass(frozen=True)
class UserRequestReceipt:
    """受理済みuser-input submitの記録。判定は`submit_hash`で行う。"""

    request_id: str
    nonce: str
    submit_hash: str
    result_hash: str
    # recordを作らない応答（permission resume）はintentの台帳に載らないためNone
    intent_key: str | None = None
    result_kind: RecordKind | None = None
    accepted_at: str | None = None


@dataclass(frozen=True)
class ConsumedIntent:
    """当該awaiting instanceで消費済みのuser intent（2経路の重複防止台帳）。"""

    intent_key: str
    binding: str
    route: str


def _user_section(payload: Mapping[str, object]) -> dict[str, object] | SectionUnavailable | None:
    section = payload.get(_USER_SECTION)
    if section is None:
        return None
    if not isinstance(section, dict):
        return SectionUnavailable(detail="user_requestがobjectでない")
    return section


def _entry_of(
    section: dict[str, object] | SectionUnavailable | None, name: str
) -> dict[str, object] | SectionUnavailable | None:
    if section is None or isinstance(section, SectionUnavailable):
        return section
    entry = section.get(name)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        return SectionUnavailable(detail=f"user_request.{name}がobjectでない")
    return entry


def _texts(
    entry: Mapping[str, object], names: tuple[str, ...], *, where: str
) -> dict[str, str] | SectionUnavailable:
    values: dict[str, str] = {}
    for name in names:
        value = _text(entry, name)
        if value is None:
            return SectionUnavailable(detail=f"{where}の{name}が欠如または不正")
        values[name] = value
    return values


_PENDING_USER_KEYS = (
    "request_id",
    "awaiting",
    "nonce",
    "expected_head_sha",
    "result_path",
    "envelope_path",
    "envelope_hash",
)


def read_user_request(
    payload: Mapping[str, object],
) -> PendingUserRequest | SectionUnavailable | None:
    """未完了のuser requestを読む（無ければNone）。"""
    entry = _entry_of(_user_section(payload), "pending")
    if entry is None or isinstance(entry, SectionUnavailable):
        return entry
    values = _texts(entry, _PENDING_USER_KEYS, where="user_request.pending")
    if isinstance(values, SectionUnavailable):
        return values
    since = entry.get("since_seq")
    if isinstance(since, bool) or not isinstance(since, int) or since < 0:
        return SectionUnavailable(detail="user_request.pendingのsince_seqが0以上の整数でない")
    try:
        awaiting = Awaiting(values["awaiting"])
    except ValueError:  # pragma: no cover - schemaのenumが値域を限定している
        return SectionUnavailable(detail="user_request.pendingに未知のawaiting")
    return PendingUserRequest(
        request_id=values["request_id"],
        awaiting=awaiting,
        nonce=values["nonce"],
        expected_head_sha=values["expected_head_sha"],
        result_path=values["result_path"],
        envelope_path=values["envelope_path"],
        envelope_hash=values["envelope_hash"],
        since_seq=since,
        issued_at=_text(entry, "issued_at"),
    )


def read_user_receipt(
    payload: Mapping[str, object],
) -> UserRequestReceipt | SectionUnavailable | None:
    """受理済み応答を読む（無ければNone）。

    `receipts`ではなく単数なのは構造的な帰結である: engineはユーザー入力へretry attemptを
    発行せず（人間の入力にbudgetを課さない）、1 instanceの応答は1件しかない。
    """
    entry = _entry_of(_user_section(payload), "receipt")
    if entry is None or isinstance(entry, SectionUnavailable):
        return entry
    values = _texts(
        entry, ("request_id", "nonce", "submit_hash", "result_hash"), where="user_request.receipt"
    )
    if isinstance(values, SectionUnavailable):
        return values
    kind_value = _text(entry, "result_kind")
    try:
        kind = RecordKind(kind_value) if kind_value is not None else None
    except ValueError:  # pragma: no cover - schemaのenumが値域を限定している
        return SectionUnavailable(detail="user_request.receiptに未知のresult_kind")
    return UserRequestReceipt(
        request_id=values["request_id"],
        nonce=values["nonce"],
        submit_hash=values["submit_hash"],
        result_hash=values["result_hash"],
        intent_key=_text(entry, "intent_key"),
        result_kind=kind,
        accepted_at=_text(entry, "accepted_at"),
    )


def read_consumed_intent(
    payload: Mapping[str, object],
) -> ConsumedIntent | SectionUnavailable | None:
    """当該instanceで消費済みのintentを読む（無ければNone）。"""
    entry = _entry_of(_user_section(payload), "consumed")
    if entry is None or isinstance(entry, SectionUnavailable):
        return entry
    values = _texts(entry, ("intent_key", "binding", "route"), where="user_request.consumed")
    if isinstance(values, SectionUnavailable):
        return values
    return ConsumedIntent(
        intent_key=values["intent_key"], binding=values["binding"], route=values["route"]
    )


@dataclass(frozen=True)
class UserRequestState:
    """`user_request` sectionの読み出し結果（3 entryを1回で読む）。

    entryごとに呼び出し側で直和を捌くと、**同じ「解釈できない」を3箇所で分岐**することに
    なる。sectionは1つの単位として読み、失敗を1点へ集約する。
    """

    pending: PendingUserRequest | None
    receipt: UserRequestReceipt | None
    consumed: ConsumedIntent | None


def read_user_section(payload: Mapping[str, object]) -> UserRequestState | SectionUnavailable:
    """`user_request` sectionをまとめて読む（1つでも解釈できなければ停止させる）。"""
    pending = read_user_request(payload)
    if isinstance(pending, SectionUnavailable):
        return pending
    receipt = read_user_receipt(payload)
    if isinstance(receipt, SectionUnavailable):
        return receipt
    consumed = read_consumed_intent(payload)
    if isinstance(consumed, SectionUnavailable):
        return consumed
    return UserRequestState(pending=pending, receipt=receipt, consumed=consumed)


def _with_user_section(
    payload: Mapping[str, object], section: dict[str, object]
) -> dict[str, object]:
    updated = dict(payload)
    if section:
        updated[_USER_SECTION] = section
    else:
        updated.pop(_USER_SECTION, None)
    return updated


def _current_user_section(payload: Mapping[str, object]) -> dict[str, object]:
    section = payload.get(_USER_SECTION)
    return dict(section) if isinstance(section, dict) else {}


def with_user_request(
    payload: Mapping[str, object], request: PendingUserRequest
) -> dict[str, object]:
    """新しいrequestを置く（**section全体を入れ替える**）。

    前instanceの応答と消費済みintentは持ち越さない。重複防止keyは`since_seq`を含むため
    instanceを跨いで一致せず、残しても判定に使えないまま伸びるだけである。
    """
    pending: dict[str, object] = {
        "request_id": request.request_id,
        "awaiting": request.awaiting.value,
        "nonce": request.nonce,
        "expected_head_sha": request.expected_head_sha,
        "result_path": request.result_path,
        "envelope_path": request.envelope_path,
        "envelope_hash": request.envelope_hash,
        "since_seq": request.since_seq,
    }
    if request.issued_at is not None:
        pending["issued_at"] = request.issued_at
    return _with_user_section(payload, {"pending": pending})


def without_user_request(payload: Mapping[str, object]) -> dict[str, object]:
    """未完了requestを外す（応答を受理したとき。receiptと消費済みintentは残す）。"""
    section = _current_user_section(payload)
    section.pop("pending", None)
    return _with_user_section(payload, section)


def with_user_receipt(
    payload: Mapping[str, object], receipt: UserRequestReceipt
) -> dict[str, object]:
    """受理済み応答を置く（同一instanceの応答は1件）。"""
    section = _current_user_section(payload)
    entry: dict[str, object] = {
        "request_id": receipt.request_id,
        "nonce": receipt.nonce,
        "submit_hash": receipt.submit_hash,
        "result_hash": receipt.result_hash,
    }
    if receipt.intent_key is not None:
        entry["intent_key"] = receipt.intent_key
    if receipt.result_kind is not None:
        entry["result_kind"] = receipt.result_kind.value
    if receipt.accepted_at is not None:
        entry["accepted_at"] = receipt.accepted_at
    section["receipt"] = entry
    return _with_user_section(payload, section)


def with_consumed_intent(
    payload: Mapping[str, object], consumed: ConsumedIntent
) -> dict[str, object]:
    """消費済みintentを記録する（2経路が同じkeyを書き、二重recordを作らない）。

    転記経路（C-08）は応答を受理した時点で、GitHub直接comment経路（C-13）は
    `accept_user_decision`の受理時点で、それぞれ同じ`intent_key`をここへ書く。
    """
    section = _current_user_section(payload)
    section["consumed"] = {
        "intent_key": consumed.intent_key,
        "binding": consumed.binding,
        "route": consumed.route,
    }
    return _with_user_section(payload, section)
