# SPDX-License-Identifier: Apache-2.0
"""`PersistRecord`のcrash matrix（Phase 8。ADR-0017）。

recordの永続化には6つの中断窓がある。どこで落ちても**GitHub上の当該recordが常に1件**で
あり、再実行が重複を作らないことを固定する。

| 窓 | 中断位置 |
| --- | --- |
| W1 | transaction保存後・投稿前 |
| W2 | 投稿の成否不明（timeout） |
| W3 | 投稿後・read-after-write確認前 |
| W4 | 確認後・C-06検証前 |
| W5 | 検証後・checkpoint（transaction消費）前 |
| W6 | checkpoint後 |

製品経路（`ensure_comment_posted` -> `verify_record_chain`）だけを通し、fake gh越しに
実行する。実GitHubへは接続しない。
"""

from __future__ import annotations

import pytest
from c05_support.helpers import make_policy, read_state
from c07_support.helpers import RUN
from c08_support.helpers import FakeRecordSource, persist_env

from claude_code_codex_review_loop.domain.values import (
    MachineState,
    OpaqueBinding,
    PendingRecord,
    RecordKind,
    State,
)
from claude_code_codex_review_loop.identity.record_chain import ChainVerification
from claude_code_codex_review_loop.state import (
    CheckpointLoaded,
    checkpoint_path,
    load_checkpoint,
    save_checkpoint,
)
from claude_code_codex_review_loop.transport.gh import TransportError
from claude_code_codex_review_loop.workflow import (
    EngineStopped,
    RecordPersisted,
    persist,
    transaction_section,
    with_machine_state,
)


def _payload(env) -> dict[str, object]:
    loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
    assert isinstance(loaded, CheckpointLoaded)
    return loaded.payload


def _records_for(env) -> int:
    """当該bindingのrecordがGitHubに何件あるか（重複投稿の検出）。"""
    comments = read_state(env.directory)["comments"]
    assert isinstance(comments, list)
    return sum(1 for comment in comments if env.issued.binding in str(comment.get("body", "")))


def _rewind(env) -> None:
    """checkpointをpersist直前（transactionとpending record保持）へ戻す。"""
    from c08_support.helpers import checkpoint_payload

    state = MachineState(
        state=State.READY_FOR_HUMAN_MERGE,
        pending_record=PendingRecord(
            kind=RecordKind.GATE_ANSWER,
            binding=OpaqueBinding(env.issued.binding),
            source_state=State.READY_FOR_HUMAN_MERGE,
        ),
    )
    payload = with_machine_state(checkpoint_payload(), state)
    payload["transaction"] = transaction_section(env.issued)
    save_checkpoint(checkpoint_path(env.paths, RUN), payload)


def _resume(env) -> object:
    """中断後の再実行（同じ引数でもう一度persistする）。"""
    return persist(**env.kwargs())


class TestCrashWindows:
    def test_w1_before_posting(self, tmp_path) -> None:
        """W1: transactionだけがある状態から再開すると、投稿して1件になる。"""
        env = persist_env(tmp_path)
        assert _records_for(env) == 0
        outcome = _resume(env)
        assert isinstance(outcome, RecordPersisted) and outcome.posted is True
        assert _records_for(env) == 1

    @pytest.mark.parametrize("position", [1, 2, 3, 4])
    def test_w2_post_outcome_unknown(self, tmp_path, position: int) -> None:
        """W2: どの呼び出しがtimeoutしても、再開後にrecordは1件だけになる。

        成否不明を「どこで起きたか」に依存させない。C-05は成否不明をidempotency markerの
        検索で解決し（`ensure_comment_posted`）、C-08は再開時に`evaluate_pending`で
        投稿済みかを判定するため、どちらの層でも重複を作らない。
        """
        steps = ["ok"] * (position - 1) + ["timeout"] + ["ok"]
        env = persist_env(tmp_path, scenario=",".join(steps), timeout_seconds=2.0)
        policy = make_policy(max_attempts=1, backoff_seconds=0.0)
        try:
            persist(**env.kwargs(policy=policy, search_backoff_seconds=0.0))
        except TransportError:
            pass  # 成否不明のまま落ちた（呼び出し側はcheckpointから再開する）
        _rewind(env)
        outcome = _resume(env)
        assert isinstance(outcome, RecordPersisted)
        assert _records_for(env) == 1

    def test_w3_after_post_before_confirmation(self, tmp_path) -> None:
        """W3: 投稿は届いたが確認前に落ちた場合、再開は既存を見つけて再投稿しない。"""
        env = persist_env(tmp_path)
        first = _resume(env)
        assert isinstance(first, RecordPersisted)
        _rewind(env)  # 確認・検証・checkpointをやり直す
        outcome = _resume(env)
        assert isinstance(outcome, RecordPersisted) and outcome.posted is False
        assert _records_for(env) == 1

    def test_w4_after_confirmation_before_verification(self, tmp_path) -> None:
        """W4: 確認済みだが検証前に落ちた場合も、chain検証からやり直して1件のまま。"""
        env = persist_env(tmp_path)
        assert isinstance(_resume(env), RecordPersisted)
        _rewind(env)
        outcome = _resume(env)
        assert isinstance(outcome, RecordPersisted)
        assert outcome.record.key == env.issued.binding
        assert _records_for(env) == 1

    def test_w5_after_verification_before_checkpoint(self, tmp_path) -> None:
        """W5: 検証まで済んだがtransactionを消す前に落ちた場合、再開は投稿しない。"""
        env = persist_env(tmp_path)
        assert isinstance(_resume(env), RecordPersisted)
        _rewind(env)
        before = _records_for(env)
        outcome = _resume(env)
        assert isinstance(outcome, RecordPersisted) and outcome.posted is False
        assert _records_for(env) == before == 1
        assert "transaction" not in _payload(env)

    def test_w6_after_checkpoint(self, tmp_path) -> None:
        """W6: checkpoint後の再実行は、永続化を待つrecordが無いとして停止する。"""
        env = persist_env(tmp_path)
        assert isinstance(_resume(env), RecordPersisted)
        outcome = _resume(env)
        assert isinstance(outcome, EngineStopped) and outcome.code == "no_pending_record"
        assert _records_for(env) == 1


class TestNoDuplicateOnRepeatedResume:
    def test_repeated_resume_keeps_one_record(self, tmp_path) -> None:
        """W1 -> W5を何度繰り返しても、GitHub上のrecordは1件を超えない。"""
        env = persist_env(tmp_path)
        for _ in range(3):
            _rewind(env)
            outcome = _resume(env)
            assert isinstance(outcome, RecordPersisted)
        assert _records_for(env) == 1

    def test_stale_chain_does_not_repost(self, tmp_path) -> None:
        """検証側が古いchainを返しても、投稿は`ensure_comment_posted`が重複を防ぐ。"""
        from c07_support.helpers import verified_chain

        env = persist_env(tmp_path)
        assert isinstance(_resume(env), RecordPersisted)
        _rewind(env)
        stale = FakeRecordSource(records=verified_chain([RecordKind.REVIEW_RESULT]).records)
        outcome = persist(**env.kwargs(records_port=stale))
        assert isinstance(outcome, EngineStopped) and outcome.code == "record_unverified"
        assert _records_for(env) == 1


def test_chain_verification_is_the_gate() -> None:
    """`ChainVerification.is_intact`がconsumerのgateである（C-06の契約）。"""
    empty = ChainVerification(records=(), violations=(), max_seq=0, assurance_high_water=0)
    assert empty.is_intact
