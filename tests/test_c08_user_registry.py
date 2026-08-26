# SPDX-License-Identifier: Apache-2.0
"""`AWAIT_USER` registryの受入test（ADR-0018）。

registryはC-01が正本で、`Awaiting` / `PRODUCED_RULES` / schemaのenumと**drift不能**である
ことを固定する。重複防止keyの決定論性と、submit envelopeの判別keyが互いに素であることも
ここで検証する。
"""

from __future__ import annotations

import json

import pytest

from claude_code_codex_review_loop.domain._ruledefs import AWAITING_COMMANDS
from claude_code_codex_review_loop.domain._rules_workflow import PRODUCED_RULES
from claude_code_codex_review_loop.domain.values import (
    AWAITING_HOME,
    USER_INPUT_RECORD_KINDS,
    Awaiting,
    OpaqueBinding,
    OpaqueRef,
    RecordEvidence,
    RecordKind,
)
from claude_code_codex_review_loop.schema import REGISTRY, SchemaKind
from claude_code_codex_review_loop.schema.projection import PROJECTION_SPECS
from claude_code_codex_review_loop.schema.user_input import (
    INPUT_ROUTES,
    PERMISSION_RESUME,
    USER_INPUT_AWAITINGS,
    USER_REQUEST,
    USER_SUBMIT,
)
from claude_code_codex_review_loop.workflow import (
    INTENT_VALUE_FIELDS,
    RESULT_VARIANTS,
    USER_REQUEST_SPECS,
    ActionRegistryError,
    UserRequestSpec,
    build_event,
    intent_digest,
    intent_key,
    intent_value_of,
    user_spec_for,
)

SPECS = list(USER_REQUEST_SPECS.values())
IDS = [spec.kind for spec in SPECS]
HEAD = "a" * 40


def _allowed_by_c01(awaiting: Awaiting) -> set[RecordKind]:
    """C-01が当該awaitingの`RecordProduced`で受理するuser-input record kind。

    `USER_CANCEL`のようにawaitingを問わないrule（P-21）は、当該awaitingが滞在し得る
    stateすべてをruleが覆う場合に「受理する」と数える。`BLOCKED`限定のrule（P-22 / P-23）は
    滞在stateが重ならないため入らない。
    """
    homes = AWAITING_HOME[awaiting]
    allowed: set[RecordKind] = set()
    for rule in PRODUCED_RULES:
        kinds = set(rule.match.record_kinds or frozenset()) & USER_INPUT_RECORD_KINDS
        if not kinds:
            continue
        declared = rule.match.awaiting
        if declared is not None:
            if awaiting in declared:
                allowed |= kinds
        elif homes <= set(rule.match.states or frozenset()):
            allowed |= kinds
    return allowed


class TestRegistryCoversC01:
    def test_covers_every_user_input_awaiting(self) -> None:
        """C-01がユーザー入力で待つawaitingと過不足なく一致する。"""
        user_awaitings = {
            awaiting for awaiting in Awaiting if awaiting.value.startswith("USER_INPUT_")
        }
        assert set(USER_REQUEST_SPECS) == user_awaitings
        assert set(USER_INPUT_AWAITINGS) == user_awaitings

    def test_awaiting_field_matches_the_key(self) -> None:
        assert all(awaiting is spec.awaiting for awaiting, spec in USER_REQUEST_SPECS.items())

    def test_c01_issues_no_command_for_these_awaitings(self) -> None:
        """`AWAIT_USER`は発行済みcommandではなくawaitingからの導出である。"""
        assert all(AWAITING_COMMANDS[spec.awaiting] == () for spec in SPECS)

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_result_kinds_match_the_produced_rules(self, spec: UserRequestSpec) -> None:
        assert set(spec.result_kinds) == _allowed_by_c01(spec.awaiting)

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_result_kinds_are_user_input_records(self, spec: UserRequestSpec) -> None:
        assert set(spec.result_kinds) <= USER_INPUT_RECORD_KINDS

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_cancel_is_accepted_everywhere(self, spec: UserRequestSpec) -> None:
        """`USER_CANCEL`はawaiting不問（P-21）なので全specが受理する。"""
        assert RecordKind.USER_CANCEL in spec.result_kinds

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_evidence_kinds_are_declared(self, spec: UserRequestSpec) -> None:
        """判断の根拠を対象headの全recordから選ばず、awaitingごとに宣言する（DOD-02）。"""
        assert spec.evidence_kinds

    def test_only_permission_has_a_record_less_response(self) -> None:
        """recordを作らない応答はtool permissionの明示resumeだけである。"""
        with_resume = {spec.awaiting for spec in SPECS if spec.resume_event is not None}
        assert with_resume == {Awaiting.USER_INPUT_PERMISSION}

    def test_lookup_returns_none_for_other_awaitings(self) -> None:
        assert user_spec_for(Awaiting.HOST_APPLY_FINDINGS) is None


class TestResultAndEvent:
    @pytest.mark.parametrize(
        "kind", sorted(USER_INPUT_RECORD_KINDS, key=lambda k: k.value), ids=lambda k: k.value
    )
    def test_every_user_kind_has_a_schema_and_head_source(self, kind: RecordKind) -> None:
        """user-input recordは必ず対象headを持つ（head bindingの前提）。"""
        assert PROJECTION_SPECS[kind].head_source is not None

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_variants_build_the_matching_event(self, spec: UserRequestSpec) -> None:
        for kind in spec.result_kinds:
            variant = spec.variant_for(kind)
            assert variant is not None and variant.record_kind is kind
            assert variant.extra_event_inputs == ()
            evidence = RecordEvidence(
                kind=kind, binding=OpaqueBinding("cr:run-1:1:x"), ref=OpaqueRef("c-1")
            )
            event = build_event(variant, evidence, {})
            assert type(event).EXPECTED_KIND is kind  # type: ignore[attr-defined]

    @pytest.mark.parametrize("spec", SPECS, ids=IDS)
    def test_unlisted_kind_has_no_variant(self, spec: UserRequestSpec) -> None:
        assert spec.variant_for(RecordKind.REVIEW_RESULT) is None

    def test_result_schema_matches_the_record_kind(self) -> None:
        for kind in USER_INPUT_RECORD_KINDS & set(RESULT_VARIANTS):
            variant = RESULT_VARIANTS[kind]
            assert variant.result_schema.value == kind.value
            assert variant.result_definition is REGISTRY[variant.result_schema]


class TestSubmitClassification:
    """判別keyは互いに素である（ADR-0018 決定5）。"""

    def test_the_discriminators_are_disjoint(self) -> None:
        from claude_code_codex_review_loop.schema.action import SUBMIT

        host = set(SUBMIT.versions[SUBMIT.current_version].fields)
        user = set(USER_SUBMIT.versions[1].fields)
        assert "action_id" in host and "action_id" not in user
        assert "request_id" in user and "request_id" not in host

    def test_the_user_submit_requires_a_kind_outside_permission(self) -> None:
        from claude_code_codex_review_loop.schema import validate_object

        for awaiting in USER_INPUT_AWAITINGS:
            payload = {
                "schema_version": 1,
                "run_id": "run-1",
                "request_id": "req-1",
                "awaiting": awaiting.value,
                "expected_head_sha": HEAD,
                "nonce": "n-1",
                "result_hash": "rh-1",
            }
            result = validate_object(USER_SUBMIT, payload)
            assert result.ok is (awaiting is Awaiting.USER_INPUT_PERMISSION), awaiting


class TestIntentKey:
    """2経路の重複防止key（ADR-0018 決定7）。"""

    def _key(self, **overrides: object) -> str:
        values: dict[str, object] = {
            "run_id": "run-1",
            "awaiting": Awaiting.USER_INPUT_GATE,
            "since_seq": 3,
            "head_sha": HEAD,
            "kind": RecordKind.MERGE_APPROVAL,
        }
        values.update(overrides)
        return intent_key(**values)  # type: ignore[arg-type]

    def test_is_deterministic(self) -> None:
        assert self._key() == self._key()

    @pytest.mark.parametrize(
        "override",
        [
            {"run_id": "run-2"},
            {"awaiting": Awaiting.USER_INPUT_DECISION},
            {"since_seq": 4},
            {"head_sha": "b" * 40},
            {"kind": RecordKind.USER_CANCEL},
        ],
        ids=["run", "awaiting", "instance", "head", "intent"],
    )
    def test_every_component_changes_the_key(self, override) -> None:
        assert self._key(**override) != self._key()

    def test_is_parseable_json_after_the_prefix(self) -> None:
        """区切り文字を含むopaque値でも衝突しない（canonical JSONで導出する）。"""
        key = self._key(run_id="run:1")
        assert key.startswith("ui:")
        assert json.loads(key[len("ui:") :])["run"] == "run:1"


class TestInputRoutes:
    def test_the_vocabulary_is_fixed(self) -> None:
        """`input_route`の値域をC-08が確定する（USER_DECISION schemaの留保を閉じる）。"""
        assert set(INPUT_ROUTES) == {"github_comment", "host_transcript"}

    def test_the_request_and_resume_schemas_are_registered(self) -> None:
        for definition in (USER_REQUEST, USER_SUBMIT, PERMISSION_RESUME):
            assert REGISTRY[definition.kind] is definition


class TestIntentValue:
    """kindだけでは正規化intentが決まらない種別の契約（ADR-0018 決定7）。"""

    def test_only_free_text_kinds_declare_a_value(self) -> None:
        """merge gateの4 intentはkindと1対1なので値を宣言しない。"""
        assert set(INTENT_VALUE_FIELDS) == {RecordKind.USER_DECISION}

    @pytest.mark.parametrize(
        ("kind", "field"), sorted(INTENT_VALUE_FIELDS.items(), key=lambda item: item[0].value)
    )
    def test_the_declared_field_is_a_required_text_field(
        self, kind: RecordKind, field: str
    ) -> None:
        """宣言するfieldはrecord schemaの必須textでなければならない（両経路が必ず持てる）。"""
        definition = REGISTRY[SchemaKind(kind.value)]
        spec = definition.versions[definition.current_version]
        declared = spec.fields[field]
        assert declared.types == (str,) and declared.required

    def test_the_value_comes_from_the_validated_payload(self) -> None:
        payload = {"decision_id": "D-1", "answer": "[1]で進める", "input_route": "host_transcript"}
        assert intent_value_of(RecordKind.USER_DECISION, payload) == "[1]で進める"
        assert intent_value_of(RecordKind.MERGE_APPROVAL, payload) is None

    def test_different_values_produce_different_keys(self) -> None:
        first = intent_key(
            run_id="run-1",
            awaiting=Awaiting.USER_INPUT_DECISION,
            since_seq=1,
            head_sha=HEAD,
            kind=RecordKind.USER_DECISION,
            intent_value="[1]",
        )
        second = intent_key(
            run_id="run-1",
            awaiting=Awaiting.USER_INPUT_DECISION,
            since_seq=1,
            head_sha=HEAD,
            kind=RecordKind.USER_DECISION,
            intent_value="[2]",
        )
        assert first != second

    def test_only_whitespace_is_normalized(self) -> None:
        """表記の揺れで別intentにしないが、**意味の解釈はしない**。"""
        assert intent_digest("答え") == intent_digest("  答え" + chr(13) + chr(10) + "  ")
        assert intent_digest("答え") != intent_digest("答 え")

    @pytest.mark.parametrize(
        ("kind", "value"),
        [(RecordKind.USER_DECISION, None), (RecordKind.MERGE_APPROVAL, "APPROVE")],
        ids=["missing", "unexpected"],
    )
    def test_declaration_and_argument_must_agree(self, kind: RecordKind, value: str | None) -> None:
        """片方だけの経路が別のkeyを作らないよう、宣言との食い違いを受理しない。"""
        with pytest.raises(ActionRegistryError):
            intent_key(
                run_id="run-1",
                awaiting=Awaiting.USER_INPUT_GATE,
                since_seq=1,
                head_sha=HEAD,
                kind=kind,
                intent_value=value,
            )
