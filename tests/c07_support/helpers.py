# SPDX-License-Identifier: Apache-2.0
"""C-07 local state testの共有helper。

state rootの用意と、最小のcheckpoint / lock payloadを組み立てる。時刻は注入引数
（製品codeが時刻sourceを持たないため、testでも固定値を渡す）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from c06_support.helpers import HEAD as C06_HEAD
from c06_support.helpers import PRODUCER, make_comment, marker_payload

from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.identity import ProducerAllowlist, verify_record_chain
from claude_code_codex_review_loop.identity.record_chain import ChainVerification
from claude_code_codex_review_loop.schema.projection import PROJECTION_KEYS
from claude_code_codex_review_loop.state import StatePaths, prepare_state_root
from claude_code_codex_review_loop.transport.conversation import UnverifiedComment, body_hash_of
from claude_code_codex_review_loop.transport.marker import attach_marker

RUN = "run-1"
REPOSITORY = "owner/repo"
NUMBER = 12
HEAD = "a" * 40
ACQUIRED_AT = "2026-08-23T10:00:00Z"


def state_paths(tmp_path: Path, *, name: str = "state") -> StatePaths:
    """tmp_path配下へstate rootを用意する（作成者限定で作られる）。"""
    return prepare_state_root(tmp_path.resolve() / name)


def checkpoint_payload(**overrides: object) -> dict[str, object]:
    """schema検証を通る最小のcheckpoint payload。"""
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": RUN,
        "repository": REPOSITORY,
        "number": NUMBER,
    }
    payload.update(overrides)
    return payload


def lock_payload(**overrides: object) -> dict[str, object]:
    """schema検証を通る最小のlock payload（破損caseの素材にも使う）。"""
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": RUN,
        "repository": REPOSITORY,
        "number": NUMBER,
        "pid": 424242,
        "host": "test-host",
        "acquired_at": ACQUIRED_AT,
    }
    payload.update(overrides)
    return payload


def approved_review_payload(head: str = C06_HEAD, *, round_number: int = 1) -> dict[str, object]:
    """blocking findingの無いAPPROVED review（representative corpusはCHANGES_REQUESTED）。"""
    return {
        "schema_version": 1,
        "target_head_sha": head,
        "round": round_number,
        "verdict": "APPROVED",
        "findings": [],
        "verification_runs": [{"command": "pytest -q", "result": "pass"}],
    }


def chain_comments_of(
    kinds: Sequence[RecordKind],
    *,
    run_id: str = RUN,
    head: str = C06_HEAD,
    author: str | None = PRODUCER,
    start_id: int = 2001,
    payloads: Mapping[int, dict[str, object]] | None = None,
    heads: Mapping[int, str] | None = None,
) -> tuple[UnverifiedComment, ...]:
    """指定kind列（seq=1..n）の正規chainをUnverifiedCommentとして組み立てる。

    kindとhead（seq単位）を混在させられる点がc06_supportの`chain_comments`と違う。
    実際のrunではFIX_RESULTでheadが変わるため、head跨ぎの履歴を1本のhash chainとして
    再現できる必要がある。marker payloadとbindingは製品側の導出関数で作る。
    """
    comments: list[UnverifiedComment] = []
    prev: str | None = None
    for index, kind in enumerate(kinds):
        text = f"record {index + 1}"
        record_head = head if heads is None else heads.get(index + 1, head)
        payload = marker_payload(
            kind=kind,
            run_id=run_id,
            head=record_head,
            seq=index + 1,
            prev=prev,
            body=text,
            payload=None if payloads is None else payloads.get(index + 1),
        )
        body = attach_marker(text, payload)
        comments.append(
            make_comment(
                start_id + index, body, author=author, created_at=f"2026-08-24T10:00:{index:02d}Z"
            )
        )
        prev = body_hash_of(body)
    return tuple(comments)


def verified_chain(
    kinds: Sequence[RecordKind],
    *,
    run_id: str = RUN,
    head: str = C06_HEAD,
    author: str | None = PRODUCER,
    start_id: int = 2001,
    payloads: Mapping[int, dict[str, object]] | None = None,
    heads: Mapping[int, str] | None = None,
) -> ChainVerification:
    """検証済みchain（C-06の製品関数を通す。fixtureへ検証結果を直書きしない）。"""
    comments = chain_comments_of(
        kinds,
        run_id=run_id,
        head=head,
        author=author,
        start_id=start_id,
        payloads=payloads,
        heads=heads,
    )
    return verify_record_chain(
        comments,
        run_id=run_id,
        detection_head=head,
        producers=ProducerAllowlist(logins=frozenset({PRODUCER})),
        checkpoint=None,
        probes={},
    )


@dataclass(frozen=True)
class PendingFixture:
    """中断中recordの素材（checkpointのtransactionと、投稿されるはずの完成形本文）。"""

    transaction: dict[str, object]
    body: str
    binding: str
    comment: UnverifiedComment


def pending_fixture(
    *,
    seq: int,
    prev: str | None = None,
    kind: RecordKind = RecordKind.CLARIFICATION_QUESTION,
    run_id: str = RUN,
    head: str = C06_HEAD,
    text: str | None = None,
    payload: dict[str, object] | None = None,
    include_body_hash: bool = True,
) -> PendingFixture:
    """製品側の導出関数だけでtransactionと完成形本文を作る（期待値を直書きしない）。"""
    body_text = text if text is not None else f"record {seq}"
    marker = marker_payload(
        kind=kind, run_id=run_id, head=head, seq=seq, prev=prev, body=body_text, payload=payload
    )
    body = attach_marker(body_text, marker)
    projection = {key: value for key, value in marker.items() if key in PROJECTION_KEYS}
    transaction: dict[str, object] = {
        "binding": str(marker["key"]),
        "kind": kind.value,
        "seq": seq,
        "head_sha": head,
        "payload_hash": str(projection["pay"]),
        "body": body_text,
        "projection": projection,
    }
    if include_body_hash:
        transaction["body_hash"] = body_hash_of(body)
    return PendingFixture(
        transaction=transaction,
        body=body,
        binding=str(marker["key"]),
        comment=make_comment(
            2000 + seq, body, created_at=f"2026-08-24T10:00:{seq - 1:02d}Z"
        ),
    )


def conversation_section(comments: Sequence[UnverifiedComment]) -> dict[str, object]:
    """checkpointの`conversation`（high-water markと既知record）を組み立てる。"""
    return {
        "high_water_mark": len(comments),
        "records": [
            {"comment_id": comment.comment_id, "seq": index + 1, "body_hash": comment.body_hash}
            for index, comment in enumerate(comments)
        ],
    }
