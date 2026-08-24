# SPDX-License-Identifier: Apache-2.0
"""C-07 local state testの共有helper。

state rootの用意と、最小のcheckpoint / lock payloadを組み立てる。時刻は注入引数
（製品codeが時刻sourceを持たないため、testでも固定値を渡す）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from c06_support.helpers import HEAD as C06_HEAD
from c06_support.helpers import PRODUCER, make_comment, marker_payload

from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.identity import ProducerAllowlist, verify_record_chain
from claude_code_codex_review_loop.identity.record_chain import ChainVerification
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
