# SPDX-License-Identifier: Apache-2.0
"""C-08 step engine testの共有helper。

engineへ渡すport（fake）と、run directory・checkpoint・result fileの用意を集約する。
時刻とIDは製品codeが生成源を持たないため、testからも固定値を注入する。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from c02_support.helpers import REPRESENTATIVE
from c06_support.helpers import HEAD
from c07_support.helpers import NUMBER, REPOSITORY, RUN, checkpoint_payload, state_paths, verified_chain

from claude_code_codex_review_loop.domain.values import Awaiting, MachineState, RecordKind, State
from claude_code_codex_review_loop.identity.fs_permissions import replace_private_text
from claude_code_codex_review_loop.identity.record_chain import VerifiedRecord
from claude_code_codex_review_loop.schema import SchemaKind
from claude_code_codex_review_loop.state import StatePaths, checkpoint_path, run_directory, save_checkpoint
from claude_code_codex_review_loop.workflow import with_machine_state
from claude_code_codex_review_loop.workflow.ports import ActionContext

ISSUED_AT = "2026-08-25T09:00:00Z"
ACCEPTED_AT = "2026-08-25T09:05:00Z"
SPEAKER = "Claude Code"
MODEL = "claude-opus-5"
NEW_HEAD = "b" * 40
MAX_RESULT_BYTES = 65_536
RETRY_BUDGET = 2


class FakeIds:
    """`id_source`のfake（呼ばれた順に安定したIDを返す）。"""

    def __init__(self, prefix: str = "id") -> None:
        self.prefix = prefix
        self.issued: list[str] = []

    def __call__(self) -> str:
        value = f"{self.prefix}-{len(self.issued) + 1}"
        self.issued.append(value)
        return value


@dataclass
class FakePayloadPort:
    """action payloadを供給する（C-10 / C-11が本実装を持つ）。"""

    payload: Mapping[str, object] = field(default_factory=lambda: {"round": 1, "finding_ids": ["F-1"]})
    calls: list[ActionContext] = field(default_factory=list)

    def payload_for(self, context: ActionContext) -> Mapping[str, object]:
        self.calls.append(context)
        return self.payload


@dataclass
class FakeEvidencePort:
    """actionの根拠recordを供給する。"""

    records: Sequence[VerifiedRecord] = ()

    def evidence_for(self, context: ActionContext) -> Sequence[VerifiedRecord]:
        return self.records


@dataclass
class FakeRecordSource:
    """当該runの検証済みrecord列（binding採番とprev body hashに使う）。"""

    records: Sequence[VerifiedRecord] = ()

    def verified_records(self, run_id: str) -> Sequence[VerifiedRecord]:
        return self.records


@dataclass
class FakeBodyPort:
    """検証済みpayloadから公開本文を作る（kindごとの表現はC-10 / C-11の領域）。"""

    text: str = "修正を適用しました。"

    def body_for(self, kind: RecordKind, payload: Mapping[str, object]) -> str:
        return f"{self.text}（{kind.value}）"


def review_records() -> tuple[VerifiedRecord, ...]:
    """seq=1のREVIEW_RESULTだけを持つ検証済みchain（APPLY_FINDINGSの根拠）。"""
    return verified_chain([RecordKind.REVIEW_RESULT]).records


def machine_state(
    state: State = State.APPLYING_FIXES,
    awaiting: Awaiting | None = Awaiting.HOST_APPLY_FINDINGS,
) -> MachineState:
    return MachineState(state=state, awaiting=awaiting)


@dataclass(frozen=True)
class EngineEnv:
    """engineへ渡すpathと、seedしたcheckpointの位置。"""

    paths: StatePaths
    run_dir: Path
    checkpoint: Path


def seed(
    tmp_path: Path,
    *,
    state: MachineState | None = None,
    extra: Mapping[str, object] | None = None,
) -> EngineEnv:
    """state rootとcheckpointを用意する（checkpointは製品のsave経路で書く）。"""
    paths = state_paths(tmp_path)
    payload = checkpoint_payload()
    if state is not None:
        payload = with_machine_state(payload, state)
    if extra is not None:
        payload.update(extra)
    save_checkpoint(checkpoint_path(paths, RUN), payload)
    return EngineEnv(
        paths=paths, run_dir=run_directory(paths, RUN), checkpoint=checkpoint_path(paths, RUN)
    )


def fix_result_payload(*, pre: str = HEAD, pushed: str = NEW_HEAD) -> dict[str, object]:
    """代表的なFIX_RESULT payload（corpusのrepresentativeをheadだけ差し替える）。"""
    payload = dict(REPRESENTATIVE[SchemaKind.FIX_RESULT])
    payload["pre_head_sha"] = pre
    payload["pushed_head_sha"] = pushed
    return payload


def failure_payload(*, category: str = "TRANSIENT", action_kind: str = "APPLY_FINDINGS") -> dict[str, object]:
    payload = dict(REPRESENTATIVE[SchemaKind.HOST_FAILURE])
    payload["error_category"] = category
    payload["action_kind"] = action_kind
    return payload


def write_result(run_dir: Path, relative: str, payload: Mapping[str, object]) -> str:
    """result fileを作成者限定で書き、その内容hashを返す（既存があれば置き換える）。"""
    text = json.dumps(payload, ensure_ascii=False)
    target = run_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    replace_private_text(target, text)
    return hashlib.sha256(target.read_bytes()).hexdigest()


def submit_payload(
    *,
    action_id: str,
    nonce: str,
    result_hash: str,
    action_kind: str = "APPLY_FINDINGS",
    outcome: str = "COMPLETED",
    result_kind: str | None = "FIX_RESULT",
    error_category: str | None = None,
    run_id: str = RUN,
    head: str = HEAD,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 2,
        "run_id": run_id,
        "action_id": action_id,
        "action_kind": action_kind,
        "expected_head_sha": head,
        "nonce": nonce,
        "result_hash": result_hash,
        "outcome": outcome,
    }
    if result_kind is not None:
        payload["result_kind"] = result_kind
    if error_category is not None:
        payload["error_category"] = error_category
    return payload


def raw(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


__all__ = [
    "ACCEPTED_AT",
    "HEAD",
    "ISSUED_AT",
    "MAX_RESULT_BYTES",
    "MODEL",
    "NEW_HEAD",
    "NUMBER",
    "REPOSITORY",
    "RETRY_BUDGET",
    "RUN",
    "SPEAKER",
    "EngineEnv",
    "FakeBodyPort",
    "FakeEvidencePort",
    "FakeIds",
    "FakePayloadPort",
    "FakeRecordSource",
    "failure_payload",
    "fix_result_payload",
    "machine_state",
    "raw",
    "review_records",
    "seed",
    "submit_payload",
    "write_result",
]
