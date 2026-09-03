# SPDX-License-Identifier: Apache-2.0
"""`RecordIntegrityIncident`の実行（C-08。Phase 8 PR-3d。ADR-0024）。

C-01は検出済みviolationを`deferred_integrity`へ集合として保持し、**全violationが検証済みの
incident recordへ含まれた後にのみ**terminalへ進む（AC-C01-12）。本moduleはそのrecordを1件
作るところまでを担う。

```
advance -> IncidentRequired -> record_incident -> RecordProduced（I-P）
        -> pending + PersistRecord -> persist -> IntegrityIncidentVerified
        -> I-VC（terminalへ）/ I-VR（残余で再実行）
```

- **記録対象はC-01の状態から決まる**。`deferred_integrity`と`procedure.audit`で、
  `_reissue_incident_request`（resume経路）が行う導出と同一である
- **payloadはportが供給し、engineが照合する**。内容の構成はC-06の責務だが、記録範囲が
  C-01の指示と違うrecordを投稿するとcoverage判定が意図しない値になるため、engineが
  `violation_bindings`と`audit_reference`の完全一致を要求する
- **chainのviolationで拒否しない**。incident recordはまさにchainが壊れているときに作る
  recordで、それを「chainが壊れている」という理由で作れないのは循環である（決定1）
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..domain.commands import Command
from ..domain.values import (
    MachineState,
    PendingRecord,
    RecordingIncidentProcedure,
    RecordKind,
)
from ..schema.records import INTEGRITY_INCIDENT
from ..schema.registry import validate_object
from ..state import StatePaths, checkpoint_path, save_checkpoint
from ..transport.render import prepare_controller_body
from .checkpoint_view import with_machine_state
from .ports import IncidentContext, IncidentPayloadPort, RecordBodyPort, RecordSourcePort
from .run_context import EngineStopped, RunContext, load_run
from .transaction import IssuedTransaction, ProduceRejected, produce_record, transaction_section

AUDIT_FIELD = "audit_reference"
BINDINGS_FIELD = "violation_bindings"


@dataclass(frozen=True)
class IncidentRecorded:
    """incident recordを1件作った（投稿はこの後の`persist`が行う）。"""

    transaction: IssuedTransaction
    machine_state: MachineState
    commands: tuple[Command, ...]


IncidentOutcome = IncidentRecorded | EngineStopped


def audit_reference(audit: PendingRecord | None) -> dict[str, str] | None:
    """監査参照のpayload表現（cancelで未完了になったturnのrecord）。"""
    if audit is None:
        return None
    return {"kind": audit.kind.value, "binding": audit.binding.value}


def _mismatch(payload: Mapping[str, object], context: IncidentContext) -> EngineStopped | None:
    """portが返したpayloadがC-01の指示と同じ範囲を指しているか照合する。

    ここを緩めると、記録範囲が指示と違うrecordを投稿でき、coverage判定
    （COMPLETE / REMAINDER）が意図しない値になる。**推測せず停止する**。
    """
    expected = [binding.value for binding in context.violation_bindings]
    recorded = payload.get(BINDINGS_FIELD)
    if not isinstance(recorded, list) or recorded != expected:
        return EngineStopped(
            "incident_bindings_mismatch", "incident payloadの記録対象がC-01の指示と一致しない"
        )
    if payload.get(AUDIT_FIELD) != audit_reference(context.audit):
        return EngineStopped(
            "incident_audit_mismatch", "incident payloadの監査参照がC-01の指示と一致しない"
        )
    return None


def record_incident(
    *,
    paths: StatePaths,
    run_id: str,
    repository: str,
    number: int,
    head_sha: str,
    incident_port: IncidentPayloadPort,
    body_port: RecordBodyPort,
    records_port: RecordSourcePort,
    speaker: str,
) -> IncidentOutcome:
    """`RecordIntegrityIncident`を実行し、投稿待ちのincident recordを1件作る。"""
    run = load_run(paths, run_id=run_id, repository=repository, number=number)
    if isinstance(run, EngineStopped):
        return run
    procedure = run.machine_state.procedure
    if not isinstance(procedure, RecordingIncidentProcedure):
        return EngineStopped("not_recording_incident", "incident記録中のrunではない")
    # 記録対象が空にならないことはC-01の`INCIDENT_NEEDS_DEFERRED`不変条件が保証する。
    # 仮に空でも`_rule_violations_not_empty`が空recordを拒否するため、二重の検査は置かない
    bindings = tuple(ref.binding for ref in run.machine_state.deferred_integrity)
    context = IncidentContext(
        violation_bindings=bindings,
        audit=procedure.audit,
        run_id=run_id,
        repository=repository,
        number=number,
        head_sha=head_sha,
    )
    payload = dict(incident_port.payload_for(context))
    validation = validate_object(INTEGRITY_INCIDENT, payload)
    if not validation.ok:
        codes = ",".join(sorted(error.code for error in validation.errors))
        return EngineStopped("incident_payload_invalid", f"incident payloadが検証を通らない（{codes}）")
    mismatch = _mismatch(payload, context)
    if mismatch is not None:
        return mismatch
    return _produce(
        run,
        payload,
        paths=paths,
        run_id=run_id,
        head_sha=head_sha,
        body_port=body_port,
        records_port=records_port,
        speaker=speaker,
    )


def _produce(
    run: RunContext,
    payload: Mapping[str, object],
    *,
    paths: StatePaths,
    run_id: str,
    head_sha: str,
    body_port: RecordBodyPort,
    records_port: RecordSourcePort,
    speaker: str,
) -> IncidentOutcome:
    """採番から`RecordProduced`までを通し、transactionをcheckpointへ置く。"""
    prepared = prepare_controller_body(
        body_port.body_for(RecordKind.INTEGRITY_INCIDENT, payload), speaker=speaker
    )
    produced = produce_record(
        run.machine_state,
        kind=RecordKind.INTEGRITY_INCIDENT,
        payload=payload,
        run_id=run_id,
        head_sha=head_sha,
        body=prepared.text,
        chain=records_port.chain(run_id),
    )
    if isinstance(produced, ProduceRejected):
        return EngineStopped(produced.code, produced.detail)
    updated = with_machine_state(run.payload, produced.machine_state)
    updated["transaction"] = transaction_section(produced.transaction)
    save_checkpoint(checkpoint_path(paths, run_id), updated)
    return IncidentRecorded(
        transaction=produced.transaction,
        machine_state=produced.machine_state,
        commands=produced.commands,
    )


__all__ = [
    "IncidentOutcome",
    "IncidentRecorded",
    "audit_reference",
    "record_incident",
]
