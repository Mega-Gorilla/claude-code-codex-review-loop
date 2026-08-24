# SPDX-License-Identifier: Apache-2.0
"""action registryの受入test（ADR-0014）。

registryはC-01の`HostAction`が正本で、`Awaiting` / `AWAITING_COMMANDS` / schemaのenumと
**drift不能**であることを固定する。actionとC-01 eventが1対1であること、結果payloadの
schemaが実在することも合わせて検証する。
"""

from __future__ import annotations

import dataclasses

import pytest

from claude_code_codex_review_loop.domain._ruledefs import AWAITING_COMMANDS
from claude_code_codex_review_loop.domain.commands import HostAction, RequestHostAction
from claude_code_codex_review_loop.domain.values import AWAITING_HOME, Awaiting, RecordKind
from claude_code_codex_review_loop.schema import REGISTRY
from claude_code_codex_review_loop.schema.action import HOST_ACTION_KINDS, HOST_ACTION_PAYLOADS
from claude_code_codex_review_loop.workflow import (
    ACTION_SPECS,
    ActionSpec,
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


class TestResultAndEvent:
    @pytest.mark.parametrize("spec", list(ACTION_SPECS.values()), ids=lambda spec: spec.kind)
    def test_result_schema_exists(self, spec: ActionSpec) -> None:
        """結果payloadは既存のrecord schemaを再利用する（新規schemaを作らない）。"""
        assert spec.result_schema in REGISTRY

    @pytest.mark.parametrize("spec", list(ACTION_SPECS.values()), ids=lambda spec: spec.kind)
    def test_result_schema_matches_the_record_kind(self, spec: ActionSpec) -> None:
        assert spec.result_schema.value == spec.record_kind.value

    @pytest.mark.parametrize("spec", list(ACTION_SPECS.values()), ids=lambda spec: spec.kind)
    def test_event_expects_the_same_record_kind(self, spec: ActionSpec) -> None:
        """actionとeventは1対1（値によるdiscriminationを持ち込まない）。"""
        assert spec.event.EXPECTED_KIND is spec.record_kind  # type: ignore[attr-defined]

    @pytest.mark.parametrize("spec", list(ACTION_SPECS.values()), ids=lambda spec: spec.kind)
    def test_extra_event_inputs_match_the_event_fields(self, spec: ActionSpec) -> None:
        """`evidence`以外に要る値を宣言と一致させる（C-08が作らない値の明示）。"""
        fields = tuple(
            field.name for field in dataclasses.fields(spec.event) if field.name != "evidence"
        )
        assert fields == spec.extra_event_inputs

    def test_progress_report_is_declared_for_apply_findings(self) -> None:
        """`ProgressReport`はC-10 / C-11由来なので、C-08は自分で作らないことを明示する。"""
        assert spec_for(HostAction.APPLY_FINDINGS).extra_event_inputs == ("report",)

    def test_other_actions_need_only_the_evidence(self) -> None:
        others = [spec for spec in ACTION_SPECS.values() if spec.action is not HostAction.APPLY_FINDINGS]
        assert all(spec.extra_event_inputs == () for spec in others)

    def test_record_kinds_are_internal(self) -> None:
        """host actionが投稿するrecordは内部record（user-input recordではない）。"""
        from claude_code_codex_review_loop.domain.values import INTERNAL_RECORD_KINDS

        assert all(spec.record_kind in INTERNAL_RECORD_KINDS for spec in ACTION_SPECS.values())


class TestLookup:
    def test_spec_for_returns_the_entry(self) -> None:
        assert spec_for(HostAction.RECORD_DECISION).record_kind is RecordKind.DECISION_RECORD

    def test_spec_for_kind_resolves_envelope_values(self) -> None:
        spec = spec_for_kind("ANSWER_GATE_QUESTION")
        assert spec is not None and spec.action is HostAction.ANSWER_GATE_QUESTION

    @pytest.mark.parametrize(
        "kind", ["IMPLEMENT_ISSUE", "ASK_CLARIFICATION", "", "apply_findings"]
    )
    def test_unknown_kind_is_none(self, kind: str) -> None:
        """v1の暫定enumに含まれていた値も、現在は未知として扱う。"""
        assert spec_for_kind(kind) is None
