# SPDX-License-Identifier: Apache-2.0
"""runtime portの受入test（Phase 8 PR-3b1。ADR-0020）。

導出できるportは製品として実装し、できないものは**担当componentを名指しして**fail closed
にする。「無いものを既定値で埋める」経路を作らないことを固定する。
"""

from __future__ import annotations

import pytest
from c06_support.helpers import HEAD
from c07_support.helpers import NUMBER, REPOSITORY, RUN, verified_chain
from c08_support.helpers import external_block, user_record_payload
from c08_support.runtime import runtime_env

from claude_code_codex_review_loop.domain.commands import HostAction
from claude_code_codex_review_loop.domain.states import State
from claude_code_codex_review_loop.domain.values import (
    Awaiting,
    MachineState,
    OpaqueBinding,
    OpaqueRef,
    RecordEvidence,
    RecordKind,
)
from claude_code_codex_review_loop.process import JobObjectRef, StopMethod, StopResult
from claude_code_codex_review_loop.runtime import (
    ChainEvidence,
    PortUnavailableError,
    RegistryRecordEvents,
    TreeStopper,
    UnavailableActionPayload,
    UnavailableIncidentPayload,
    UserInputBody,
)
from claude_code_codex_review_loop.workflow.ports import (
    ActionContext,
    BlockRequestContext,
    IncidentContext,
    UserRequestContext,
)


class _StaticRecords:
    def __init__(self, records) -> None:
        self._chain = records

    def chain(self, run_id: str):
        return self._chain


def _gate_context(head: str = HEAD) -> UserRequestContext:
    return UserRequestContext(
        awaiting=Awaiting.USER_INPUT_GATE,
        run_id=RUN,
        repository=REPOSITORY,
        number=NUMBER,
        head_sha=head,
    )


class TestChainRecords:
    def test_the_chain_comes_from_the_product_path(self, tmp_path) -> None:
        """C-05の取得とC-06の検証をそのまま通す（fixtureへ検証結果を直書きしない）。"""
        env = runtime_env(
            tmp_path,
            state=MachineState(state=State.READY_FOR_HUMAN_MERGE, awaiting=Awaiting.USER_INPUT_GATE),
            seeded=(RecordKind.REVIEW_RESULT, RecordKind.FINAL_REPORT),
        )
        chain = env.ports().records.chain(RUN)
        assert chain.is_intact
        assert [record.kind for record in chain.records] == [
            RecordKind.REVIEW_RESULT,
            RecordKind.FINAL_REPORT,
        ]


class TestTreeStopper:
    def test_the_stop_is_delegated_to_c03(self, monkeypatch) -> None:
        """C-08は停止を自分で実装しない（実際の停止挙動はC-03のtestが担保する）。"""
        from claude_code_codex_review_loop.runtime import ports as ports_module

        calls: list[tuple[object, float]] = []
        expected = StopResult(method=StopMethod.GRACEFUL, graceful_requested=True)

        def record(ref: object, grace_seconds: float) -> StopResult:
            calls.append((ref, grace_seconds))
            return expected

        monkeypatch.setattr(ports_module, "stop_tree_by_ref", record)
        ref = JobObjectRef(pid=4242, job_name="cc-review-tree-1")
        assert TreeStopper().stop(ref, 1.5) is expected
        assert calls == [(ref, 1.5)]


class TestChainEvidence:
    """DOD-02の選択規則: 対象headの全recordではなく、registryが宣言したkindだけ。"""

    def test_only_declared_kinds_are_selected(self) -> None:
        chain = verified_chain([RecordKind.REVIEW_RESULT, RecordKind.FINAL_REPORT])
        port = ChainEvidence(records=_StaticRecords(chain))
        selected = port.evidence_for(_gate_context())
        assert [record.kind for record in selected] == [RecordKind.FINAL_REPORT]

    def test_host_action_kinds_come_from_the_action_spec(self) -> None:
        chain = verified_chain([RecordKind.REVIEW_RESULT, RecordKind.FINAL_REPORT])
        port = ChainEvidence(records=_StaticRecords(chain))
        context = ActionContext(
            action=HostAction.APPLY_FINDINGS,
            run_id=RUN,
            repository=REPOSITORY,
            number=NUMBER,
            head_sha=HEAD,
        )
        assert [record.kind for record in port.evidence_for(context)] == [RecordKind.REVIEW_RESULT]

    def test_records_of_another_head_are_excluded(self) -> None:
        """head bindingを迂回する根拠を同梱しない（engineの`_evidence_of`と同じ条件）。"""
        chain = verified_chain([RecordKind.FINAL_REPORT])
        port = ChainEvidence(records=_StaticRecords(chain))
        assert port.evidence_for(_gate_context(head="b" * 40)) == ()

    def test_block_kinds_come_from_the_block_spec(self) -> None:
        """介入requestの根拠も同じ規則で選ぶ（registryが値域を決める。ADR-0023）。"""
        chain = verified_chain([RecordKind.EXTERNAL_DEPENDENCY, RecordKind.FINAL_REPORT])
        port = ChainEvidence(records=_StaticRecords(chain))
        context = BlockRequestContext(
            block=external_block(),
            block_binding="cr:run-1:1:external",
            run_id=RUN,
            repository=REPOSITORY,
            number=NUMBER,
            head_sha=HEAD,
        )
        selected = port.evidence_for(context)
        assert [record.kind for record in selected] == [RecordKind.EXTERNAL_DEPENDENCY]

    def test_selection_is_seq_ascending(self) -> None:
        chain = verified_chain([RecordKind.FINAL_REPORT, RecordKind.FINAL_REPORT])
        port = ChainEvidence(records=_StaticRecords(chain))
        selected = port.evidence_for(_gate_context())
        assert [record.seq for record in selected] == [1, 2]


class TestRegistryRecordEvents:
    def test_builds_the_event_for_registry_kinds(self) -> None:
        chain = verified_chain([RecordKind.GATE_QUESTION])
        record = chain.records[0]
        evidence = RecordEvidence(
            kind=record.kind, binding=OpaqueBinding(record.key), ref=OpaqueRef(record.comment_id)
        )
        event = RegistryRecordEvents().event_for(evidence, record)
        assert type(event).__name__ == "GateQuestionVerified"

    def test_kinds_needing_extra_inputs_are_refused(self) -> None:
        """`ProgressReport`はC-10 / C-11が決める値で、ここで作ると判定を偽装する。"""
        chain = verified_chain([RecordKind.FIX_RESULT])
        record = chain.records[0]
        evidence = RecordEvidence(
            kind=record.kind, binding=OpaqueBinding(record.key), ref=OpaqueRef(record.comment_id)
        )
        with pytest.raises(PortUnavailableError, match="C-10"):
            RegistryRecordEvents().event_for(evidence, record)

    def test_kinds_outside_the_registry_are_refused(self) -> None:
        chain = verified_chain([RecordKind.REVIEW_RESULT])
        record = chain.records[0]
        evidence = RecordEvidence(
            kind=record.kind, binding=OpaqueBinding(record.key), ref=OpaqueRef(record.comment_id)
        )
        with pytest.raises(PortUnavailableError, match="C-10"):
            RegistryRecordEvents().event_for(evidence, record)


class TestUserInputBody:
    """転記recordの本文は**ユーザーが書いた文そのもの**で、C-08は文面を作らない。"""

    @pytest.mark.parametrize(
        ("kind", "field"),
        [
            (RecordKind.USER_DECISION, "answer"),
            (RecordKind.GATE_QUESTION, "body"),
            (RecordKind.GATE_CHANGES, "body"),
            (RecordKind.USER_CANCEL, "reason"),
        ],
        ids=lambda value: getattr(value, "value", value),
    )
    def test_selects_the_declared_field(self, kind: RecordKind, field: str) -> None:
        payload = user_record_payload(kind)
        assert UserInputBody().body_for(kind, payload) == payload[field]

    def test_a_kind_without_free_text_is_refused(self) -> None:
        """`MERGE_APPROVAL`は自由記述を持たず、表現を作るのはC-13の領域である。"""
        payload = user_record_payload(RecordKind.MERGE_APPROVAL)
        with pytest.raises(PortUnavailableError, match="C-13"):
            UserInputBody().body_for(RecordKind.MERGE_APPROVAL, payload)

    def test_an_agent_record_is_refused(self) -> None:
        with pytest.raises(PortUnavailableError, match="C-10"):
            UserInputBody().body_for(RecordKind.FIX_RESULT, {})

    def test_an_empty_declared_field_is_refused(self) -> None:
        """optionalなfieldが空でも本文を作らない（`USER_CANCEL.reason`は任意）。"""
        payload = dict(user_record_payload(RecordKind.USER_CANCEL))
        del payload["reason"]
        with pytest.raises(PortUnavailableError, match="reason"):
            UserInputBody().body_for(RecordKind.USER_CANCEL, payload)


class TestUnavailableActionPayload:
    def test_names_the_owning_component(self) -> None:
        context = ActionContext(
            action=HostAction.APPLY_FINDINGS,
            run_id=RUN,
            repository=REPOSITORY,
            number=NUMBER,
            head_sha=HEAD,
        )
        with pytest.raises(PortUnavailableError, match="C-10"):
            UnavailableActionPayload().payload_for(context)


class TestUnavailableIncidentPayload:
    def test_names_the_owning_component(self) -> None:
        """incident record内容の構成はC-06の責務である（ADR-0024 決定12）。"""
        context = IncidentContext(
            violation_bindings=(OpaqueBinding("violation-1"),),
            audit=None,
            run_id=RUN,
            repository=REPOSITORY,
            number=NUMBER,
            head_sha=HEAD,
        )
        with pytest.raises(PortUnavailableError, match="C-06"):
            UnavailableIncidentPayload().payload_for(context)


def test_default_ports_binds_the_same_chain_source(tmp_path) -> None:
    """evidenceとrecordsが同じchain sourceを見る（別々に取得して食い違わせない）。"""
    env = runtime_env(
        tmp_path,
        state=MachineState(state=State.READY_FOR_HUMAN_MERGE, awaiting=Awaiting.USER_INPUT_GATE),
        seeded=(RecordKind.FINAL_REPORT,),
    )
    ports = env.ports()
    assert isinstance(ports.evidence, ChainEvidence)
    assert ports.evidence.records is ports.records
