# SPDX-License-Identifier: Apache-2.0
"""runtime（PR-3b1）のtest支援。

fake GitHubは**fake gh実行file**（`c05_support`）を使う。境界が`gh`実行fileなので、
C-05の取得からC-06の検証までは製品codeがそのまま走る。fake active hostも実processを
起動せず、同一process内で`HostPort`を実装する（AC-C08-02: subprocess起動0）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from c05_support.helpers import make_context, read_state, seed_state
from c06_support.helpers import HEAD, PRODUCER, seed_dict
from c07_support.helpers import (
    NUMBER,
    REPOSITORY,
    RUN,
    chain_comments_of,
    checkpoint_payload,
    state_paths,
)

from claude_code_codex_review_loop.domain.values import Awaiting, MachineState, RecordKind
from claude_code_codex_review_loop.identity.fs_permissions import replace_private_text
from claude_code_codex_review_loop.runtime import (
    DriveClock,
    HostWork,
    PortSet,
    PortUnavailableError,
    SessionConfig,
    UserInputBody,
    default_ports,
    read_session_config,
    write_session_config,
)
from claude_code_codex_review_loop.state import StatePaths, checkpoint_path, run_directory, save_checkpoint
from claude_code_codex_review_loop.workflow import (
    AwaitUser,
    HostActionIssued,
    with_machine_state,
)
from claude_code_codex_review_loop.workflow.ports import ActionContext

from .helpers import (
    MODEL,
    SPEAKER,
    USER_SPEAKER,
    FakeIds,
    FakeStopPort,
    gate_answer_payload,
    user_record_payload,
    user_submit_payload,
)

ISSUED_AT = "2026-08-26T09:00:00Z"
ACCEPTED_AT = "2026-08-26T09:05:00Z"


def session_payload(directory: Path, **overrides: object) -> dict[str, object]:
    """fake ghを指すsession config payload（全fieldを明示する）。"""
    context = make_context(directory)
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": RUN,
        "repository": REPOSITORY,
        "number": NUMBER,
        "head_sha": HEAD,
        "gh_command": list(context.gh_command),
        "gh_workdir": str(context.workdir),
        "gh_timeout_ms": 30_000,
        "gh_grace_ms": 1_000,
        "gh_env": dict(context.env),
        "retry_max_attempts": 2,
        "retry_backoff_ms": 0,
        "retry_max_wait_ms": 2_000,
        "search_since": None,
        "search_attempts": 1,
        "search_backoff_ms": 0,
        "search_max_pages": 5,
        "detection_head": HEAD,
        "producer_logins": [PRODUCER],
        "max_result_bytes": 65_536,
        "retry_budget": 2,
        "speaker": SPEAKER,
        "model": MODEL,
        "user_speaker": USER_SPEAKER,
        "halt_grace_ms": 1_500,
    }
    payload.update(overrides)
    return payload


def seed_comments(directory: Path, comments: Sequence[object]) -> None:
    """fake GitHubのcomment列を丸ごと置き換える（削除・改竄の再現に使う）。"""
    seed_state(directory, comments=list(comments))


@dataclass(frozen=True)
class RuntimeEnv:
    """runtimeを動かす一式（state root、run directory、fake ghのstate）。"""

    paths: StatePaths
    run_dir: Path
    directory: Path
    config: SessionConfig

    def ports(self) -> PortSet:
        return default_ports(self.paths, self.config)

    def comments(self) -> list[object]:
        entries = read_state(self.directory)["comments"]
        assert isinstance(entries, list)
        return entries

    def seed(self, comments: Sequence[object]) -> None:
        """fake GitHub側のcomment列を差し替える。"""
        seed_comments(self.directory, comments)

    def records_for(self, binding: str) -> int:
        return sum(1 for item in self.comments() if binding in str(item.get("body", "")))  # type: ignore[union-attr]


def runtime_env(
    tmp_path: Path,
    *,
    state: MachineState,
    seeded: Sequence[RecordKind] = (),
    config_overrides: Mapping[str, object] | None = None,
    extra: Mapping[str, object] | None = None,
) -> RuntimeEnv:
    """checkpointとsession configをrun directoryへ用意する。"""
    directory = tmp_path / "gh"
    directory.mkdir(parents=True, exist_ok=True)
    posted = chain_comments_of(list(seeded))
    seed_state(directory, comments=[seed_dict(comment, issue=NUMBER) for comment in posted])
    paths = state_paths(tmp_path)
    payload = with_machine_state(checkpoint_payload(), state)
    if extra is not None:
        payload.update(extra)
    save_checkpoint(checkpoint_path(paths, RUN), payload)
    write_session_config(paths, RUN, session_payload(directory, **dict(config_overrides or {})))
    config = read_session_config(paths, RUN)
    assert isinstance(config, SessionConfig), config
    return RuntimeEnv(
        paths=paths, run_dir=run_directory(paths, RUN), directory=directory, config=config
    )


def fixed_clock(prefix: str = "rid") -> DriveClock:
    return DriveClock(
        id_source=FakeIds(prefix),
        issued_at=lambda: ISSUED_AT,
        accepted_at=lambda: ACCEPTED_AT,
    )


QUESTION_COMMENT_ID = "c-2001"


@dataclass
class AgentRecordBody:
    """agent recordの本文だけを補うport（user-input recordは製品実装が処理する）。

    C-08が本文を**選べる**のは転記recordだけである。agent recordの表現はC-10 / C-11の
    領域なので、fail closedになったkindだけをfakeが引き取る。
    """

    text: str = "回答です。"

    def body_for(self, kind: RecordKind, payload: Mapping[str, object]) -> str:
        try:
            return UserInputBody().body_for(kind, payload)
        except PortUnavailableError:
            return f"{self.text}（{kind.value}）"


@dataclass
class FakeActionPayloads:
    """action種別ごとの入力payload（本実装はC-10 / C-11）。"""

    payloads: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def payload_for(self, context: ActionContext) -> Mapping[str, object]:
        self.calls.append(context.action.value)
        return self.payloads[context.action.value]


@dataclass
class FakeActiveHost:
    """同一process内でhost作業を行うfake（AC-C08-01 / 02）。

    **subprocessを起動せず、キー入力も注入しない**。instanceが1つのまま複数roundを扱えることが
    「同一sessionのcontextを維持している」ことの観測点になる。
    """

    env: RuntimeEnv
    user_kinds: Sequence[RecordKind] = ()
    action_results: Mapping[str, tuple[RecordKind, Mapping[str, object]]] = field(
        default_factory=dict
    )
    session_id: str = "active-session-1"
    executed: list[str] = field(default_factory=list)
    spawned: int = 0
    key_injections: int = 0
    _user_turns: int = 0

    def execute(self, work: HostWork) -> bytes:
        if isinstance(work, AwaitUser):
            return self._user(work)
        return self._action(work)

    def _user(self, work: AwaitUser) -> bytes:
        kind = (
            self.user_kinds[self._user_turns] if self.user_kinds else RecordKind.USER_CANCEL
        )
        self._user_turns += 1
        payload = user_record_payload(kind)
        digest = _write(work.result_path, payload)
        self.executed.append(f"user:{kind.value}")
        return _raw(
            user_submit_payload(
                request_id=work.request.request_id,
                nonce=work.request.nonce,
                result_hash=digest,
                awaiting=work.awaiting,
                result_kind=kind.value,
            )
        )

    def _action(self, work: HostActionIssued) -> bytes:
        from .helpers import submit_payload

        kind, payload = self.action_results[work.action.action_kind]
        digest = _write(work.result_path, payload)
        self.executed.append(f"action:{work.action.action_kind}")
        return _raw(
            submit_payload(
                action_id=work.action.action_id,
                nonce=work.action.nonce,
                result_hash=digest,
                action_kind=work.action.action_kind,
                result_kind=kind.value,
            )
        )


def round_ports(env: RuntimeEnv, **overrides: object) -> PortSet:
    """1 roundを通すport束。

    導出できる4 portは**製品実装のまま**使い、C-10 / C-11が持つ2つ——action payloadと
    agent recordの本文——だけをfakeで補う。fakeの範囲がそのまま「まだ実装が無い範囲」である。
    """
    ports = replace(
        default_ports(env.paths, env.config),
        payload=FakeActionPayloads({"ANSWER_GATE_QUESTION": {"question_comment_id": QUESTION_COMMENT_ID}}),
        body=AgentRecordBody(),
    )
    return replace(ports, **overrides) if overrides else ports


def stopping_ports(env: RuntimeEnv, **overrides: object) -> PortSet:
    """停止portだけをfakeにした束。

    台帳へ置くrefはtestが組み立てた値で、**実在するprocessを指さない**。製品の
    `TreeStopper`へ渡すと現在のOSのprocess APIを実際に叩き、他OSのref種別は
    `ref_mismatch`で拒否される（C-03の仕様）。実停止の挙動はC-03のtestが担保する。
    """
    ports = replace(default_ports(env.paths, env.config), stop=FakeStopPort())
    return replace(ports, **overrides) if overrides else ports


def gate_host(env: RuntimeEnv) -> FakeActiveHost:
    """merge gateから3 round回るfake host（質問 -> 回答 -> cancel）。"""
    return FakeActiveHost(
        env=env,
        user_kinds=(RecordKind.GATE_QUESTION, RecordKind.USER_CANCEL),
        action_results={"ANSWER_GATE_QUESTION": (RecordKind.GATE_ANSWER, gate_answer_payload())},
    )


def user_submit_for(
    envelope_path: str | Path, result_path: str | Path, kind: RecordKind = RecordKind.USER_CANCEL
) -> dict[str, object]:
    """発行済み`USER_REQUEST`へ応答する（別processが出したrequestにも使える）。

    引数はentry pointの出力そのもの（path）で、request IDとnonceは**envelope fileから
    読む**。processを跨いでも同じ手順で応答できることが、cross-process resumeの前提である。
    """
    envelope = json.loads(Path(envelope_path).read_text(encoding="utf-8"))
    payload = user_record_payload(kind)
    digest = _write(Path(result_path), payload)
    return user_submit_payload(
        request_id=str(envelope["request_id"]),
        nonce=str(envelope["nonce"]),
        result_hash=digest,
        awaiting=Awaiting(envelope["awaiting"]),
        result_kind=kind.value,
    )


def _write(path: Path, payload: Mapping[str, object]) -> str:
    text = json.dumps(dict(payload), ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_private_text(path, text)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _raw(payload: Mapping[str, object]) -> bytes:
    return json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")


__all__ = [
    "ACCEPTED_AT",
    "ISSUED_AT",
    "FakeActionPayloads",
    "FakeActiveHost",
    "AgentRecordBody",
    "FakeIds",
    "RuntimeEnv",
    "QUESTION_COMMENT_ID",
    "fixed_clock",
    "gate_host",
    "round_ports",
    "stopping_ports",
    "runtime_env",
    "seed_comments",
    "session_payload",
    "user_submit_for",
]
