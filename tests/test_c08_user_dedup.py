# SPDX-License-Identifier: Apache-2.0
"""2経路の重複防止key（Phase 8 PR-2c。ADR-0018 決定6 / 7）。

ユーザー入力は2経路ある（対話型sessionからの転記 / GitHubへの直接comment）。同じ待機に
対して両方が使われても、canonical recordを二重に作らないことをkeyで保証する。

本PRが提供するのは**keyの導出と台帳の読み書き、engine側の判定**である。経路2のcommentを
GitHubから見つけて受理するのはC-13の責務で、ここでは「受理済みとして台帳に載っている」
状態を作って判定を確かめる。
"""

from __future__ import annotations

from c07_support.helpers import RUN
from c08_support.helpers import (
    HEAD,
    FakeIds,
    raw,
    user_env,
    user_record_payload,
    user_submit_payload,
    write_result,
)

from claude_code_codex_review_loop.domain.values import Awaiting, RecordKind
from claude_code_codex_review_loop.state import (
    CheckpointLoaded,
    checkpoint_path,
    load_checkpoint,
    save_checkpoint,
)
from claude_code_codex_review_loop.workflow import (
    AwaitUser,
    ConsumedIntent,
    EngineStopped,
    UserInputAccepted,
    UserIntentAlreadyRecorded,
    advance,
    intent_key,
    submit,
    with_consumed_intent,
)

GATE = Awaiting.USER_INPUT_GATE
GITHUB_BINDING = "ud:{\"comment\":\"c-900\"}"


def _payload(env) -> dict[str, object]:
    loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
    assert isinstance(loaded, CheckpointLoaded)
    return loaded.payload


def _issued(env, **overrides) -> AwaitUser:
    outcome = advance(**env.advance_kwargs(**overrides))
    assert isinstance(outcome, AwaitUser), outcome
    return outcome


def _consume_via_github(env, issued: AwaitUser, kind: RecordKind) -> str:
    """経路2（GitHub直接comment）の受理をC-13の代わりに台帳へ書く。

    C-13は`accept_user_decision`で受理した時点で同じkeyを書き、未応答requestは残す
    （残さないと、遅れて届く転記submitへ「どのbindingで確定したか」を返せない）。
    """
    key = intent_key(
        run_id=RUN,
        awaiting=issued.request.awaiting,
        since_seq=issued.request.since_seq,
        head_sha=issued.request.expected_head_sha,
        kind=kind,
    )
    payload = with_consumed_intent(
        _payload(env),
        ConsumedIntent(intent_key=key, binding=GITHUB_BINDING, route="github_comment"),
    )
    save_checkpoint(checkpoint_path(env.paths, RUN), payload)
    return key


def _respond(env, issued: AwaitUser, kind: RecordKind) -> dict[str, object]:
    digest = write_result(env.run_dir, issued.request.result_path, user_record_payload(kind))
    return user_submit_payload(
        request_id=issued.request.request_id,
        nonce=issued.request.nonce,
        result_hash=digest,
        awaiting=env.awaiting,
        result_kind=kind.value,
    )


def _comment_count(env) -> int:
    return len(env.comments())


class TestSameIntent:
    def test_the_transcript_does_not_add_a_second_record(self, tmp_path) -> None:
        """同じintentが別経路で記録済みなら、転記は2件目を作らない。"""
        env = user_env(tmp_path)
        issued = _issued(env)
        _consume_via_github(env, issued, RecordKind.MERGE_APPROVAL)
        before = _comment_count(env)
        outcome = submit(raw(_respond(env, issued, RecordKind.MERGE_APPROVAL)), **env.submit_kwargs())
        assert isinstance(outcome, UserIntentAlreadyRecorded)
        assert outcome.consumed.binding == GITHUB_BINDING
        assert outcome.consumed.route == "github_comment"
        assert _comment_count(env) == before

    def test_no_transaction_is_issued(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issued(env)
        _consume_via_github(env, issued, RecordKind.MERGE_APPROVAL)
        submit(raw(_respond(env, issued, RecordKind.MERGE_APPROVAL)), **env.submit_kwargs())
        assert "transaction" not in _payload(env)

    def test_the_state_is_not_advanced_twice(self, tmp_path) -> None:
        env = user_env(tmp_path)
        issued = _issued(env)
        _consume_via_github(env, issued, RecordKind.MERGE_APPROVAL)
        submit(raw(_respond(env, issued, RecordKind.MERGE_APPROVAL)), **env.submit_kwargs())
        section = _payload(env)["state"]
        assert isinstance(section, dict)
        assert section["awaiting"] == GATE.value


class TestDifferentIntent:
    def test_a_conflicting_intent_stops(self, tmp_path) -> None:
        """同じ待機に2経路が別のintentを主張したら、どちらかを推測せず停止する。"""
        env = user_env(tmp_path)
        issued = _issued(env)
        _consume_via_github(env, issued, RecordKind.MERGE_APPROVAL)
        outcome = submit(raw(_respond(env, issued, RecordKind.GATE_CHANGES)), **env.submit_kwargs())
        assert isinstance(outcome, EngineStopped) and outcome.code == "user_intent_conflict"


class TestInstanceBoundary:
    def test_a_later_instance_is_not_deduplicated(self, tmp_path) -> None:
        """chainが進んだ次の待機は別instanceであり、前instanceのkeyと一致しない。"""
        env = user_env(tmp_path)
        issued = _issued(env)
        key = _consume_via_github(env, issued, RecordKind.MERGE_APPROVAL)
        later = intent_key(
            run_id=RUN,
            awaiting=GATE,
            since_seq=issued.request.since_seq + 1,
            head_sha=HEAD,
            kind=RecordKind.MERGE_APPROVAL,
        )
        assert later != key

    def test_a_new_request_replaces_the_ledger(self, tmp_path) -> None:
        """新しいrequestはsection全体を入れ替える（前instanceのkeyを持ち越さない）。"""
        env = user_env(tmp_path)
        issued = _issued(env)
        _consume_via_github(env, issued, RecordKind.MERGE_APPROVAL)
        # 応答前にrequestを消し、次のadvanceで新しいinstanceを開かせる
        payload = _payload(env)
        section = payload["user_request"]
        assert isinstance(section, dict)
        del section["pending"]
        save_checkpoint(checkpoint_path(env.paths, RUN), payload)
        again = _issued(env, id_source=FakeIds("req2"))
        assert again.reissued is False
        stored = _payload(env)["user_request"]
        assert isinstance(stored, dict)
        assert set(stored) == {"pending"}
        outcome = submit(raw(_respond(env, again, RecordKind.MERGE_APPROVAL)), **env.submit_kwargs())
        assert isinstance(outcome, UserInputAccepted)
