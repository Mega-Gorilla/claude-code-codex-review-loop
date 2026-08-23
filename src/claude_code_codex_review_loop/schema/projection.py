# SPDX-License-Identifier: Apache-2.0
"""canonical recordのprojection codec（C-02所有。ADR-0010）。

GitHubへ永続化される機械情報はmarker payloadだけで、公開本文は人間可読テキストである。
そのため`RecordKind`だけでは`REVIEW_RESULT`のApproved / Blockingを区別できず、
「resumeがGitHubからstateを再構築する」（AC-C07-01〜03）が成立しない。本moduleは
検証済みpayloadのうち**state遷移の判断に使うscalar値**をmarkerへ射影する規約を持つ。

規約:

- projectionは**検証済みpayloadからの射影**であり、新しい値を作らない。marker keyの値は
  すべて既存schemaのfieldの写しで、語彙もschemaのenumをそのまま使う
- list値はcanonical digest（sorted unique）と要素数だけを載せ、内容は公開本文と
  local artifactへ置く。artifactはpayload hash（`pay`）へbindして照合する
- semantic payload hash（`pay`）はmarker付加より**前**の入力（検証済みpayload・射影・
  render済み公開本文）だけから決まる。したがってrecord binding（= idempotency key）の
  導出がbody hashへ依存せず、`key -> marker -> body hash -> key`の循環にならない
- `pay`は本文を入力に含むため、**同一keyのrecordは同一本文**である（同一payloadを別の
  本文へrenderすれば別key）。C-05のsearch-first（key一致 AND body hash一致）と整合する
- 検証はschemaのvalidatorを再利用する。projection側で意味的fieldを捏造しない
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from ..domain.values import RecordKind
from .decision import (
    DECISION_BRIEF,
    DECISION_RECORD,
    DECISION_REQUEST,
    DECISION_VERDICT,
    USER_DECISION,
)
from .merge import GATE_ANSWER, GATE_CHANGES, GATE_QUESTION, MERGE_APPROVAL
from .records import (
    BLOCK_INTERVENTION,
    CI_CODE_FAILURE,
    CI_TIMEOUT,
    EXTERNAL_DEPENDENCY,
    INTEGRITY_INCIDENT,
    PERMISSION_BLOCK,
    USER_CANCEL,
)
from .registry import SchemaDefinition, SchemaKind, validate_object
from .report import FINAL_REPORT
from .review import CLARIFICATION_ANSWER, CLARIFICATION_QUESTION, FIX_RESULT, REVIEW_RESULT
from .validate import is_integer_token

# markerへ載せるprojection key（構造key `key` / `kind` / `run` / `head` / `seq` / `prev`とは別）
RESULT_KEY: Final = "res"
ROUND_KEY: Final = "round"
TURN_KEY: Final = "turn"
FINGERPRINT_KEY: Final = "fp"
SUBJECT_KEY: Final = "sid"
TARGET_KEY: Final = "tgt"
PAYLOAD_HASH_KEY: Final = "pay"
DIGEST_KEY: Final = "dig"
COUNT_KEY: Final = "cnt"

PROJECTION_KEYS: Final = frozenset(
    {
        RESULT_KEY,
        ROUND_KEY,
        TURN_KEY,
        FINGERPRINT_KEY,
        SUBJECT_KEY,
        TARGET_KEY,
        PAYLOAD_HASH_KEY,
        DIGEST_KEY,
        COUNT_KEY,
    }
)

_INTEGER_KEYS: Final = frozenset({ROUND_KEY, TURN_KEY, COUNT_KEY})
_HASH_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
_SHA_PATTERN: Final = re.compile(r"[0-9a-f]{40}\Z")
# record bindingへ埋め込むrun IDの文字集合（`:`区切りのbindingを一意に読めるようにする）
_RUN_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
BINDING_PREFIX: Final = "cr:"


class ProjectionError(Exception):
    """projectionの構築失敗（producer側の誤り。未検証payload・head不一致・型不一致）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class ProjectionField:
    """1つのmarker keyと、その値を取り出す検証済みpayloadのfield名。"""

    key: str
    source: str
    required: bool = True


@dataclass(frozen=True)
class ProjectionSpec:
    """kindごとのprojection規約。`pay`は全kind共通なのでここには現れない。"""

    fields: tuple[ProjectionField, ...] = ()
    digest_source: str | None = None
    head_source: str | None = None

    @property
    def keys(self) -> frozenset[str]:
        """このkindが持ち得るprojection key（`pay`とdigest keyを含む）。"""
        keys = {field.key for field in self.fields} | {PAYLOAD_HASH_KEY}
        if self.digest_source is not None:
            keys |= {DIGEST_KEY, COUNT_KEY}
        return frozenset(keys)


@dataclass(frozen=True)
class DecodedProjection:
    """markerから復元したrecordの意味情報（値の解釈はC-10 / C-11が行う）。"""

    payload_hash: str
    result: str | None = None
    round: int | None = None
    turn: int | None = None
    fingerprint: str | None = None
    subject_id: str | None = None
    target: str | None = None
    digest: str | None = None
    count: int | None = None


def _field(key: str, source: str, *, required: bool = True) -> ProjectionField:
    return ProjectionField(key=key, source=source, required=required)


# kindごとのprojection。値の出所（payload field名）を明示し、射影以外の値を作らない
PROJECTION_SPECS: Final[dict[RecordKind, ProjectionSpec]] = {
    RecordKind.REVIEW_RESULT: ProjectionSpec(
        fields=(_field(RESULT_KEY, "verdict"), _field(ROUND_KEY, "round")),
        head_source="target_head_sha",
    ),
    RecordKind.FIX_RESULT: ProjectionSpec(head_source="pushed_head_sha"),
    RecordKind.CLARIFICATION_QUESTION: ProjectionSpec(
        fields=(
            _field(TURN_KEY, "turn"),
            _field(FINGERPRINT_KEY, "fingerprint"),
            _field(TARGET_KEY, "target_finding"),
        ),
        head_source="target_head_sha",
    ),
    RecordKind.CLARIFICATION_ANSWER: ProjectionSpec(
        fields=(
            _field(RESULT_KEY, "result"),
            _field(TURN_KEY, "turn"),
            _field(FINGERPRINT_KEY, "fingerprint"),
        ),
        head_source="target_head_sha",
    ),
    RecordKind.DECISION_REQUEST: ProjectionSpec(head_source="target_head_sha"),
    RecordKind.DECISION_VERDICT: ProjectionSpec(
        fields=(
            _field(RESULT_KEY, "verdict"),
            _field(FINGERPRINT_KEY, "fingerprint", required=False),
        ),
        head_source="target_head_sha",
    ),
    RecordKind.DECISION_BRIEF: ProjectionSpec(
        fields=(_field(SUBJECT_KEY, "decision_id"),),
        head_source="target_head_sha",
    ),
    RecordKind.DECISION_RECORD: ProjectionSpec(
        fields=(_field(SUBJECT_KEY, "decision_id"), _field(RESULT_KEY, "verdict")),
        head_source="target_head_sha",
    ),
    RecordKind.EXTERNAL_DEPENDENCY: ProjectionSpec(head_source="target_head_sha"),
    RecordKind.PERMISSION_BLOCK: ProjectionSpec(
        fields=(_field(SUBJECT_KEY, "permission_id"),),
        head_source="target_head_sha",
    ),
    RecordKind.CI_TIMEOUT: ProjectionSpec(head_source="target_head_sha"),
    RecordKind.CI_CODE_FAILURE: ProjectionSpec(head_source="target_head_sha"),
    RecordKind.FINAL_REPORT: ProjectionSpec(head_source="approved_head_sha"),
    RecordKind.INTEGRITY_INCIDENT: ProjectionSpec(digest_source="violation_bindings"),
    RecordKind.GATE_ANSWER: ProjectionSpec(head_source="target_head_sha"),
    RecordKind.USER_DECISION: ProjectionSpec(
        fields=(_field(SUBJECT_KEY, "decision_id"),),
        head_source="target_head_sha",
    ),
    RecordKind.GATE_QUESTION: ProjectionSpec(head_source="target_head_sha"),
    RecordKind.GATE_CHANGES: ProjectionSpec(head_source="target_head_sha"),
    RecordKind.MERGE_APPROVAL: ProjectionSpec(
        fields=(_field(RESULT_KEY, "merge_method"),),
        head_source="approved_head_sha",
    ),
    RecordKind.BLOCK_INTERVENTION: ProjectionSpec(
        fields=(_field(TARGET_KEY, "target_block_binding"),),
        head_source="target_head_sha",
    ),
    RecordKind.USER_CANCEL: ProjectionSpec(head_source="target_head_sha"),
}

# canonical record kindのschema定義（`schema/__init__`のREGISTRYとの一致はtestで常設検証する）
RECORD_DEFINITIONS: Final[dict[RecordKind, SchemaDefinition]] = {
    RecordKind.REVIEW_RESULT: REVIEW_RESULT,
    RecordKind.FIX_RESULT: FIX_RESULT,
    RecordKind.CLARIFICATION_QUESTION: CLARIFICATION_QUESTION,
    RecordKind.CLARIFICATION_ANSWER: CLARIFICATION_ANSWER,
    RecordKind.DECISION_REQUEST: DECISION_REQUEST,
    RecordKind.DECISION_VERDICT: DECISION_VERDICT,
    RecordKind.DECISION_BRIEF: DECISION_BRIEF,
    RecordKind.DECISION_RECORD: DECISION_RECORD,
    RecordKind.EXTERNAL_DEPENDENCY: EXTERNAL_DEPENDENCY,
    RecordKind.PERMISSION_BLOCK: PERMISSION_BLOCK,
    RecordKind.CI_TIMEOUT: CI_TIMEOUT,
    RecordKind.CI_CODE_FAILURE: CI_CODE_FAILURE,
    RecordKind.FINAL_REPORT: FINAL_REPORT,
    RecordKind.INTEGRITY_INCIDENT: INTEGRITY_INCIDENT,
    RecordKind.GATE_ANSWER: GATE_ANSWER,
    RecordKind.USER_DECISION: USER_DECISION,
    RecordKind.GATE_QUESTION: GATE_QUESTION,
    RecordKind.GATE_CHANGES: GATE_CHANGES,
    RecordKind.MERGE_APPROVAL: MERGE_APPROVAL,
    RecordKind.BLOCK_INTERVENTION: BLOCK_INTERVENTION,
    RecordKind.USER_CANCEL: USER_CANCEL,
}


def canonical_json(value: object) -> str:
    """canonical encoding（sorted keys / compact separators / 非ASCIIをそのまま）。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_payload_hash(payload: Mapping[str, object]) -> str:
    """検証済みpayloadのcanonical encodingのSHA-256 hex。"""
    return hashlib.sha256(canonical_json(dict(payload)).encode("utf-8")).hexdigest()


def normalize_body_for_hash(body: str) -> str:
    """hash対象の公開本文（marker付加時に埋め込まれる形と同一の正規化）。

    `attach_marker`は改行正規化済み本文の末尾改行を落としてmarker行を足すため、
    同じ正規化を行えばGitHub上のrecord本文からmarker行を除いた文字列と一致する
    （local artifactの照合が成立する）。transport側との一致はtestで常設検証する。
    """
    return body.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def semantic_payload_hash(
    *, body: str, payload: Mapping[str, object], projection: Mapping[str, str | int]
) -> str:
    """`pay`の値: 公開本文・検証済みpayload・射影を覆うSHA-256 hex。

    marker付加**前**に確定する3つの入力だけから決まる（`pay`自身は入力に含まない）。
    payload hashを含めるのは、local artifact（完全payload）をGitHub上のrecordへ
    bindできるようにするため（ADR-0010 決定13）。
    """
    material = canonical_json(
        {
            "body": normalize_body_for_hash(body),
            "payload": canonical_payload_hash(payload),
            "projection": dict(projection),
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def canonical_set_digest(values: Sequence[str]) -> tuple[str, int]:
    """文字列集合のdigestと要素数（順序・重複に依存しない）。"""
    unique = sorted(set(values))
    return hashlib.sha256(canonical_json(unique).encode("utf-8")).hexdigest(), len(unique)


def result_vocabulary(kind: RecordKind) -> tuple[str, ...] | None:
    """`res`の許可語彙（schemaのenumをそのまま使う。持たないkindはNone）。"""
    spec = PROJECTION_SPECS[kind]
    source = next((field.source for field in spec.fields if field.key == RESULT_KEY), None)
    if source is None:
        return None
    definition = RECORD_DEFINITIONS[kind]
    field = definition.versions[definition.current_version].fields[source]
    return field.enum


def build_record_projection(
    kind: RecordKind, payload: Mapping[str, object], *, head_sha: str, body: str
) -> dict[str, str | int]:
    """検証済みpayloadからmarker projectionを構築する（未検証payloadは受理しない）。

    `head_sha`はmarkerの`head`に載る値で、payloadの対象head fieldと一致しなければ
    errorにする（markerと本文の対象headが食い違うrecordを作らせない）。

    `body`は**sanitize -> redact -> render後・marker attach前**の公開本文で、`pay`の
    入力になる。同一payloadを別の本文へrenderしたrecordは別のbinding（= key）を持つ。
    """
    spec = PROJECTION_SPECS.get(kind)
    if spec is None:  # pragma: no cover - 全RecordKindを登録済み（testで常設検証する）
        raise ProjectionError(f"projection specが未登録のkind: {kind.value}")
    if not isinstance(body, str):
        raise ProjectionError("bodyはstrでなければならない")
    data = dict(payload)
    result = validate_object(RECORD_DEFINITIONS[kind], data)
    if not result.ok:
        codes = ",".join(sorted(error.code for error in result.errors))
        raise ProjectionError(f"payloadがschema検証を通らない（stage={result.stage}, codes={codes}）")
    if spec.head_source is not None and data.get(spec.head_source) != head_sha:
        raise ProjectionError(f"payloadの{spec.head_source}がmarkerのheadと一致しない")
    projection: dict[str, str | int] = {}
    for field in spec.fields:
        if field.source not in data:
            if field.required:
                raise ProjectionError(f"projectionに必要なfieldがpayloadに無い: {field.source}")
            continue
        value = data[field.source]
        if field.key in _INTEGER_KEYS:
            # 1始まりはdecode側の制約と揃える（builderだけが通す値を作らない）
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ProjectionError(f"{field.source}は1以上のintでなければならない")
        elif not isinstance(value, str) or not value:
            raise ProjectionError(f"{field.source}は非空のstrでなければならない")
        projection[field.key] = value
    if spec.digest_source is not None:
        values = data.get(spec.digest_source)
        if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
            raise ProjectionError(f"{spec.digest_source}は非空文字列のlistでなければならない")
        digest, count = canonical_set_digest(values)
        projection[DIGEST_KEY] = digest
        projection[COUNT_KEY] = count
    projection[PAYLOAD_HASH_KEY] = semantic_payload_hash(body=body, payload=data, projection=projection)
    return projection


def derive_record_binding(
    *, run_id: str, seq: int, kind: RecordKind, head_sha: str, payload_hash: str
) -> str:
    """canonical recordのbinding（= markerの`key` = idempotency key）を導出する。

    引数はいずれもmarker付加**前**に確定する値で、body hashを受け取れない。これにより
    `key -> marker -> body hash -> key`の循環が型として成立しない（ADR-0010）。

    digestは切り詰めない（record identity・idempotency key・block参照に使う値であり、
    短縮digestのchosen-input衝突耐性ではADRの「衝突不在」を支えられない）。

    identityの入口として、型もruntimeで検証する（型注釈だけに依存しない）。`bool`は
    Pythonでは`int`のsubclassであり、`float` / `str`はformat・比較の段階で
    `ProjectionError`以外のexceptionを漏らすため、C-02の`is_integer_token`
    （JSON integer意味論）で先に拒否する。
    """
    if not isinstance(kind, RecordKind):
        raise ProjectionError("kindはRecordKindでなければならない")
    if not isinstance(run_id, str) or _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ProjectionError("run IDは英数字と`.` `_` `-`のみ（64字以内）でなければならない")
    if not is_integer_token(seq) or seq < 1:
        raise ProjectionError("seqは1以上のJSON integer（bool / floatを含まない）でなければならない")
    if not isinstance(head_sha, str) or _SHA_PATTERN.fullmatch(head_sha) is None:
        raise ProjectionError("head SHAは40桁の小文字hexでなければならない")
    if not isinstance(payload_hash, str) or _HASH_PATTERN.fullmatch(payload_hash) is None:
        raise ProjectionError("payload hashはSHA-256 hex（64桁小文字）でなければならない")
    material = canonical_json(
        {"head": head_sha, "kind": kind.value, "pay": payload_hash, "run": run_id, "seq": seq}
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{BINDING_PREFIX}{run_id}:{seq:08d}:{digest}"


def _decoded_value(key: str, value: object) -> str | int | None:
    """projection値の型・形式検査。不正ならNone（呼び出し側が理由を作る）。"""
    if key in _INTEGER_KEYS:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return None
        return value
    if not isinstance(value, str) or not value:
        return None
    if key in {PAYLOAD_HASH_KEY, DIGEST_KEY} and _HASH_PATTERN.fullmatch(value) is None:
        return None
    return value


def decode_record_projection(kind: RecordKind, payload: Mapping[str, object]) -> DecodedProjection | str:
    """marker payloadからprojectionを復元する。非正規形式は理由文字列を返す。

    戻り値の文字列はC-06の「条件2（非正規marker）」の根拠になる。
    """
    spec = PROJECTION_SPECS.get(kind)
    if spec is None:  # pragma: no cover - 全RecordKindを登録済み（testで常設検証する）
        return f"projection specが未登録のkind: {kind.value}"
    allowed = spec.keys
    present = {key: value for key, value in payload.items() if key in PROJECTION_KEYS}
    unexpected = sorted(set(present) - allowed)
    if unexpected:
        return f"このkindが持たないprojection key: {unexpected[0]}"
    decoded: dict[str, str | int] = {}
    for key, value in present.items():
        checked = _decoded_value(key, value)
        if checked is None:
            return f"projection key `{key}`の値が不正"
        decoded[key] = checked
    payload_hash = decoded.get(PAYLOAD_HASH_KEY)
    if not isinstance(payload_hash, str):
        return f"projection key `{PAYLOAD_HASH_KEY}`が無い"
    for field in spec.fields:
        if field.required and field.key not in decoded:
            return f"このkindに必要なprojection key `{field.key}`が無い"
    if spec.digest_source is not None and not {DIGEST_KEY, COUNT_KEY} <= set(decoded):
        return f"このkindに必要なprojection key `{DIGEST_KEY}` / `{COUNT_KEY}`が無い"
    vocabulary = result_vocabulary(kind)
    result = decoded.get(RESULT_KEY)
    if vocabulary is not None and result is not None and result not in vocabulary:
        return f"projection key `{RESULT_KEY}`の値が語彙に無い"
    return DecodedProjection(
        payload_hash=payload_hash,
        result=result if isinstance(result, str) else None,
        round=_as_int(decoded.get(ROUND_KEY)),
        turn=_as_int(decoded.get(TURN_KEY)),
        fingerprint=_as_str(decoded.get(FINGERPRINT_KEY)),
        subject_id=_as_str(decoded.get(SUBJECT_KEY)),
        target=_as_str(decoded.get(TARGET_KEY)),
        digest=_as_str(decoded.get(DIGEST_KEY)),
        count=_as_int(decoded.get(COUNT_KEY)),
    )


def _as_int(value: str | int | None) -> int | None:
    return value if isinstance(value, int) else None


def _as_str(value: str | int | None) -> str | None:
    return value if isinstance(value, str) else None


def schema_kind_of(kind: RecordKind) -> SchemaKind:
    """canonical record kindに対応するSchemaKind（同名。C-02が全kindを所有する）。"""
    return SchemaKind(kind.value)
