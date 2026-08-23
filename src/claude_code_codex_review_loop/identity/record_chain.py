# SPDX-License-Identifier: Apache-2.0
"""record chainの再構築と7条件検証（AC-C06-06〜09）。chain specの正本はADR-0008。

**Chain spec（C-07 / C-08と共有する仕様点）**: 内部recordはmarker payloadの6 keyを使う —
`key`（冪等投稿key）/ `kind`（`RecordKind.value`）/ `run`（chainのscope。runごとに独立chain）/
`head`（対象head SHA）/ `seq`（run内で1始まりの通し番号。kind横断の単一系列）/
`prev`（直前record（seq-1）の`body_hash` = 正規化済み全body・marker行込みのSHA-256 hex）。
genesis（seq=1）はprevの欠如が正規形式で、sentinelを使わない。prevが前recordのmarker行
（そのseq / prev）を推移的に被覆するため、並べ替え・差し替えがhash照合で検出できる。

構造keyに加えて、C-02が定義するprojection key（検証済みpayloadからのscalar射影。ADR-0010）
を持つ。projectionの正規性判定は`decode_record_projection`へ委譲し、失敗は条件2として扱う。

検出する7条件（実装plan Section 5のthreat model表）:

1. 不正actor: producer allowlistと完全一致しないactorのchain record投稿（`iv:actor:`）
2. 埋め込みmarker: 末尾1行の正規形式以外の予約token出現（`iv:marker:`。ADR-0007 決定2）
3. 本文改変: checkpointが記録したbody hashと現在値の不一致（`iv:tamper:`）
4. 編集: `updatedAt != createdAt`（`iv:edited:`。Controllerはrecordを編集しない）
5. 中間削除: sequence番号のgap（`iv:gap:`。検査範囲は1..max(観測最大seq, N)）
6. 並べ替え: prevのhash chain不一致（`iv:chain:`）
7. 既知record消失: checkpointの既知comment IDがGitHubで404（`iv:missing:`）

**保証しない範囲（AC-C06-09）**: checkpointを失ったfresh resume、およびhigh-water mark `N`
より後のtail truncationは検出できない（gap検査範囲の上限が観測最大seqとNで決まるため、
構造的に正常なconversationとして扱われる）。`ChainVerification.assurance_high_water`が
この境界を明示する。

violation（改ざんの証拠）とerror（呼び出し誤り・設定誤り・一時障害）は峻別する:
probeのTRANSIENT失敗は再raiseし、violationを捏造しない。bindingは検出回・順序に依存しない
決定論的導出（`iv:{condition}:{run}:{subject}`）で、`canonicalize_integrity`と両立する。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from ..domain.values import (
    IntegrityEvidenceRef,
    OpaqueBinding,
    OpaqueRef,
    RecordKind,
    canonicalize_integrity,
)
from ..errors import ErrorCategory
from ..schema.projection import DecodedProjection, decode_record_projection
from ..transport.conversation import UnverifiedComment, get_issue_comment
from ..transport.gh import GhApiError, GhContext, RepoRef, RetryPolicy
from ..transport.marker import (
    ALLOWED_PAYLOAD_KEYS,
    MARKER_TOKEN,
    MARKER_VERSION,
    MAX_PAYLOAD_BYTES,
    STRUCTURAL_PAYLOAD_KEYS,
)
from .actor import ActorClass, resolve_actor
from .allowlist import ProducerAllowlist
from .errors import IdentityError

_HASH_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")


# ---------------------------------------------------------------------------
# 入力・結果の型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnownRecord:
    """checkpointが保持する既知record（seqはhigh-water mark以下）。"""

    seq: int
    comment_id: str
    body_hash: str


@dataclass(frozen=True)
class ChainCheckpoint:
    """local checkpointのchain部分。high_water_markは確認済み最大seq（`N`）。"""

    high_water_mark: int
    known_records: tuple[KnownRecord, ...]

    def __post_init__(self) -> None:
        if self.high_water_mark < 0:
            raise IdentityError("checkpoint", "high-water markは非負でなければならない")
        seqs = [known.seq for known in self.known_records]
        ids = [known.comment_id for known in self.known_records]
        if len(set(seqs)) != len(seqs) or len(set(ids)) != len(ids):
            raise IdentityError("checkpoint", "既知recordのseqとcomment IDは一意でなければならない")
        for known in self.known_records:
            if known.seq < 1 or known.seq > self.high_water_mark:
                raise IdentityError("checkpoint", f"既知recordのseqが範囲外: {known.seq}")


@dataclass(frozen=True)
class ChainPayload:
    """正規形式markerから解析したchain payload。prevはgenesisのみNone。"""

    key: str
    kind: RecordKind
    run: str
    head: str
    seq: int
    prev: str | None
    projection: DecodedProjection


@dataclass(frozen=True)
class VerifiedRecord:
    """検証を通過したcanonical record。C-07以降はこの型だけを入力にする。"""

    seq: int
    kind: RecordKind
    key: str
    comment_id: str
    url: str
    body: str
    body_hash: str
    author_login: str
    head_sha: str
    created_at: str
    projection: DecodedProjection


@dataclass(frozen=True)
class ChainVerification:
    """chain検証の結果。violationsはcanonical order（binding昇順・重複なし）。

    - recordsはviolationに関与しなかったrecordのseq昇順列（violationがある場合も
      差分提示のために返すが、consumerは`is_intact`でgateする）
    - assurance_high_waterはAC-C06-09の境界: これ以下の欠落は検出済み、これより後の
      tail truncationは検出できない残存risk
    """

    records: tuple[VerifiedRecord, ...]
    violations: tuple[IntegrityEvidenceRef, ...]
    max_seq: int
    assurance_high_water: int

    @property
    def is_intact(self) -> bool:
        """violationが1件もないこと。C-07以降が進行してよい条件。"""
        return not self.violations


# ---------------------------------------------------------------------------
# marker payloadの合成と解析（producer=C-08とverifierの共有規約。同居でround-trip保証）
# ---------------------------------------------------------------------------


def compose_record_marker_payload(
    *,
    key: str,
    kind: RecordKind,
    run_id: str,
    head_sha: str,
    seq: int,
    prev_body_hash: str | None,
    projection: Mapping[str, str | int] | None = None,
) -> dict[str, str | int]:
    """chain recordのmarker payloadを合成する（`attach_marker`へ渡す形。ADR-0008）。

    projectionはC-02の`build_record_projection`が作る意味情報の射影で、構造keyを
    上書きできない。合成後のpayloadが上限byte数を超える場合はここで停止する
    （marker attachと投稿より前に落とし、markerが本文の代替へ肥大化する方向を塞ぐ）。
    """
    for name, value in (("key", key), ("run", run_id), ("head", head_sha)):
        if not value:
            raise IdentityError("compose", f"{name}は空にできない")
    if seq < 1:
        raise IdentityError("compose", "seqは1始まりの正の整数でなければならない")
    if (seq == 1) != (prev_body_hash is None):
        raise IdentityError("compose", "prevはgenesis（seq=1）でのみ欠如し、seq>=2では必須")
    payload: dict[str, str | int] = {
        "key": key,
        "kind": kind.value,
        "run": run_id,
        "head": head_sha,
        "seq": seq,
    }
    if prev_body_hash is not None:
        if _HASH_PATTERN.fullmatch(prev_body_hash) is None:
            raise IdentityError("compose", "prevはSHA-256 hex（64桁小文字）でなければならない")
        payload["prev"] = prev_body_hash
    for projection_key, projection_value in (projection or {}).items():
        if projection_key in STRUCTURAL_PAYLOAD_KEYS:
            raise IdentityError("compose", f"projectionは構造keyを上書きできない: {projection_key}")
        payload[projection_key] = projection_value
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise IdentityError("compose", "marker payloadが上限byte数を超える")
    return payload


def _parse_chain_payload(comment: UnverifiedComment) -> ChainPayload | str:
    """予約tokenを含むcommentの正規形式判定。非正規形式は理由文字列（条件2の根拠）。"""
    if comment.marker is None:
        return "予約tokenが本文末尾の正規形式markerでない"
    if comment.body.upper().count(MARKER_TOKEN) != 1:
        return "予約tokenがmarker行以外にも出現する"
    payload = comment.marker.payload
    if payload is None:
        return "marker payloadがJSONとして解釈できない"
    raw_json = comment.marker.raw_json
    if len(raw_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        return "marker payloadが上限byte数を超える"
    # canonical encoding（sorted keysのcompact JSON。ADR-0007 決定1）との完全一致を要求する。
    # 複数行・空白・key順の乱れ・重複key（parseで縮退する）は全て非正規形式になる
    canonical = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if raw_json != canonical:
        return "marker payloadがcanonical encoding（sorted keysのcompact JSON）でない"
    # markerは本文末尾の単独1行そのもの（末尾の余分な空白・改行も非正規形式）
    if not comment.body.endswith(f"\n<!-- {MARKER_TOKEN}:{MARKER_VERSION} {raw_json} -->"):
        return "markerが本文末尾の単独1行でない"
    if not set(payload.keys()) <= ALLOWED_PAYLOAD_KEYS:
        return "markerが許可されないkeyを含む"
    values: dict[str, str] = {}
    for field in ("key", "kind", "run", "head"):
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            return f"markerの{field}が欠如または不正"
        values[field] = value
    seq = payload.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        return "markerのseqが1始まりの整数でない"
    prev = payload.get("prev")
    if seq == 1:
        if "prev" in payload:
            return "genesis record（seq=1）はprevを持たない"
        prev = None
    elif not isinstance(prev, str) or _HASH_PATTERN.fullmatch(prev) is None:
        return "markerのprevがSHA-256 hexでない"
    try:
        kind = RecordKind(values["kind"])
    except ValueError:
        return "markerのkindが未知の種別"
    # 意味情報（projection）の正規性判定はC-02へ委譲する（定義を1箇所に保つ。ADR-0010）
    projection = decode_record_projection(kind, payload)
    if isinstance(projection, str):
        return projection
    return ChainPayload(
        key=values["key"],
        kind=kind,
        run=values["run"],
        head=values["head"],
        seq=seq,
        prev=prev if isinstance(prev, str) else None,
        projection=projection,
    )


# ---------------------------------------------------------------------------
# 既知record probe（I/O wrapper。404のみをviolation材料にする）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeFound:
    """既知comment IDがGitHubに現存する（本文照合はpure core側で行う）。"""

    comment: UnverifiedComment


@dataclass(frozen=True)
class ProbeMissing:
    """既知comment IDがGitHubで404（AC-C06-08の材料）。"""

    comment_id: str


ProbeOutcome = ProbeFound | ProbeMissing


def probe_known_records(
    context: GhContext,
    repo: RepoRef,
    checkpoint: ChainCheckpoint,
    *,
    present_comment_ids: frozenset[str],
    policy: RetryPolicy,
) -> dict[str, ProbeOutcome]:
    """fetch結果に現れなかった既知comment IDをGETで実在確認する。

    NOT_FOUNDのみを`ProbeMissing`にする。TRANSIENT / AUTH / PERMANENTは再raiseし、
    一時障害や設定不備からviolationを捏造しない（bounded retryはpolicyが内蔵）。
    """
    outcomes: dict[str, ProbeOutcome] = {}
    for known in checkpoint.known_records:
        if known.comment_id in present_comment_ids:
            continue
        try:
            comment = get_issue_comment(context, repo, known.comment_id, policy=policy)
        except GhApiError as exc:
            if exc.category is ErrorCategory.NOT_FOUND:
                outcomes[known.comment_id] = ProbeMissing(comment_id=known.comment_id)
                continue
            raise
        outcomes[known.comment_id] = ProbeFound(comment=comment)
    return outcomes


# ---------------------------------------------------------------------------
# pure core
# ---------------------------------------------------------------------------


def _violation(
    condition: str, run_id: str, subject: str, detection_head: str, **fields: object
) -> IntegrityEvidenceRef:
    """violationの決定論的構成。bindingは検出回・順序に依存しない（冪等）。"""
    descriptor: dict[str, object] = dict(fields)
    descriptor["type"] = condition
    encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return IntegrityEvidenceRef(
        binding=OpaqueBinding(f"iv:{condition}:{run_id}:{subject}"),
        descriptor=OpaqueRef(encoded),
        head=OpaqueRef(detection_head),
    )


@dataclass(frozen=True)
class _Candidate:
    comment: UnverifiedComment
    payload: ChainPayload
    author_login: str


def _dedupe(
    comments: tuple[UnverifiedComment, ...], probes: Mapping[str, ProbeOutcome]
) -> dict[str, UnverifiedComment]:
    """comment_idでのdedupe（since境界の再配送吸収。updated_at最大の観測を採用する）。"""
    pool: dict[str, UnverifiedComment] = {}
    probed = tuple(outcome.comment for outcome in probes.values() if isinstance(outcome, ProbeFound))
    for comment in comments + probed:
        existing = pool.get(comment.comment_id)
        if existing is None or comment.updated_at > existing.updated_at:
            pool[comment.comment_id] = comment
    return pool


def verify_record_chain(
    comments: tuple[UnverifiedComment, ...],
    *,
    run_id: str,
    detection_head: str,
    producers: ProducerAllowlist,
    checkpoint: ChainCheckpoint | None,
    probes: Mapping[str, ProbeOutcome],
) -> ChainVerification:
    """指定runのcanonical record chainを再構築し、7条件を個別に検証する（pure）。

    commentsは当該runの取得窓を覆うconversation comment列（rawのまま）。probesは
    `probe_known_records`の結果（checkpointなしなら空mapping）。窓がcheckpointの
    既知IDを覆っていない場合はIdentityError（probe忘れ = 呼び出し誤り）。
    """
    pool = _dedupe(comments, probes)
    known_records = checkpoint.known_records if checkpoint is not None else ()
    for known in known_records:
        if known.comment_id not in pool and known.comment_id not in probes:
            raise IdentityError("probe", f"既知comment IDが取得窓にもprobe結果にもない: {known.comment_id}")

    violations: list[IntegrityEvidenceRef] = []

    # 分類（条件2） + actor検査（条件1）
    candidates: list[_Candidate] = []
    for comment in pool.values():
        if MARKER_TOKEN not in comment.body.upper():
            continue  # markerなしの通常comment（chain対象外）
        parsed = _parse_chain_payload(comment)
        if isinstance(parsed, str):
            violations.append(
                _violation(
                    "marker", run_id, f"c{comment.comment_id}", detection_head,
                    comment_id=comment.comment_id, reason=parsed,
                )
            )
            continue
        if parsed.run != run_id:
            continue  # 別runのchain（当該runの検証対象外）
        actor = resolve_actor(comment.author_login)
        if actor.klass is not ActorClass.USER or actor.login not in producers.logins:
            violations.append(
                _violation(
                    "actor", run_id, f"c{comment.comment_id}", detection_head,
                    comment_id=comment.comment_id, seq=parsed.seq,
                )
            )
            continue
        candidates.append(_Candidate(comment=comment, payload=parsed, author_login=actor.login))

    # 同一seqの重複canonical選択（ADR-0007 決定7の履行）
    by_seq: dict[int, list[_Candidate]] = {}
    for candidate in candidates:
        by_seq.setdefault(candidate.payload.seq, []).append(candidate)
    canonical: dict[int, _Candidate] = {}
    for seq, group in by_seq.items():
        signatures = {(entry.payload.key, entry.comment.body_hash) for entry in group}
        if len(signatures) > 1:
            violations.append(
                _violation(
                    "seqconflict", run_id, f"s{seq:08d}", detection_head,
                    seq=seq, comment_ids=sorted(entry.comment.comment_id for entry in group),
                )
            )
            continue
        # timeout再投稿による良性重複: created_at最小（tie: comment ID数値最小）を正とする
        canonical[seq] = min(
            group,
            key=lambda entry: (entry.comment.created_at, len(entry.comment.comment_id), entry.comment.comment_id),
        )

    # 編集検知（条件4）
    reliable: dict[int, _Candidate] = {}
    for seq, candidate in canonical.items():
        if candidate.comment.updated_at != candidate.comment.created_at:
            violations.append(
                _violation(
                    "edited", run_id, f"c{candidate.comment.comment_id}", detection_head,
                    comment_id=candidate.comment.comment_id, seq=seq,
                )
            )
            continue
        reliable[seq] = candidate

    # sequence gap（条件5 + AC-C06-07）。検査上限がNと観測最大で決まるため、Nより後の
    # tail truncationは範囲外 = 検出しない（AC-C06-09の負の保証が構造的に成立する）。
    # seqconflict / editedのseqは「recordの主張が観測された」ためgapとして二重報告しない
    observed = set(by_seq)
    high_water = checkpoint.high_water_mark if checkpoint is not None else 0
    max_seq = max(observed, default=0)
    for seq in range(1, max(max_seq, high_water) + 1):
        if seq not in observed:
            violations.append(
                _violation("gap", run_id, f"s{seq:08d}", detection_head, seq=seq, high_water=high_water)
            )

    # hash chain（条件6）。前seqが欠番・違反済みならskip（二重報告しない）
    chain_broken: set[int] = set()
    for seq in sorted(reliable):
        if seq < 2 or (seq - 1) not in reliable:
            continue
        expected = reliable[seq - 1].comment.body_hash
        if reliable[seq].payload.prev != expected:
            chain_broken.add(seq)
            violations.append(
                _violation(
                    "chain", run_id, f"s{seq:08d}", detection_head,
                    seq=seq, expected=expected, observed=reliable[seq].payload.prev,
                )
            )

    # 既知recordの実在（条件7 + AC-C06-08）と本文改変（条件3）
    tampered_ids: set[str] = set()
    for known in known_records:
        outcome = probes.get(known.comment_id)
        if isinstance(outcome, ProbeMissing):
            violations.append(
                _violation(
                    "missing", run_id, f"c{known.comment_id}", detection_head,
                    comment_id=known.comment_id, seq=known.seq,
                )
            )
            continue
        current = pool[known.comment_id]
        if current.body_hash != known.body_hash:
            tampered_ids.add(known.comment_id)
            violations.append(
                _violation(
                    "tamper", run_id, f"c{known.comment_id}", detection_head,
                    comment_id=known.comment_id, seq=known.seq,
                    expected=known.body_hash, observed=current.body_hash,
                )
            )

    records = tuple(
        VerifiedRecord(
            seq=seq,
            kind=candidate.payload.kind,
            key=candidate.payload.key,
            comment_id=candidate.comment.comment_id,
            url=candidate.comment.url,
            body=candidate.comment.body,
            body_hash=candidate.comment.body_hash,
            author_login=candidate.author_login,
            head_sha=candidate.payload.head,
            created_at=candidate.comment.created_at,
            projection=candidate.payload.projection,
        )
        for seq, candidate in sorted(reliable.items())
        if seq not in chain_broken and candidate.comment.comment_id not in tampered_ids
    )
    return ChainVerification(
        records=records,
        violations=canonicalize_integrity(tuple(violations)),
        max_seq=max_seq,
        assurance_high_water=high_water,
    )
