# SPDX-License-Identifier: Apache-2.0
"""`RecordIntegrityIncident`の実行の受入test（Phase 8 PR-3d。ADR-0024）。

incident recordは**まさにchainが壊れているときに投稿するrecord**である。violationは
`verify_record_chain`がliveのGitHubから毎回再導出するため、壊れたchainは壊れたままで、
`is_intact`だけでgateするとincident recordは永久に投稿できずrunがterminalへ到達しない。

そこで許すのは次の3条件がすべて揃う場合**だけ**である（決定1）。

1. 記録しようとしているのが`INTEGRITY_INCIDENT` recordであること
2. runが`RecordingIncidentProcedure`にいること
3. chainのviolationが**すべて`deferred_integrity`に入っている**こと

このfileはまずその判定を固定し（既定の挙動が1 bitも変わらないことを含む）、次に
executorとend-to-endを見る。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest
from c05_support.helpers import read_state
from c07_support.helpers import verified_chain
from c08_support.helpers import (
    HEAD,
    ISSUED_AT,
    NUMBER,
    REPOSITORY,
    RUN,
    FakeBodyPort,
    FakeIds,
    FakeRecordEvents,
    FakeRecordSource,
    persist_env,
)
from c08_support.runtime import round_ports, runtime_env

from claude_code_codex_review_loop.domain.values import (
    IllegalMachineStateError,
    IncidentTarget,
    IntegrityEvidenceRef,
    MachineState,
    OpaqueBinding,
    OpaqueRef,
    PendingRecord,
    RecordingIncidentProcedure,
    RecordKind,
    State,
)
from claude_code_codex_review_loop.runtime import default_ports, step
from claude_code_codex_review_loop.state import (
    CheckpointLoaded,
    CheckpointStoreError,
    checkpoint_path,
    load_checkpoint,
    save_checkpoint,
)
from claude_code_codex_review_loop.workflow import (
    EngineStopped,
    IncidentContext,
    IncidentRecorded,
    IncidentRequired,
    IntegrityDetected,
    RecordPersisted,
    SectionUnavailable,
    Terminal,
    advance,
    audit_reference,
    persist,
    read_machine_state,
    read_recorded_violations,
    record_incident,
    with_machine_state,
    with_recorded_violations,
)

INCIDENT_BINDING = "iv:tamper:run-1:c1"


def _payload(env: Any) -> dict[str, object]:
    loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
    assert isinstance(loaded, CheckpointLoaded)
    return loaded.payload


def violation(name: str = INCIDENT_BINDING) -> IntegrityEvidenceRef:
    return IntegrityEvidenceRef(
        binding=OpaqueBinding(name), descriptor=OpaqueRef("desc"), head=OpaqueRef(HEAD)
    )


def incident_payload(*bindings: str) -> dict[str, object]:
    """代表的なINTEGRITY_INCIDENT payload（記録対象のbindingだけ差し替える）。"""
    return {
        "schema_version": 1,
        "violation_bindings": list(bindings or (INCIDENT_BINDING,)),
        "summary": "canonical commentの改変を検出しました。",
    }


def _incident_env(tmp_path: Any, *, deferred: tuple[IntegrityEvidenceRef, ...], **overrides: Any):
    """incident recordの永続化を待っているrun（`RecordingIncidentProcedure`）。"""
    env = persist_env(
        tmp_path,
        kind=RecordKind.INTEGRITY_INCIDENT,
        payload=incident_payload(*[ref.binding.value for ref in deferred]),
        state=MachineState(state=State.MERGING),
        **overrides,
    )
    # `persist_env`はtransactionを先に発行するので、bindingが決まってからstateを組む
    state = MachineState(
        state=State.MERGING,
        procedure=RecordingIncidentProcedure(target=IncidentTarget.CANCELLED, audit=None),
        pending_record=PendingRecord(
            kind=RecordKind.INTEGRITY_INCIDENT,
            binding=OpaqueBinding(env.issued.binding),
            source_state=State.MERGING,
        ),
        deferred_integrity=deferred,
    )
    save_checkpoint(checkpoint_path(env.paths, RUN), with_machine_state(_payload(env), state))
    return env


class _StillBroken:
    """実chainを読み、**同じviolationが残り続ける**様子を再現するport。

    violationは`verify_record_chain`がliveのGitHubから毎回再導出するため、記録しても
    消えない。投稿後の再検証でも同じ集合が返る、という現実の形をそのまま置く。
    """

    def __init__(self, env: Any, violations: tuple[IntegrityEvidenceRef, ...]) -> None:
        self._real = env.kwargs()["records_port"]
        self._violations = violations

    def chain(self, run_id: str) -> Any:
        chain = self._real.chain(run_id)
        return replace(chain, violations=self._violations)


class TestKnownViolationGate:
    """既知violationの上でだけ投稿を許す（決定1）。"""

    def test_a_known_violation_does_not_stop_the_incident_record(self, tmp_path: Any) -> None:
        """記録対象として既にC-01が受理しているviolationは、投稿を妨げない。

        これが通らないとincident recordは永久に投稿できず、runはterminalへ到達しない。
        """
        known = violation()
        env = _incident_env(tmp_path, deferred=(known,))
        before = env.comment_count()
        outcome = persist(
            **env.kwargs(
                records_port=_StillBroken(env, (known,)),
                event_port=FakeRecordEvents(recorded_bindings=(known.binding,)),
            )
        )
        assert isinstance(outcome, RecordPersisted), outcome
        assert env.comment_count() == before + 1
        # 記録が済んだのでincident手続きを抜け、targetのterminalへ進む（I-VC）
        assert outcome.machine_state.state is State.CANCELLED

    def test_an_unknown_violation_is_detected_first(self, tmp_path: Any) -> None:
        """新しいviolationは従来どおり検出が優先される（記録漏れは行き止まりより悪い）。"""
        known = violation()
        env = _incident_env(tmp_path, deferred=(known,))
        broken = FakeRecordSource(
            records=verified_chain([RecordKind.REVIEW_RESULT]).records,
            violations=(known, violation("iv:gap:run-1:9")),
        )
        outcome = persist(**env.kwargs(records_port=broken))
        assert isinstance(outcome, IntegrityDetected), outcome
        # 検出へ回すのは**未知のものだけ**（既知はC-01が受理済みでunionは冪等）
        assert outcome.violations == (violation("iv:gap:run-1:9"),)
        assert env.comment_count() == 1  # 投稿していない


class TestTheDefaultGateIsUnchanged:
    """incident record以外は1 bitも緩めない（壊れたchainの上へ通常のrecordを積まない）。"""

    def test_an_ordinary_record_still_refuses_a_broken_chain(self, tmp_path: Any) -> None:
        env = persist_env(tmp_path)
        broken = FakeRecordSource(
            records=verified_chain([RecordKind.REVIEW_RESULT]).records, violations=(violation(),)
        )
        outcome = persist(**env.kwargs(records_port=broken))
        assert isinstance(outcome, IntegrityDetected)
        assert outcome.violations == (violation(),)

    def test_c01_confines_the_incident_pending_to_the_procedure(self) -> None:
        """緩和の条件が`kind`だけで足りる根拠を固定する。

        `_unknown_violations`は「pendingが`INTEGRITY_INCIDENT`か」しか見ない。それで
        足りるのは、C-01の`INCIDENT_PENDING_SCOPE`不変条件が`MachineState`の構築時点で
        「`INTEGRITY_INCIDENT`のpendingはincident記録中に限る」を強制しているためである。
        **この依存が崩れたらここでfailする**（緩和の前提が変わったことに気付ける）。
        """
        with pytest.raises(IllegalMachineStateError, match="INCIDENT_PENDING_SCOPE"):
            MachineState(
                state=State.MERGING,
                pending_record=PendingRecord(
                    kind=RecordKind.INTEGRITY_INCIDENT,
                    binding=OpaqueBinding("cr:run-1:2:x"),
                    source_state=State.MERGING,
                ),
            )


class FakeIncidentPayloads:
    """incident recordの内容を構成するport（本実装はC-06）。

    既定は「C-01の指示どおり」を返す。`override`を渡すと違う内容を返せるので、engine側の
    照合（記録範囲がC-01の指示と一致すること）を観測できる。
    """

    def __init__(self, override: Mapping[str, object] | None = None) -> None:
        self.override = override
        self.calls: list[IncidentContext] = []

    def payload_for(self, context: IncidentContext) -> Mapping[str, object]:
        self.calls.append(context)
        if self.override is not None:
            return self.override
        payload: dict[str, object] = {
            "schema_version": 1,
            "violation_bindings": [binding.value for binding in context.violation_bindings],
            "summary": "canonical recordの改変を検出しました。",
        }
        reference = audit_reference(context.audit)
        if reference is not None:
            payload["audit_reference"] = reference
        return payload


def _recording_state(
    *,
    deferred: tuple[IntegrityEvidenceRef, ...],
    target: IncidentTarget = IncidentTarget.CANCELLED,
    audit: PendingRecord | None = None,
    state: State = State.MERGING,
) -> MachineState:
    """incident recordをまだ作っていないrun（`RecordIntegrityIncident`が発行された直後）。"""
    return MachineState(
        state=state,
        procedure=RecordingIncidentProcedure(target=target, audit=audit),
        deferred_integrity=deferred,
    )


def _runtime(tmp_path: Any, machine_state: MachineState, **kwargs: Any) -> Any:
    return runtime_env(
        tmp_path, state=machine_state, seeded=(RecordKind.REVIEW_RESULT,), **kwargs
    )


def _ports(env: Any, *, incident: Any = None, recorded: tuple[Any, ...] = ()) -> Any:
    return round_ports(
        env,
        incident=incident or FakeIncidentPayloads(),
        events=FakeRecordEvents(recorded_bindings=recorded),
        body=FakeBodyPort(text="整合性incidentを記録しました"),
    )


def _ports_with(
    env: Any, *, records: Any, recorded: tuple[Any, ...] = (), events: Any = None
) -> Any:
    """chainの見え方とrecorded bindingを差し替えた束（end-to-end用）。"""
    return replace(
        _ports(env, recorded=recorded),
        records=records,
        **({"events": events} if events is not None else {}),
    )


def _advance_kwargs(env: Any) -> dict[str, Any]:
    ports = _ports(env)
    return {
        "paths": env.paths,
        "run_id": RUN,
        "repository": REPOSITORY,
        "number": NUMBER,
        "head_sha": HEAD,
        "payload_port": ports.payload,
        "evidence_port": ports.evidence,
        "records_port": ports.records,
        "id_source": FakeIds("inc"),
        "issued_at": ISSUED_AT,
    }


def _incident_kwargs(env: Any, port: Any) -> dict[str, Any]:
    ports = _ports(env, incident=port)
    return {
        "paths": env.paths,
        "run_id": RUN,
        "repository": REPOSITORY,
        "number": NUMBER,
        "head_sha": HEAD,
        "incident_port": ports.incident,
        "body_port": ports.body,
        "records_port": ports.records,
        "speaker": env.config.controller_speaker,
    }


class TestTheExecutor:
    """`advance`が`IncidentRequired`を返し、executorがrecordを1件作る。"""

    def test_advance_asks_for_the_incident_record(self, tmp_path: Any) -> None:
        known = violation()
        env = _runtime(tmp_path, _recording_state(deferred=(known,)))
        outcome = advance(**_advance_kwargs(env))
        assert isinstance(outcome, IncidentRequired), outcome
        # 記録対象はC-01の状態から決まる（`_reissue_incident_request`と同じ導出）
        assert outcome.violation_bindings == (known.binding,)
        assert outcome.audit is None

    def test_the_audit_reference_comes_from_the_procedure(self, tmp_path: Any) -> None:
        """cancelで未完了になったturnの監査参照を持ち越す。"""
        audit = PendingRecord(
            kind=RecordKind.REVIEW_RESULT,
            binding=OpaqueBinding("cr:run-1:1:audit"),
            source_state=State.MERGING,
        )
        env = _runtime(tmp_path, _recording_state(deferred=(violation(),), audit=audit))
        outcome = advance(**_advance_kwargs(env))
        assert isinstance(outcome, IncidentRequired)
        assert outcome.audit == audit

    def test_the_executor_produces_a_pending_record(self, tmp_path: Any) -> None:
        known = violation()
        env = _runtime(tmp_path, _recording_state(deferred=(known,)))
        port = FakeIncidentPayloads()
        outcome = record_incident(**_incident_kwargs(env, port))
        assert isinstance(outcome, IncidentRecorded), outcome
        assert outcome.machine_state.pending_record is not None
        assert outcome.machine_state.pending_record.kind is RecordKind.INTEGRITY_INCIDENT
        # portへはC-01の指示がそのまま渡る
        assert port.calls[0].violation_bindings == (known.binding,)

    def test_c01_keeps_the_deferred_set_non_empty(self) -> None:
        """記録対象が空にならないことをC-08が重ねて検査しない根拠を固定する。

        `INCIDENT_NEEDS_DEFERRED`が`MachineState`の構築時点で強制するため、空のまま
        incident記録中になるrunは存在しない。**この依存が崩れたらここでfailする**。
        """
        with pytest.raises(IllegalMachineStateError, match="INCIDENT_NEEDS_DEFERRED"):
            _recording_state(deferred=())


class TestThePayloadIsVerified:
    """portが返した内容がC-01の指示と同じ範囲を指しているか（決定4）。"""

    def test_a_narrower_record_is_refused(self, tmp_path: Any) -> None:
        """記録範囲を勝手に狭めたpayloadは受理しない（coverage判定が狂う）。"""
        env = _runtime(tmp_path, _recording_state(deferred=(violation("iv:gap:run-1:2"), violation())))
        port = FakeIncidentPayloads(
            {
                "schema_version": 1,
                "violation_bindings": ["iv:gap:run-1:2"],
                "summary": "1件だけ記録します。",
            }
        )
        outcome = record_incident(**_incident_kwargs(env, port))
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "incident_bindings_mismatch"

    def test_a_wrong_audit_reference_is_refused(self, tmp_path: Any) -> None:
        env = _runtime(tmp_path, _recording_state(deferred=(violation(),)))
        port = FakeIncidentPayloads(
            {
                "schema_version": 1,
                "violation_bindings": [INCIDENT_BINDING],
                "summary": "監査参照を勝手に足します。",
                "audit_reference": {"kind": "REVIEW_RESULT", "binding": "cr:run-1:9:x"},
            }
        )
        outcome = record_incident(**_incident_kwargs(env, port))
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "incident_audit_mismatch"

    def test_an_invalid_payload_is_refused(self, tmp_path: Any) -> None:
        env = _runtime(tmp_path, _recording_state(deferred=(violation(),)))
        port = FakeIncidentPayloads({"schema_version": 1, "violation_bindings": []})
        outcome = record_incident(**_incident_kwargs(env, port))
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "incident_payload_invalid"


class _LiveChain:
    """実chainを読み、指定したviolationを毎回足すport。

    violationは`verify_record_chain`がliveのGitHubから毎回再導出するため、incident recordを
    書いても消えない。`add_after`を渡すと、n回目の呼び出し以降で新しいviolationが**追加で**
    見えるようになる（記録中に別のviolationが検出される状況）。
    """

    def __init__(
        self,
        env: Any,
        known: tuple[IntegrityEvidenceRef, ...],
        *,
        late: IntegrityEvidenceRef | None = None,
        add_after: int = 0,
    ) -> None:
        self._real = default_ports(env.paths, env.config).records
        self._known = known
        self._late = late
        self._add_after = add_after
        self.calls = 0

    def chain(self, run_id: str) -> Any:
        self.calls += 1
        violations = self._known
        if self._late is not None and self.calls > self._add_after:
            violations = tuple(sorted(violations + (self._late,), key=lambda ref: ref.binding.value))
        return replace(self._real.chain(run_id), violations=violations)


class _RecordedQueue:
    """incident recordが記録したbindingを順に返すport（構成・検証はC-06）。"""

    def __init__(self, *rounds: tuple[OpaqueBinding, ...]) -> None:
        self._rounds = list(rounds)
        self.calls = 0

    def event_for(self, evidence: Any, record: Any) -> Any:
        self.calls += 1
        recorded = self._rounds[min(self.calls, len(self._rounds)) - 1]
        return FakeRecordEvents(recorded_bindings=recorded).event_for(evidence, record)


def _drive(env: Any, ports: Any) -> Any:
    return step(
        paths=env.paths,
        config=env.config,
        ports=ports,
        id_source=FakeIds("inc"),
        issued_at=ISSUED_AT,
    )


class TestEndToEnd:
    """`RecordIntegrityIncident`の発行からterminal到達までを1本で通す。

    これが通らないと、cancel中またはmerge outcome確定時にviolationを検出したrunは
    完走しない（Phase 8最後の行き止まり）。
    """

    def test_a_cancelled_run_records_its_incident_and_finishes(self, tmp_path: Any) -> None:
        known = violation()
        env = _runtime(tmp_path, _recording_state(deferred=(known,)))
        ports = _ports_with(env, records=_LiveChain(env, (known,)), recorded=(known.binding,))
        result = _drive(env, ports)
        assert isinstance(result.outcome, Terminal), result.outcome
        assert result.outcome.state is State.CANCELLED
        # incident recordを1件作り、1件投稿している
        assert result.trace.incidents == 1
        assert len(result.trace.persisted) == 1

    def test_a_merged_run_records_its_incident_and_finishes(self, tmp_path: Any) -> None:
        """`MERGED`側のtargetでも同じ経路でterminalへ到達する（I-46）。"""
        known = violation()
        env = _runtime(
            tmp_path, _recording_state(deferred=(known,), target=IncidentTarget.MERGED)
        )
        ports = _ports_with(env, records=_LiveChain(env, (known,)), recorded=(known.binding,))
        result = _drive(env, ports)
        assert isinstance(result.outcome, Terminal), result.outcome
        assert result.outcome.state is State.MERGED

    def test_the_incident_reaches_github_as_a_canonical_record(self, tmp_path: Any) -> None:
        known = violation()
        env = _runtime(tmp_path, _recording_state(deferred=(known,)))
        before = len(read_state(env.directory)["comments"])
        ports = _ports_with(env, records=_LiveChain(env, (known,)), recorded=(known.binding,))
        _drive(env, ports)
        assert len(read_state(env.directory)["comments"]) == before + 1


class TestSerialisation:
    """部分記録は次のcycleへ直列化する（I-VR。節5.4）。"""

    def test_a_violation_detected_while_recording_is_carried_to_the_next_cycle(
        self, tmp_path: Any
    ) -> None:
        """記録中に見つかったviolationは、terminalへ進む前に次のincident recordへ入る。

        1周目は`{A}`だけを記録し、その間に`B`が見つかる -> coverageはREMAINDER ->
        残余`{B}`で2周目を回してからterminalへ到達する。**取りこぼしたままterminalへ
        進まない**ことがこのtestの主張である。
        """
        first, late = violation("iv:aaa:run-1:1"), violation("iv:zzz:run-1:2")
        env = _runtime(tmp_path, _recording_state(deferred=(first,)))
        records = _LiveChain(env, (first,), late=late, add_after=2)
        ports = _ports_with(
            env,
            records=records,
            events=_RecordedQueue((first.binding,), (late.binding,)),
        )
        before = len(read_state(env.directory)["comments"])
        result = _drive(env, ports)

        assert isinstance(result.outcome, Terminal), result.outcome
        assert result.outcome.state is State.CANCELLED
        # incident recordは2件作られ、2件ともcanonical recordとして残る（直列化）
        assert result.trace.incidents == 2
        assert len(set(result.trace.persisted)) == 2
        assert len(read_state(env.directory)["comments"]) == before + 2

        loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
        assert isinstance(loaded, CheckpointLoaded)
        # 全violationが記録され、未記録は残っていない（terminalへ進める条件）
        assert read_machine_state(loaded.payload).deferred_integrity == ()
        assert read_recorded_violations(loaded.payload) == (
            first.binding.value,
            late.binding.value,
        )


class TestTheExecutorRefuses:
    """実行できない状況を推測で通さない。"""

    def test_a_run_outside_the_procedure_is_refused(self, tmp_path: Any) -> None:
        env = _runtime(tmp_path, MachineState(state=State.READY_FOR_HUMAN_MERGE))
        outcome = record_incident(**_incident_kwargs(env, FakeIncidentPayloads()))
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "not_recording_incident"

    def test_a_missing_checkpoint_is_reported(self, tmp_path: Any) -> None:
        env = _runtime(tmp_path, _recording_state(deferred=(violation(),)))
        checkpoint_path(env.paths, RUN).unlink()
        outcome = record_incident(**_incident_kwargs(env, FakeIncidentPayloads()))
        assert isinstance(outcome, EngineStopped)

    def test_a_state_that_c01_rejects_is_reported(self, tmp_path: Any) -> None:
        """I-Pはpendingが空であることを求める（永続化待ちの上へ重ねて作らない）。"""
        env = _runtime(
            tmp_path,
            replace(
                _recording_state(deferred=(violation(),)),
                pending_record=PendingRecord(
                    kind=RecordKind.INTEGRITY_INCIDENT,
                    binding=OpaqueBinding("cr:run-1:2:pending"),
                    source_state=State.MERGING,
                ),
            ),
        )
        outcome = record_incident(**_incident_kwargs(env, FakeIncidentPayloads()))
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "illegal_event"


class TestTheAuditReference:
    def test_the_audit_reference_is_recorded(self, tmp_path: Any) -> None:
        """cancelで未完了になったturnの監査参照がpayloadへ入る（節5.4）。"""
        audit = PendingRecord(
            kind=RecordKind.REVIEW_RESULT,
            binding=OpaqueBinding("cr:run-1:1:audit"),
            source_state=State.MERGING,
        )
        env = _runtime(tmp_path, _recording_state(deferred=(violation(),), audit=audit))
        port = FakeIncidentPayloads()
        outcome = record_incident(**_incident_kwargs(env, port))
        assert isinstance(outcome, IncidentRecorded), outcome
        assert audit_reference(audit) == {
            "kind": "REVIEW_RESULT",
            "binding": "cr:run-1:1:audit",
        }
        assert port.calls[0].audit == audit


class TestTheLedgerIsReadFailClosed:
    """台帳を読めないまま「記録済みが無い」と決めない。"""

    @pytest.mark.parametrize(
        "section",
        ["not-an-object", {"recorded_bindings": "not-a-list"}, {"recorded_bindings": [1]}],
        ids=["not_object", "not_list", "not_strings"],
    )
    def test_a_malformed_ledger_is_reported(self, section: Any) -> None:
        outcome = read_recorded_violations({"incident_record": section})
        assert isinstance(outcome, SectionUnavailable)

    def test_an_absent_ledger_reads_as_empty(self) -> None:
        assert read_recorded_violations({}) == ()
        assert read_recorded_violations({"incident_record": {}}) == ()

    def test_the_union_is_idempotent_and_sorted(self) -> None:
        first = with_recorded_violations({}, ["iv:b:2", "iv:a:1"])
        assert not isinstance(first, SectionUnavailable)
        again = with_recorded_violations(first, ["iv:a:1"])
        assert not isinstance(again, SectionUnavailable)
        assert read_recorded_violations(again) == ("iv:a:1", "iv:b:2")

    def test_a_malformed_ledger_is_not_overwritten(self) -> None:
        """壊れた台帳を「無いもの」として上書きしない（silent repair禁止）。"""
        outcome = with_recorded_violations({"incident_record": "broken"}, ["iv:a:1"])
        assert isinstance(outcome, SectionUnavailable)

    def test_the_schema_keeps_the_ledger_well_formed(self, tmp_path: Any) -> None:
        """壊れた台帳をcheckpointへ落とさない（`persist`側の防御が働く前に止まる）。

        readerが直和を返すのは、schemaを通っていないpayloadを受け取る呼び出しがあるため
        である。checkpointとして保存される経路では、この検証が先に働く。
        """
        env = _incident_env(tmp_path, deferred=(violation(),))
        payload = _payload(env)
        payload["incident_record"] = {"recorded_bindings": [1]}
        with pytest.raises(CheckpointStoreError):
            save_checkpoint(checkpoint_path(env.paths, RUN), payload)


class TestTheDriverReportsFailures:
    def test_a_refused_payload_stops_the_step(self, tmp_path: Any) -> None:
        """executorが停止したら、driverはそれをそのまま返す（推測で先へ進めない）。"""
        env = _runtime(tmp_path, _recording_state(deferred=(violation(),)))
        port = FakeIncidentPayloads(
            {"schema_version": 1, "violation_bindings": ["iv:other:1"], "summary": "違う範囲"}
        )
        ports = replace(_ports(env, incident=port), records=_LiveChain(env, (violation(),)))
        result = _drive(env, ports)
        assert isinstance(result.outcome, EngineStopped)
        assert result.outcome.code == "incident_bindings_mismatch"
        assert result.trace.incidents == 0
