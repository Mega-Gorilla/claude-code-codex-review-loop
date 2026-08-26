# SPDX-License-Identifier: Apache-2.0
"""R-04: 代表的な1 roundの通し（Phase 8 PR-3b1。ADR-0020）。

fake GitHub（fake gh実行file）とfake hostで、merge gateから始まる3 roundを通す。

```
AWAIT_USER(gate) -> GATE_QUESTION -> HOST_ACTION(ANSWER_GATE_QUESTION)
                 -> GATE_ANSWER   -> AWAIT_USER(gate) -> USER_CANCEL -> CANCELLED
```

境界が`gh`実行fileなので、**C-05の投稿・取得からC-06のchain検証までは製品codeが走る**。
観測するのは、hostへ渡すHOST_ACTIONの粒度と、各turnがGitHubへ永続化されてから次の作業が
出ること（GitHub canonical）である。
"""

from __future__ import annotations

import json
from pathlib import Path

from c07_support.helpers import RUN
from c08_support.helpers import user_machine_state
from c08_support.runtime import (
    ISSUED_AT,
    QUESTION_COMMENT_ID,
    FakeIds,
    RuntimeEnv,
    gate_host,
    round_ports,
    runtime_env,
)

from claude_code_codex_review_loop.domain.values import Awaiting, RecordKind, State
from claude_code_codex_review_loop.runtime import PortSet, StepResult, step, submit_result
from claude_code_codex_review_loop.workflow import AwaitUser, EngineStopped, HostActionIssued, Terminal

ACCEPTED_AT = "2026-08-26T09:05:00Z"


def _round_env(tmp_path: Path) -> RuntimeEnv:
    return runtime_env(
        tmp_path,
        state=user_machine_state(Awaiting.USER_INPUT_GATE),
        seeded=(RecordKind.FINAL_REPORT,),
    )


def _step(env: RuntimeEnv, ports: PortSet, prefix: str) -> StepResult:
    return step(
        paths=env.paths,
        config=env.config,
        ports=ports,
        id_source=FakeIds(prefix),
        issued_at=ISSUED_AT,
    )


def _submit(env: RuntimeEnv, ports: PortSet, raw: bytes) -> object:
    outcome = submit_result(
        raw, paths=env.paths, config=env.config, ports=ports, accepted_at=ACCEPTED_AT
    )
    assert not isinstance(outcome, EngineStopped), outcome
    return outcome


def _envelope(path: Path | str) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _kinds(env: RuntimeEnv, ports: PortSet) -> list[RecordKind]:
    chain = ports.records.chain(RUN)
    assert chain.is_intact, chain.violations
    return [record.kind for record in sorted(chain.records, key=lambda item: item.seq)]


def test_one_round_runs_through_the_real_transport(tmp_path: Path) -> None:
    """gate質問 -> 回答 -> cancelを、各turnの永続化を確かめながら通す。"""
    env = _round_env(tmp_path)
    ports = round_ports(env)
    host = gate_host(env)
    assert _kinds(env, ports) == [RecordKind.FINAL_REPORT]

    # --- round 1: ユーザーへ質問を求める --------------------------------------
    first = _step(env, ports, "req")
    assert isinstance(first.outcome, AwaitUser)
    request = _envelope(first.outcome.envelope_path)
    assert request["awaiting"] == Awaiting.USER_INPUT_GATE.value
    # 受理するrecord種別と根拠はregistryの写しで、hostが推測する余地を残さない
    assert sorted(str(kind) for kind in request["accepted_result_kinds"]) == [  # type: ignore[union-attr]
        "GATE_CHANGES",
        "GATE_QUESTION",
        "MERGE_APPROVAL",
        "USER_CANCEL",
    ]
    assert [entry["head_sha"] for entry in request["verified_records"]] == [  # type: ignore[union-attr, index]
        env.config.head_sha
    ]
    assert request["since_seq"] == 1

    _submit(env, ports, host.execute(first.outcome))
    # 次の作業を出す前にGitHubへ永続化されている（GitHub canonical）
    persisted = _step(env, ports, "act")
    assert _kinds(env, ports) == [RecordKind.FINAL_REPORT, RecordKind.GATE_QUESTION]

    # --- round 2: hostへ回答を依頼する ----------------------------------------
    assert isinstance(persisted.outcome, HostActionIssued)
    action = _envelope(persisted.outcome.envelope_path)
    assert action["action_kind"] == "ANSWER_GATE_QUESTION"
    # 粒度: 1 actionは「何を」「どのheadで」「何を根拠に」だけを渡し、手順を渡さない
    assert action["payload"] == {"question_comment_id": QUESTION_COMMENT_ID}
    assert action["expected_head_sha"] == env.config.head_sha
    assert [entry["comment_id"] for entry in action["verified_records"]] == [  # type: ignore[union-attr, index]
        _question_comment_id(env, ports)
    ]

    _submit(env, ports, host.execute(persisted.outcome))
    answered = _step(env, ports, "req2")
    assert _kinds(env, ports)[-1] is RecordKind.GATE_ANSWER

    # --- round 3: gateへ戻り、cancelで終端まで進む ------------------------------
    assert isinstance(answered.outcome, AwaitUser)
    assert answered.outcome.awaiting is Awaiting.USER_INPUT_GATE
    # 新しいawaiting instanceなので、since_seqが前回より進んでいる
    assert _envelope(answered.outcome.envelope_path)["since_seq"] == 3

    _submit(env, ports, host.execute(answered.outcome))
    done = _step(env, ports, "end")
    assert done.outcome == Terminal(state=State.CANCELLED)
    assert _kinds(env, ports) == [
        RecordKind.FINAL_REPORT,
        RecordKind.GATE_QUESTION,
        RecordKind.GATE_ANSWER,
        RecordKind.USER_CANCEL,
    ]
    assert host.executed == [
        "user:GATE_QUESTION",
        "action:ANSWER_GATE_QUESTION",
        "user:USER_CANCEL",
    ]


def _question_comment_id(env: RuntimeEnv, ports: PortSet) -> str:
    chain = ports.records.chain(RUN)
    question = next(record for record in chain.records if record.kind is RecordKind.GATE_QUESTION)
    return question.comment_id
