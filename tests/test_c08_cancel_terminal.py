# SPDX-License-Identifier: Apache-2.0
"""cancel完走シナリオ（Phase 8 PR-3a。ADR-0019）。

Phase 8で正当に到達できるterminalは`CANCELLED`だけである（`MERGED`へ至る`MergeConfirmed`系
eventはC-13の責務で、engineに入口が無い）。この経路を**製品関数だけ**で1本通す。

```
AWAIT_USER -> USER_CANCEL を submit -> record永続化 -> HaltRun
           -> halt（process tree停止 -> checkpoint保存） -> CANCELLED
```

PR-2cは`USER_CANCEL`をどのuser input待ちでも受理していたが、永続化した先で
`CancellingProcedure`を保存できず止まっていた。ここはその行き止まりが繋がったことを固定する。
実GitHubへは接続せず、実processも起動しない。
"""

from __future__ import annotations

import pytest
from c05_support.helpers import read_state
from c07_support.helpers import RUN
from c08_support.helpers import (
    GRACE_SECONDS,
    FakeEvidencePort,
    FakeStopPort,
    process_group_ref,
    raw,
    user_env,
    user_record_payload,
    user_submit_payload,
    with_active_trees,
    write_result,
)

from claude_code_codex_review_loop.domain.commands import HaltRun
from claude_code_codex_review_loop.domain.states import State
from claude_code_codex_review_loop.domain.values import (
    Awaiting,
    CancellingProcedure,
    MachineState,
    OpaqueBinding,
    RecordKind,
)
from claude_code_codex_review_loop.state import (
    CheckpointLoaded,
    checkpoint_path,
    load_checkpoint,
    save_checkpoint,
)
from claude_code_codex_review_loop.workflow import (
    AwaitUser,
    EngineStopped,
    HaltCompleted,
    HaltRequired,
    RecordPersisted,
    Terminal,
    UserInputAccepted,
    advance,
    halt,
    persist,
    read_machine_state,
    submit,
)

AWAITINGS = [Awaiting.USER_INPUT_DECISION, Awaiting.USER_INPUT_GATE, Awaiting.USER_INPUT_PERMISSION]
SEEDED = {
    Awaiting.USER_INPUT_DECISION: (RecordKind.DECISION_BRIEF,),
    Awaiting.USER_INPUT_GATE: (RecordKind.FINAL_REPORT,),
    Awaiting.USER_INPUT_PERMISSION: (RecordKind.PERMISSION_BLOCK,),
}


def _payload(env) -> dict[str, object]:
    loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
    assert isinstance(loaded, CheckpointLoaded)
    return loaded.payload


def _register_tree(env) -> None:
    """走っているagent process treeを台帳へ置く（書き手はC-09 / headless adapter）。"""
    save_checkpoint(
        checkpoint_path(env.paths, RUN), with_active_trees(_payload(env), [process_group_ref()])
    )


def _cancel(env, issued: AwaitUser):
    payload = user_record_payload(RecordKind.USER_CANCEL)
    digest = write_result(env.run_dir, issued.request.result_path, payload)
    envelope = user_submit_payload(
        request_id=issued.request.request_id,
        nonce=issued.request.nonce,
        result_hash=digest,
        awaiting=env.awaiting,
        result_kind=RecordKind.USER_CANCEL.value,
    )
    return submit(raw(envelope), **env.submit_kwargs())


@pytest.mark.parametrize("awaiting", AWAITINGS, ids=lambda a: a.value)
def test_cancel_runs_to_the_cancelled_terminal(tmp_path, awaiting: Awaiting) -> None:
    """どのuser input待ちからでもcancelがterminalまで到達する。"""
    env = user_env(tmp_path, awaiting=awaiting, seeded=SEEDED[awaiting])
    _register_tree(env)

    issued = advance(**env.advance_kwargs(evidence_port=FakeEvidencePort(())))
    assert isinstance(issued, AwaitUser)

    accepted = _cancel(env, issued)
    assert isinstance(accepted, UserInputAccepted)

    persisted = persist(**env.persist_kwargs())
    assert isinstance(persisted, RecordPersisted), persisted
    binding = OpaqueBinding(accepted.transaction.binding)  # type: ignore[union-attr]
    assert persisted.machine_state.procedure == CancellingProcedure(attempt_binding=binding)
    assert persisted.commands == (HaltRun(binding),)

    requested = advance(**env.advance_kwargs(evidence_port=FakeEvidencePort(())))
    assert isinstance(requested, HaltRequired)
    assert requested.procedure == persisted.machine_state.procedure

    port = FakeStopPort()
    stopped = halt(
        paths=env.paths,
        run_id=RUN,
        repository=env.repo.owner + "/" + env.repo.name,
        number=12,
        stop_port=port,
        grace_seconds=GRACE_SECONDS,
    )
    assert isinstance(stopped, HaltCompleted)
    assert stopped.machine_state == MachineState(state=State.CANCELLED)
    assert port.calls == [(process_group_ref(), GRACE_SECONDS)]

    assert read_machine_state(_payload(env)) == MachineState(state=State.CANCELLED)
    assert advance(**env.advance_kwargs(evidence_port=FakeEvidencePort(()))) == Terminal(
        state=State.CANCELLED
    )


def test_the_cancel_record_is_on_github_exactly_once(tmp_path) -> None:
    """cancelもcanonical recordとして1件だけ残る（未永続化の入力を根拠にしない）。"""
    env = user_env(tmp_path)
    issued = advance(**env.advance_kwargs())
    assert isinstance(issued, AwaitUser)
    accepted = _cancel(env, issued)
    assert isinstance(accepted, UserInputAccepted) and accepted.transaction is not None
    assert isinstance(persist(**env.persist_kwargs()), RecordPersisted)
    comments = read_state(env.directory)["comments"]
    assert isinstance(comments, list)
    matching = [c for c in comments if accepted.transaction.binding in str(c.get("body", ""))]
    assert len(matching) == 1


def test_halting_before_the_record_is_persisted_stops(tmp_path) -> None:
    """手続きへ入る前にhaltを呼んでも、停止したことにしない。"""
    env = user_env(tmp_path)
    issued = advance(**env.advance_kwargs())
    assert isinstance(issued, AwaitUser)
    assert isinstance(_cancel(env, issued), UserInputAccepted)
    outcome = halt(
        paths=env.paths,
        run_id=RUN,
        repository=env.repo.owner + "/" + env.repo.name,
        number=12,
        stop_port=FakeStopPort(),
        grace_seconds=GRACE_SECONDS,
    )
    assert isinstance(outcome, EngineStopped) and outcome.code == "no_halt_procedure"
