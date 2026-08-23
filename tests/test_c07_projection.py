# SPDX-License-Identifier: Apache-2.0
"""canonical record projection codecの受入test（ADR-0010、AC-C07-01〜03の前提）。

markerのprojectionは「検証済みpayloadからの射影」であり、新しい値を作らない。ここでは
(1) 全RecordKindがspecを持つこと、(2) specの参照先が実在のschema fieldであること、
(3) build -> decodeがround tripすること、(4) 未検証payload・head不一致・spec誤りが
黙って通らないこと、(5) binding導出が決定論的でbody hashへ依存しないことを検証する。
"""

from __future__ import annotations

import pytest
from c02_support.helpers import REPRESENTATIVE, record_payload

from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.schema import REGISTRY, SchemaKind
from claude_code_codex_review_loop.schema.projection import (
    BINDING_PREFIX,
    COUNT_KEY,
    DIGEST_KEY,
    FINGERPRINT_KEY,
    PAYLOAD_HASH_KEY,
    PROJECTION_KEYS,
    PROJECTION_SPECS,
    RECORD_DEFINITIONS,
    RESULT_KEY,
    ROUND_KEY,
    SUBJECT_KEY,
    TARGET_KEY,
    TURN_KEY,
    DecodedProjection,
    ProjectionError,
    ProjectionField,
    ProjectionSpec,
    build_record_projection,
    canonical_payload_hash,
    canonical_set_digest,
    decode_record_projection,
    derive_record_binding,
    result_vocabulary,
    schema_kind_of,
)

HEAD = "a" * 40
RUN = "run-1"
_ATTRIBUTE: dict[str, str] = {
    RESULT_KEY: "result",
    ROUND_KEY: "round",
    TURN_KEY: "turn",
    FINGERPRINT_KEY: "fingerprint",
    SUBJECT_KEY: "subject_id",
    TARGET_KEY: "target",
}


def _projection(kind: RecordKind, *, head: str = HEAD) -> dict[str, str | int]:
    return build_record_projection(kind, record_payload(kind, head_sha=head), head_sha=head)


class TestSpecIntegrity:
    """specとschemaのdrift検出（片方だけが変わることを許さない）。"""

    def test_every_record_kind_has_a_spec_and_definition(self) -> None:
        assert set(PROJECTION_SPECS) == set(RecordKind)
        assert set(RECORD_DEFINITIONS) == set(RecordKind)

    @pytest.mark.parametrize("kind", list(RecordKind), ids=lambda k: k.value)
    def test_definition_matches_c02_registry(self, kind: RecordKind) -> None:
        assert RECORD_DEFINITIONS[kind] is REGISTRY[schema_kind_of(kind)]
        assert schema_kind_of(kind) is SchemaKind(kind.value)

    @pytest.mark.parametrize("kind", list(RecordKind), ids=lambda k: k.value)
    def test_projection_sources_exist_in_schema(self, kind: RecordKind) -> None:
        """射影元はschemaに実在するfieldであり、projectionは値を発明しない。"""
        spec = PROJECTION_SPECS[kind]
        definition = RECORD_DEFINITIONS[kind]
        fields = definition.versions[definition.current_version].fields
        for field in spec.fields:
            assert field.source in fields, field.source
            assert field.key in PROJECTION_KEYS
        for source in (spec.digest_source, spec.head_source):
            assert source is None or source in fields

    @pytest.mark.parametrize("kind", list(RecordKind), ids=lambda k: k.value)
    def test_result_vocabulary_comes_from_schema_enum(self, kind: RecordKind) -> None:
        vocabulary = result_vocabulary(kind)
        has_result = any(field.key == RESULT_KEY for field in PROJECTION_SPECS[kind].fields)
        assert (vocabulary is not None) == has_result
        if vocabulary is not None:
            assert len(vocabulary) >= 1

    def test_keys_property_includes_digest_only_when_used(self) -> None:
        assert PROJECTION_SPECS[RecordKind.INTEGRITY_INCIDENT].keys >= {DIGEST_KEY, COUNT_KEY}
        assert DIGEST_KEY not in PROJECTION_SPECS[RecordKind.REVIEW_RESULT].keys
        assert PAYLOAD_HASH_KEY in PROJECTION_SPECS[RecordKind.USER_CANCEL].keys


class TestRoundTrip:
    @pytest.mark.parametrize("kind", list(RecordKind), ids=lambda k: k.value)
    def test_build_then_decode_is_identity(self, kind: RecordKind) -> None:
        payload = record_payload(kind, head_sha=HEAD)
        projection = build_record_projection(kind, payload, head_sha=HEAD)
        decoded = decode_record_projection(kind, projection)
        assert isinstance(decoded, DecodedProjection)
        assert decoded.payload_hash == canonical_payload_hash(payload)
        for field in PROJECTION_SPECS[kind].fields:
            if field.source in payload:
                assert getattr(decoded, _ATTRIBUTE[field.key]) == payload[field.source]

    def test_decode_ignores_structural_keys(self) -> None:
        """構造keyが同居していてもprojectionの解釈は変わらない（markerの実形）。"""
        projection = _projection(RecordKind.REVIEW_RESULT)
        marker = dict(projection, key="cr:x", kind="REVIEW_RESULT", run=RUN, head=HEAD, seq=1)
        assert decode_record_projection(RecordKind.REVIEW_RESULT, marker) == decode_record_projection(
            RecordKind.REVIEW_RESULT, projection
        )

    def test_review_result_projects_verdict_and_round(self) -> None:
        projection = _projection(RecordKind.REVIEW_RESULT)
        assert projection == {
            PAYLOAD_HASH_KEY: projection[PAYLOAD_HASH_KEY],
            RESULT_KEY: "CHANGES_REQUESTED",
            ROUND_KEY: 1,
        }

    def test_integrity_incident_projects_set_digest(self) -> None:
        payload = record_payload(RecordKind.INTEGRITY_INCIDENT, head_sha=HEAD)
        projection = build_record_projection(RecordKind.INTEGRITY_INCIDENT, payload, head_sha=HEAD)
        bindings = payload["violation_bindings"]
        assert isinstance(bindings, list)
        digest, count = canonical_set_digest([str(item) for item in bindings])
        assert projection[DIGEST_KEY] == digest and projection[COUNT_KEY] == count

    def test_optional_field_absent_is_omitted(self) -> None:
        payload = dict(REPRESENTATIVE[SchemaKind.DECISION_VERDICT])
        payload["target_head_sha"] = HEAD
        payload.pop("fingerprint", None)
        projection = build_record_projection(RecordKind.DECISION_VERDICT, payload, head_sha=HEAD)
        assert FINGERPRINT_KEY not in projection
        decoded = decode_record_projection(RecordKind.DECISION_VERDICT, projection)
        assert isinstance(decoded, DecodedProjection) and decoded.fingerprint is None


class TestCanonicalHashing:
    def test_payload_hash_is_key_order_independent(self) -> None:
        first = {"schema_version": 1, "b": "2", "a": "1"}
        second = {"a": "1", "schema_version": 1, "b": "2"}
        assert canonical_payload_hash(first) == canonical_payload_hash(second)

    def test_payload_hash_changes_with_content(self) -> None:
        assert canonical_payload_hash({"a": "1"}) != canonical_payload_hash({"a": "2"})

    def test_set_digest_ignores_order_and_duplicates(self) -> None:
        assert canonical_set_digest(["b", "a", "b"]) == canonical_set_digest(["a", "b"])
        assert canonical_set_digest(["a", "b"])[1] == 2

    def test_set_digest_distinguishes_members(self) -> None:
        assert canonical_set_digest(["a"])[0] != canonical_set_digest(["b"])[0]


class TestBuildRejections:
    def test_unvalidated_payload_is_rejected(self) -> None:
        payload = dict(REPRESENTATIVE[SchemaKind.REVIEW_RESULT])
        del payload["verdict"]
        with pytest.raises(ProjectionError, match="schema検証"):
            build_record_projection(RecordKind.REVIEW_RESULT, payload, head_sha=HEAD)

    def test_head_mismatch_is_rejected(self) -> None:
        """markerのheadとpayloadの対象headが食い違うrecordを作らせない。"""
        payload = record_payload(RecordKind.REVIEW_RESULT, head_sha=HEAD)
        with pytest.raises(ProjectionError, match="target_head_sha"):
            build_record_projection(RecordKind.REVIEW_RESULT, payload, head_sha="b" * 40)

    def test_required_source_missing_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """specが必須と宣言したfieldがpayloadに無ければ、markerを作らず停止する。"""
        spec = ProjectionSpec(
            fields=(ProjectionField(key=SUBJECT_KEY, source="fingerprint"),),
            head_source="target_head_sha",
        )
        monkeypatch.setitem(PROJECTION_SPECS, RecordKind.DECISION_VERDICT, spec)
        payload = record_payload(RecordKind.DECISION_VERDICT, head_sha=HEAD)
        payload.pop("fingerprint", None)
        with pytest.raises(ProjectionError, match="fingerprint"):
            build_record_projection(RecordKind.DECISION_VERDICT, payload, head_sha=HEAD)

    def test_integer_key_bound_to_text_field_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spec = ProjectionSpec(
            fields=(ProjectionField(key=ROUND_KEY, source="verdict"),), head_source="target_head_sha"
        )
        monkeypatch.setitem(PROJECTION_SPECS, RecordKind.REVIEW_RESULT, spec)
        with pytest.raises(ProjectionError, match="int"):
            build_record_projection(
                RecordKind.REVIEW_RESULT, record_payload(RecordKind.REVIEW_RESULT, head_sha=HEAD), head_sha=HEAD
            )

    def test_text_key_bound_to_integer_field_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spec = ProjectionSpec(
            fields=(ProjectionField(key=SUBJECT_KEY, source="round"),), head_source="target_head_sha"
        )
        monkeypatch.setitem(PROJECTION_SPECS, RecordKind.REVIEW_RESULT, spec)
        with pytest.raises(ProjectionError, match="str"):
            build_record_projection(
                RecordKind.REVIEW_RESULT, record_payload(RecordKind.REVIEW_RESULT, head_sha=HEAD), head_sha=HEAD
            )

    def test_digest_source_must_be_string_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            PROJECTION_SPECS, RecordKind.INTEGRITY_INCIDENT, ProjectionSpec(digest_source="summary")
        )
        with pytest.raises(ProjectionError, match="list"):
            build_record_projection(
                RecordKind.INTEGRITY_INCIDENT,
                record_payload(RecordKind.INTEGRITY_INCIDENT, head_sha=HEAD),
                head_sha=HEAD,
            )


class TestBindingDerivation:
    def _binding(self, **overrides: object) -> str:
        arguments: dict[str, object] = {
            "run_id": RUN,
            "seq": 1,
            "kind": RecordKind.REVIEW_RESULT,
            "head_sha": HEAD,
            "payload_hash": "c" * 64,
        }
        arguments.update(overrides)
        return derive_record_binding(**arguments)  # type: ignore[arg-type]

    def test_is_deterministic(self) -> None:
        assert self._binding() == self._binding()
        assert self._binding().startswith(f"{BINDING_PREFIX}{RUN}:00000001:")

    @pytest.mark.parametrize(
        "override",
        [
            {"run_id": "run-2"},
            {"seq": 2},
            {"kind": RecordKind.FIX_RESULT},
            {"head_sha": "b" * 40},
            {"payload_hash": "d" * 64},
        ],
        ids=["run", "seq", "kind", "head", "payload"],
    )
    def test_any_input_change_changes_binding(self, override: dict[str, object]) -> None:
        assert self._binding(**override) != self._binding()

    @pytest.mark.parametrize(
        "override",
        [
            {"run_id": "run:1"},
            {"run_id": ""},
            {"seq": 0},
            {"head_sha": "a" * 39},
            {"payload_hash": "z" * 64},
        ],
        ids=["run_charset", "run_empty", "seq", "head", "payload_hash"],
    )
    def test_invalid_input_is_rejected(self, override: dict[str, object]) -> None:
        with pytest.raises(ProjectionError):
            self._binding(**override)

    def test_derivation_does_not_accept_body_hash(self) -> None:
        """`key -> marker -> body hash -> key`の循環をsignatureで排除している（ADR-0010）。"""
        with pytest.raises(TypeError):
            derive_record_binding(  # type: ignore[call-arg]
                run_id=RUN,
                seq=1,
                kind=RecordKind.REVIEW_RESULT,
                head_sha=HEAD,
                payload_hash="c" * 64,
                body_hash="d" * 64,
            )


class TestDecodeRejections:
    """非正規projectionはC-06の条件2（非正規marker）の根拠になる理由文字列を返す。"""

    def _reason(self, kind: RecordKind, payload: dict[str, object]) -> str:
        decoded = decode_record_projection(kind, payload)
        assert isinstance(decoded, str)
        return decoded

    def test_missing_payload_hash(self) -> None:
        projection = dict(_projection(RecordKind.REVIEW_RESULT))
        del projection[PAYLOAD_HASH_KEY]
        assert PAYLOAD_HASH_KEY in self._reason(RecordKind.REVIEW_RESULT, dict(projection))

    def test_missing_required_key(self) -> None:
        projection = dict(_projection(RecordKind.REVIEW_RESULT))
        del projection[ROUND_KEY]
        assert ROUND_KEY in self._reason(RecordKind.REVIEW_RESULT, dict(projection))

    def test_key_not_allowed_for_kind(self) -> None:
        projection = dict(_projection(RecordKind.REVIEW_RESULT))
        projection[TURN_KEY] = 1
        assert TURN_KEY in self._reason(RecordKind.REVIEW_RESULT, dict(projection))

    def test_missing_digest_keys(self) -> None:
        projection = dict(_projection(RecordKind.INTEGRITY_INCIDENT))
        del projection[DIGEST_KEY]
        assert DIGEST_KEY in self._reason(RecordKind.INTEGRITY_INCIDENT, dict(projection))

    def test_result_outside_vocabulary(self) -> None:
        projection = dict(_projection(RecordKind.REVIEW_RESULT))
        projection[RESULT_KEY] = "APPROVED_BY_HAND"
        assert "語彙" in self._reason(RecordKind.REVIEW_RESULT, dict(projection))

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            (ROUND_KEY, "1"),
            (ROUND_KEY, True),
            (ROUND_KEY, 0),
            (RESULT_KEY, 1),
            (RESULT_KEY, ""),
            (PAYLOAD_HASH_KEY, "not-a-hash"),
        ],
        ids=["round_str", "round_bool", "round_zero", "result_int", "result_empty", "hash_shape"],
    )
    def test_malformed_value(self, key: str, value: object) -> None:
        projection = dict(_projection(RecordKind.REVIEW_RESULT))
        projection[key] = value  # type: ignore[assignment]
        assert key in self._reason(RecordKind.REVIEW_RESULT, dict(projection))

    def test_malformed_digest_value(self) -> None:
        projection = dict(_projection(RecordKind.INTEGRITY_INCIDENT))
        projection[DIGEST_KEY] = "short"
        assert DIGEST_KEY in self._reason(RecordKind.INTEGRITY_INCIDENT, dict(projection))
