# SPDX-License-Identifier: Apache-2.0
"""ユーザー入力の転記1往復とcrash窓（Phase 8 PR-2c。ADR-0018）。

`advance -> submit -> persist`を**製品経路だけ**でfake gh越しに通し、ユーザー入力が
canonical recordとしてGitHubへ載り、検証を経てstateが進むことを固定する。転記経路は
PR-2bの汎用`PersistRecord`をそのまま使うため、投稿・確認・検証のcrash窓も同じ性質
（**当該recordが常に1件**）を保つ。実GitHubへは接続しない。
"""

from __future__ import annotations

import pytest
from c05_support.helpers import make_policy, read_state
from c07_support.helpers import RUN
from c08_support.helpers import (
    HEAD,
    raw,
    user_env,
    user_record_payload,
    user_submit_payload,
    write_result,
)

from claude_code_codex_review_loop.domain.states import State
from claude_code_codex_review_loop.domain.values import Awaiting, RecordKind
from claude_code_codex_review_loop.state import (
    CheckpointLoaded,
    checkpoint_path,
    load_checkpoint,
    save_checkpoint,
)
from claude_code_codex_review_loop.transport.gh import TransportError
from claude_code_codex_review_loop.workflow import (
    AwaitUser,
    EngineStopped,
    RecordPersisted,
    UserInputAccepted,
    UserInputReplayed,
    advance,
    persist,
    read_user_request,
    submit,
)

GATE = Awaiting.USER_INPUT_GATE


def _payload(env) -> dict[str, object]:
    loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
    assert isinstance(loaded, CheckpointLoaded)
    return loaded.payload


def _records_for(env, binding: str) -> int:
    """当該bindingのrecordがGitHubに何件あるか（重複投稿の検出）。"""
    comments = read_state(env.directory)["comments"]
    assert isinstance(comments, list)
    return sum(1 for comment in comments if binding in str(comment.get("body", "")))


def _round_trip(env, kind: RecordKind) -> tuple[UserInputAccepted, dict[str, object]]:
    """requestを払い出し、ユーザー入力をsubmitして`RecordProduced`まで進める。"""
    issued = advance(**env.advance_kwargs())
    assert isinstance(issued, AwaitUser), issued
    digest = write_result(env.run_dir, issued.request.result_path, user_record_payload(kind))
    envelope = user_submit_payload(
        request_id=issued.request.request_id,
        nonce=issued.request.nonce,
        result_hash=digest,
        awaiting=env.awaiting,
        result_kind=kind.value,
    )
    accepted = submit(raw(envelope), **env.submit_kwargs())
    assert isinstance(accepted, UserInputAccepted), accepted
    return accepted, envelope


class TestTranscript:
    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            (RecordKind.GATE_QUESTION, State.READY_FOR_HUMAN_MERGE),
            (RecordKind.MERGE_APPROVAL, State.MERGING),
            (RecordKind.GATE_CHANGES, State.CHANGES_REQUESTED),
        ],
        ids=lambda value: getattr(value, "value", value),
    )
    def test_the_input_becomes_a_verified_record(self, tmp_path, kind, expected) -> None:
        env = user_env(tmp_path)
        accepted, _ = _round_trip(env, kind)
        assert accepted.transaction is not None
        outcome = persist(**env.persist_kwargs())
        assert isinstance(outcome, RecordPersisted), outcome
        assert outcome.posted is True
        assert outcome.record.kind is kind
        assert outcome.record.head_sha == HEAD
        assert outcome.machine_state.state is expected
        assert outcome.machine_state.pending_record is None
        assert _records_for(env, accepted.transaction.binding) == 1

    def test_the_posted_body_names_the_user_and_the_route(self, tmp_path) -> None:
        """転記recordは発言者と入力経路を明示する（TE 5.4の記録要件）。"""
        env = user_env(tmp_path)
        _round_trip(env, RecordKind.GATE_QUESTION)
        outcome = persist(**env.persist_kwargs())
        assert isinstance(outcome, RecordPersisted)
        assert outcome.record.body.startswith("**User**（入力経路: host_transcript）")

    def test_the_transaction_is_consumed(self, tmp_path) -> None:
        env = user_env(tmp_path)
        _round_trip(env, RecordKind.GATE_QUESTION)
        assert "transaction" in _payload(env)
        assert isinstance(persist(**env.persist_kwargs()), RecordPersisted)
        assert "transaction" not in _payload(env)

    def test_the_wait_is_closed_after_the_record_is_verified(self, tmp_path) -> None:
        """gate質問の転記後はgateを維持したままhostの回答待ちへ移り、requestは残らない。"""
        env = user_env(tmp_path)
        _round_trip(env, RecordKind.GATE_QUESTION)
        outcome = persist(**env.persist_kwargs())
        assert isinstance(outcome, RecordPersisted)
        assert outcome.machine_state.awaiting is Awaiting.HOST_ANSWER_GATE_QUESTION
        assert read_user_request(_payload(env)) is None


class TestCrashWindows:
    """転記路の中断窓。どこで落ちても当該recordは1件（ADR-0017 決定17と同じ性質）。"""

    def test_crash_before_posting_posts_once(self, tmp_path) -> None:
        """U1: submitは受理済み・投稿前。再開が投稿して1件になる。"""
        env = user_env(tmp_path)
        accepted, _ = _round_trip(env, RecordKind.GATE_QUESTION)
        assert accepted.transaction is not None
        assert _records_for(env, accepted.transaction.binding) == 0
        outcome = persist(**env.persist_kwargs())
        assert isinstance(outcome, RecordPersisted) and outcome.posted is True
        assert _records_for(env, accepted.transaction.binding) == 1

    @pytest.mark.parametrize("position", [1, 2, 3, 4])
    def test_crash_with_an_unknown_post_outcome_posts_once(self, tmp_path, position: int) -> None:
        """U2: 投稿の成否が不明でも、再開後にrecordは1件だけになる。"""
        steps = ["ok"] * (position - 1) + ["timeout"] + ["ok"]
        env = user_env(tmp_path, scenario=",".join(steps), timeout_seconds=2.0)
        accepted, _ = _round_trip(env, RecordKind.GATE_QUESTION)
        assert accepted.transaction is not None
        before = _payload(env)
        policy = make_policy(max_attempts=1, backoff_seconds=0.0)
        try:
            persist(**env.persist_kwargs(policy=policy, search_backoff_seconds=0.0))
        except TransportError:
            pass  # 成否不明のまま落ちた（呼び出し側はcheckpointから再開する）
        save_checkpoint(checkpoint_path(env.paths, RUN), before)  # 中断前へ戻す
        outcome = persist(**env.persist_kwargs())
        assert isinstance(outcome, RecordPersisted)
        assert _records_for(env, accepted.transaction.binding) == 1

    def test_crash_before_the_checkpoint_does_not_repost(self, tmp_path) -> None:
        """U3: 投稿・検証済みでtransactionを消す前に落ちた場合、再開は投稿しない。"""
        env = user_env(tmp_path)
        accepted, _ = _round_trip(env, RecordKind.GATE_QUESTION)
        before = _payload(env)
        assert isinstance(persist(**env.persist_kwargs()), RecordPersisted)
        save_checkpoint(checkpoint_path(env.paths, RUN), before)
        outcome = persist(**env.persist_kwargs())
        assert isinstance(outcome, RecordPersisted) and outcome.posted is False
        assert accepted.transaction is not None
        assert _records_for(env, accepted.transaction.binding) == 1

    def test_a_resent_submit_after_persistence_is_replayed(self, tmp_path) -> None:
        """U4: 永続化後に遅れて届いた同一submitは、recordを増やさず冪等に返る。"""
        env = user_env(tmp_path)
        accepted, envelope = _round_trip(env, RecordKind.GATE_QUESTION)
        assert isinstance(persist(**env.persist_kwargs()), RecordPersisted)
        outcome = submit(raw(envelope), **env.submit_kwargs())
        assert isinstance(outcome, UserInputReplayed)
        assert outcome.receipt == accepted.receipt
        assert accepted.transaction is not None
        assert _records_for(env, accepted.transaction.binding) == 1

    def test_persist_without_a_pending_record_stops(self, tmp_path) -> None:
        """U5: checkpoint後の再実行は、永続化を待つrecordが無いとして停止する。"""
        env = user_env(tmp_path)
        _round_trip(env, RecordKind.GATE_QUESTION)
        assert isinstance(persist(**env.persist_kwargs()), RecordPersisted)
        outcome = persist(**env.persist_kwargs())
        assert isinstance(outcome, EngineStopped) and outcome.code == "no_pending_record"
