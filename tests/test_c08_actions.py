# SPDX-License-Identifier: Apache-2.0
"""action registryの受入test（ADR-0014）。

registryはC-01の`HostAction`が正本で、`Awaiting` / `AWAITING_COMMANDS` / schemaのenumと
**drift不能**であることを固定する。actionとC-01 eventが1対1であること、結果payloadの
schemaが実在することも合わせて検証する。
"""

from __future__ import annotations

import dataclasses

import pytest
from c08_support.helpers import integrity_block, limit_block, progress_block

from claude_code_codex_review_loop.domain import events as ev
from claude_code_codex_review_loop.domain._ruledefs import AWAITING_COMMANDS
from claude_code_codex_review_loop.domain._rules_workflow import PRODUCED_RULES
from claude_code_codex_review_loop.domain.commands import HostAction, RequestHostAction
from claude_code_codex_review_loop.domain.values import (
    AWAITING_HOME,
    Awaiting,
    OpaqueBinding,
    OpaqueRef,
    RecordEvidence,
    RecordKind,
)
from claude_code_codex_review_loop.schema import REGISTRY
from claude_code_codex_review_loop.schema.action import HOST_ACTION_KINDS, HOST_ACTION_PAYLOADS
from claude_code_codex_review_loop.schema.projection import PROJECTION_SPECS
from claude_code_codex_review_loop.workflow import (
    ACTION_SPECS,
    BLOCK_REQUEST_SPECS,
    RESULT_VARIANTS,
    USER_REQUEST_SPECS,
    ActionRegistryError,
    ActionSpec,
    ResultVariant,
    block_binding_of,
    block_spec_for,
    build_event,
    spec_for,
    spec_for_kind,
)


def _host_awaitings() -> set[Awaiting]:
    return {awaiting for awaiting in Awaiting if awaiting.value.startswith("HOST_")}


class TestRegistryCoversC01:
    """C-01が発行し得るactionと過不足なく一致する。"""

    def test_covers_every_host_action(self) -> None:
        assert set(ACTION_SPECS) == set(HostAction)

    def test_action_field_matches_the_key(self) -> None:
        assert all(action is spec.action for action, spec in ACTION_SPECS.items())

    def test_awaiting_is_a_bijection(self) -> None:
        """`Awaiting.HOST_*`とactionが1対1（どちらにも余りが無い）。"""
        awaitings = {spec.awaiting for spec in ACTION_SPECS.values()}
        assert awaitings == _host_awaitings()
        assert len(awaitings) == len(ACTION_SPECS)

    def test_awaiting_matches_the_c01_command_table(self) -> None:
        """`AWAITING_COMMANDS`が同じactionを発行する（C-01の表が正本）。"""
        for spec in ACTION_SPECS.values():
            assert AWAITING_COMMANDS[spec.awaiting] == (RequestHostAction(spec.action),)

    def test_awaiting_home_is_declared(self) -> None:
        """各awaitingは有効なstateを持つ（C-01のAWAITING_HOME）。"""
        assert all(AWAITING_HOME[spec.awaiting] for spec in ACTION_SPECS.values())

    def test_schema_enum_is_derived_from_the_registry(self) -> None:
        """schemaのenumはC-01のHostActionから導出され、二重定義を持たない。"""
        assert set(HOST_ACTION_KINDS) == {spec.kind for spec in ACTION_SPECS.values()}

    def test_every_action_has_a_payload_spec(self) -> None:
        assert set(HOST_ACTION_PAYLOADS) == set(HOST_ACTION_KINDS)


def _allowed_by_c01(awaiting: Awaiting) -> set[RecordKind]:
    """C-01が当該awaitingの`RecordProduced`で受理するrecord kind集合（rule registryから導出）。"""
    allowed: set[RecordKind] = set()
    for rule in PRODUCED_RULES:
        if awaiting in (rule.match.awaiting or frozenset()):
            allowed |= set(rule.match.record_kinds or frozenset())
    return allowed


class TestResultVariantsMatchC01:
    """**registryの結果集合はC-01が許可するkind集合と完全一致する**（driftを止める）。"""

    @pytest.mark.parametrize("spec", list(ACTION_SPECS.values()), ids=lambda spec: spec.kind)
    def test_result_kinds_match_the_produced_rules(self, spec: ActionSpec) -> None:
        assert set(spec.result_kinds) == _allowed_by_c01(spec.awaiting)

    def test_apply_findings_has_five_result_kinds(self) -> None:
        """修正完了だけでなく、質問・判断依頼・外部依存・permission停止も正規経路。"""
        spec = spec_for(HostAction.APPLY_FINDINGS)
        assert set(spec.result_kinds) == {
            RecordKind.FIX_RESULT,
            RecordKind.CLARIFICATION_QUESTION,
            RecordKind.DECISION_REQUEST,
            RecordKind.EXTERNAL_DEPENDENCY,
            RecordKind.PERMISSION_BLOCK,
        }

    def test_result_kinds_are_deterministic_and_unique(self) -> None:
        for spec in ACTION_SPECS.values():
            assert len(set(spec.result_kinds)) == len(spec.result_kinds)

    def test_every_result_kind_has_a_variant(self) -> None:
        used = {kind for spec in ACTION_SPECS.values() for kind in spec.result_kinds}
        assert used <= set(RESULT_VARIANTS)

    def test_no_unused_variant_is_declared(self) -> None:
        """使われないvariantを残さない（registryの語彙を実態と一致させる）。"""
        used = {kind for spec in ACTION_SPECS.values() for kind in spec.result_kinds}
        used |= {kind for spec in USER_REQUEST_SPECS.values() for kind in spec.result_kinds}
        used |= {kind for spec in BLOCK_REQUEST_SPECS for kind in spec.result_kinds}
        assert set(RESULT_VARIANTS) == used


def _variant_id(variant: ResultVariant) -> str:
    return variant.record_kind.value


# `RecordEvidence`からeventを組み立てるvariant（1対1のcontractが成り立つ範囲）。
# port_mappedなvariantはeventの受け取る形そのものが違うため、別のcontractで固定する
_EVIDENCE_VARIANTS = [variant for variant in RESULT_VARIANTS.values() if not variant.port_mapped]


class TestBlockRequestSpecsMatchC01:
    """介入requestを出すblockはC-01が決める（ADR-0023 決定7）。"""

    def test_the_specs_cover_the_c01_intervention_rules(self) -> None:
        """P-22（`NO_PROGRESS`の膠着）とP-23（外部依存）の2つだけである。"""
        rules = [
            rule
            for rule in PRODUCED_RULES
            if RecordKind.BLOCK_INTERVENTION in (rule.match.record_kinds or frozenset())
        ]
        assert {rule.rule_id for rule in rules} == {"P-22", "P-23"}
        declared = {spec.block_kind for spec in BLOCK_REQUEST_SPECS}
        assert declared == {kind for rule in rules for kind in (rule.match.block_kinds or ())}

    def test_the_progress_spec_is_limited_to_no_progress(self) -> None:
        """P-22はreasonまで一致しないと受理しない。limit到達の出口はlimit引き上げである。"""
        assert block_spec_for(progress_block()) is not None
        assert block_spec_for(limit_block()) is None

    def test_record_integrity_never_opens_the_transport(self) -> None:
        """出口は復元 / salvage専用evidenceで、fail closedのままにする。"""
        assert block_spec_for(integrity_block()) is None
        # attempt bindingを持たないので、requestの識別子も作れない
        assert block_binding_of(integrity_block()) is None

    def test_the_offered_kinds_are_the_ones_c01_accepts(self) -> None:
        """`USER_CANCEL`はP-21（awaiting不問・非terminal全state）が`BLOCKED`を覆うため。"""
        for spec in BLOCK_REQUEST_SPECS:
            assert set(spec.result_kinds) == {
                RecordKind.BLOCK_INTERVENTION,
                RecordKind.USER_CANCEL,
            }

    def test_a_kind_outside_the_spec_has_no_variant(self) -> None:
        """blockが受理しない種別の契約は引けない（提示していない結果を受け取らない）。"""
        spec = block_spec_for(progress_block())
        assert spec is not None
        assert spec.variant_for(RecordKind.MERGE_APPROVAL) is None
        assert spec.variant_for(RecordKind.BLOCK_INTERVENTION) is not None


class TestPortMappedVariants:
    """evidenceだけでは組み立てられないevent（ADR-0023 決定7）。

    `extra_event_inputs`は「C-08が作らない**値**」を宣言するが、こちらはevidenceの
    **形そのもの**が違う場合である。`BlockResolvedIntervention`が受け取るのは
    `BlockResolutionEvidence`（解除対象binding / head / record）であって`RecordEvidence`
    ではないため、registryは推測で埋めずに写像をportへ委ねる（ADR-0017 決定2）。
    """

    def test_only_block_intervention_is_port_mapped(self) -> None:
        mapped = {kind for kind, variant in RESULT_VARIANTS.items() if variant.port_mapped}
        assert mapped == {RecordKind.BLOCK_INTERVENTION}

    def test_build_event_refuses_a_port_mapped_variant(self) -> None:
        """**推測で埋めない**。呼び出し側がportを使うべきことをerrorで示す。"""
        variant = RESULT_VARIANTS[RecordKind.BLOCK_INTERVENTION]
        evidence = RecordEvidence(
            RecordKind.BLOCK_INTERVENTION, OpaqueBinding("bi-1"), OpaqueRef("c-1")
        )
        with pytest.raises(ActionRegistryError, match="RecordEvidenceだけでは構築できない"):
            build_event(variant, evidence, {})

    def test_the_event_is_the_c01_resolution_event(self) -> None:
        """写像先はC-01のblock解消event（B-IV1 / B-IV2の入口）。"""
        assert RESULT_VARIANTS[RecordKind.BLOCK_INTERVENTION].event is ev.BlockResolvedIntervention


class TestResultAndEvent:
    @pytest.mark.parametrize(
        "variant", list(RESULT_VARIANTS.values()), ids=lambda variant: variant.record_kind.value
    )
    def test_result_schema_exists(self, variant: ResultVariant) -> None:
        """結果payloadは既存のrecord schemaを再利用する（新規schemaを作らない）。"""
        assert variant.result_schema in REGISTRY

    @pytest.mark.parametrize(
        "variant", list(RESULT_VARIANTS.values()), ids=lambda variant: variant.record_kind.value
    )
    def test_result_schema_matches_the_record_kind(self, variant: ResultVariant) -> None:
        assert variant.result_schema.value == variant.record_kind.value

    @pytest.mark.parametrize("variant", _EVIDENCE_VARIANTS, ids=_variant_id)
    def test_event_expects_the_same_record_kind(self, variant: ResultVariant) -> None:
        """record kindとeventは1対1（値によるdiscriminationを持ち込まない）。"""
        assert variant.event.EXPECTED_KIND is variant.record_kind  # type: ignore[attr-defined]

    @pytest.mark.parametrize("variant", _EVIDENCE_VARIANTS, ids=_variant_id)
    def test_extra_event_inputs_match_the_event_fields(self, variant: ResultVariant) -> None:
        """`evidence`以外に要る値を宣言と一致させる（C-08が作らない値の明示）。"""
        fields = tuple(
            field.name for field in dataclasses.fields(variant.event) if field.name != "evidence"
        )
        assert fields == variant.extra_event_inputs

    def test_c10_owned_inputs_are_declared(self) -> None:
        """`ProgressReport` / `head`はC-10 / C-11由来で、C-08は自分で作らない。"""
        declared = {
            kind.value: variant.extra_event_inputs for kind, variant in RESULT_VARIANTS.items()
        }
        assert declared["FIX_RESULT"] == ("report",)
        assert declared["CLARIFICATION_QUESTION"] == ("report",)
        assert declared["EXTERNAL_DEPENDENCY"] == ("head",)
        assert declared["GATE_ANSWER"] == ()

    def test_record_kinds_are_internal(self) -> None:
        """host actionが投稿するrecordは内部record（user-input recordではない）。"""
        from claude_code_codex_review_loop.domain.values import INTERNAL_RECORD_KINDS

        used = {kind for spec in ACTION_SPECS.values() for kind in spec.result_kinds}
        assert used <= INTERNAL_RECORD_KINDS


class TestEvidenceSelection:
    """`verified_records`は対象headの全recordではなく、actionごとの根拠を選ぶ（DOD-02）。"""

    @pytest.mark.parametrize("spec", list(ACTION_SPECS.values()), ids=lambda spec: spec.kind)
    def test_evidence_kinds_are_declared(self, spec: ActionSpec) -> None:
        assert spec.evidence_kinds

    @pytest.mark.parametrize("spec", list(ACTION_SPECS.values()), ids=lambda spec: spec.kind)
    def test_evidence_kinds_are_unique(self, spec: ActionSpec) -> None:
        assert len(set(spec.evidence_kinds)) == len(spec.evidence_kinds)

    def test_evidence_is_not_the_action_result(self) -> None:
        """根拠はそのactionの入力であって、そのactionが作る結果ではない。"""
        for spec in ACTION_SPECS.values():
            if spec.action is not HostAction.REVISE_DECISION_REQUEST:
                assert not set(spec.evidence_kinds) & set(spec.result_kinds), spec.kind

    def test_revise_takes_the_previous_request_as_evidence(self) -> None:
        """再提出だけは、直前の同種recordを根拠にする（差し戻し対象そのもの）。"""
        spec = spec_for(HostAction.REVISE_DECISION_REQUEST)
        assert RecordKind.DECISION_REQUEST in spec.evidence_kinds


class TestResultHead:
    @pytest.mark.parametrize(
        "variant", list(RESULT_VARIANTS.values()), ids=lambda variant: variant.record_kind.value
    )
    def test_every_result_declares_a_head_source(self, variant: ResultVariant) -> None:
        """結果recordの対象headはpayloadから決まる（engineが既定値で埋めない）。"""
        assert PROJECTION_SPECS[variant.record_kind].head_source is not None

    @pytest.mark.parametrize(
        "variant", list(RESULT_VARIANTS.values()), ids=lambda variant: variant.record_kind.value
    )
    def test_result_definition_is_the_record_schema(self, variant: ResultVariant) -> None:
        assert variant.result_definition is REGISTRY[variant.result_schema]


class TestBuildEvent:
    """`extra_event_inputs`の宣言と実際の入力が一致しなければeventを作らない。"""

    def _evidence(self, kind: RecordKind) -> RecordEvidence:
        return RecordEvidence(kind=kind, binding=OpaqueBinding("cr:x"), ref=OpaqueRef("c-1"))

    def test_builds_an_event_without_extra_inputs(self) -> None:
        variant = RESULT_VARIANTS[RecordKind.GATE_ANSWER]
        event = build_event(variant, self._evidence(RecordKind.GATE_ANSWER), {})
        assert isinstance(event, ev.GateAnswerVerified)

    def test_missing_extra_input_is_rejected(self) -> None:
        variant = RESULT_VARIANTS[RecordKind.FIX_RESULT]
        with pytest.raises(ActionRegistryError):
            build_event(variant, self._evidence(RecordKind.FIX_RESULT), {})

    def test_unexpected_extra_input_is_rejected(self) -> None:
        """C-08が作らない値をNoneで埋めたり、余計な値を渡したりしない。"""
        variant = RESULT_VARIANTS[RecordKind.GATE_ANSWER]
        with pytest.raises(ActionRegistryError):
            build_event(variant, self._evidence(RecordKind.GATE_ANSWER), {"report": None})


class TestVariantLookup:
    def test_variant_for_allowed_kind(self) -> None:
        spec = spec_for(HostAction.APPLY_FINDINGS)
        variant = spec.variant_for(RecordKind.PERMISSION_BLOCK)
        assert variant is not None and variant.event is ev.ToolPermissionBlocked

    def test_variant_for_disallowed_kind_is_none(self) -> None:
        """当該actionで許可されないrecord種別は引けない（engineが拒否できる）。"""
        assert spec_for(HostAction.RECORD_DECISION).variant_for(RecordKind.FIX_RESULT) is None

    def test_variants_property_follows_the_declared_order(self) -> None:
        spec = spec_for(HostAction.APPLY_FINDINGS)
        assert tuple(variant.record_kind for variant in spec.variants) == spec.result_kinds


class TestLookup:
    def test_spec_for_returns_the_entry(self) -> None:
        assert spec_for(HostAction.RECORD_DECISION).result_kinds == (RecordKind.DECISION_RECORD,)

    def test_spec_for_kind_resolves_envelope_values(self) -> None:
        spec = spec_for_kind("ANSWER_GATE_QUESTION")
        assert spec is not None and spec.action is HostAction.ANSWER_GATE_QUESTION

    @pytest.mark.parametrize(
        "kind", ["IMPLEMENT_ISSUE", "ASK_CLARIFICATION", "", "apply_findings"]
    )
    def test_unknown_kind_is_none(self, kind: str) -> None:
        """v1の暫定enumに含まれていた値も、現在は未知として扱う。"""
        assert spec_for_kind(kind) is None
