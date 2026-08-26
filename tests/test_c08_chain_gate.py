# SPDX-License-Identifier: Apache-2.0
"""壊れたchainの上で次のturnを起こさないことの受入test（Phase 8 PR-3b1。ADR-0020）。

`verify_record_chain`は**violationがあってもrecordsを返す**契約である（差分表示のため）。
consumerが`is_intact`を確かめなければ、正当性を確かめていないrecordをhostやユーザーへ
根拠として渡すことになる。

ここではfake GitHubのcomment列を実際に壊し、**製品の取得 -> 検証経路**（C-05 -> C-06）を
通した上で、`HOST_ACTION`も`AWAIT_USER`も発行されないことを固定する。
"""

from __future__ import annotations

import dataclasses

import pytest
from c06_support.helpers import seed_dict
from c07_support.helpers import NUMBER, RUN, chain_comments_of, conversation_section
from c08_support.helpers import machine_state, user_machine_state
from c08_support.runtime import (
    ACCEPTED_AT,
    ISSUED_AT,
    FakeActionPayloads,
    FakeIds,
    RuntimeEnv,
    gate_host,
    round_ports,
    runtime_env,
)

from claude_code_codex_review_loop.domain.values import Awaiting, RecordKind
from claude_code_codex_review_loop.runtime import (
    ChainNotIntactError,
    PortSet,
    step,
    submit_result,
)
from claude_code_codex_review_loop.state import checkpoint_path
from claude_code_codex_review_loop.workflow import (
    AwaitUser,
    EngineStopped,
    HostActionIssued,
    SubmitAccepted,
    SubmitReplayed,
)

GATE_KINDS = (RecordKind.FINAL_REPORT,)
ACTION_KINDS = (RecordKind.REVIEW_RESULT,)


def _step(env: RuntimeEnv, ports: PortSet | None = None):
    return step(
        paths=env.paths,
        config=env.config,
        ports=ports if ports is not None else env.ports(),
        id_source=FakeIds("req"),
        issued_at=ISSUED_AT,
    )


def _action_ports(env: RuntimeEnv) -> PortSet:
    return dataclasses.replace(
        round_ports(env),
        payload=FakeActionPayloads({"APPLY_FINDINGS": {"round": 1, "finding_ids": ["F-1"]}}),
    )


def _tamper(env: RuntimeEnv, kinds, *, mutate) -> None:
    """正規chainを組み立ててから壊し、fake GitHubへ置き直す。"""
    comments = list(chain_comments_of(list(kinds)))
    entries = [seed_dict(comment, issue=NUMBER) for comment in comments]
    env.seed(mutate(entries))


class TestBrokenLink:
    """条件3（`prev`が直前recordの本文hashと一致する）が破れた場合。"""

    def _broken(self, entries: list[dict]) -> list[dict]:
        # seq=1の本文を書き換えると、seq=2の`prev`が指すhashと合わなくなる
        entries[0]["body"] = str(entries[0]["body"]).replace("record 1", "record 1（改竄）")
        return entries

    def test_no_user_request_is_issued(self, tmp_path) -> None:
        env = runtime_env(
            tmp_path,
            state=user_machine_state(Awaiting.USER_INPUT_GATE),
            seeded=(RecordKind.REVIEW_RESULT, RecordKind.FINAL_REPORT),
        )
        _tamper(env, (RecordKind.REVIEW_RESULT, RecordKind.FINAL_REPORT), mutate=self._broken)
        outcome = _step(env).outcome
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "chain_violation"

    def test_no_host_action_is_issued(self, tmp_path) -> None:
        """`HOST_ACTION`側にもgateがある（PR-3b1以前はここが素通しだった）。"""
        env = runtime_env(
            tmp_path,
            state=machine_state(),
            seeded=(RecordKind.REVIEW_RESULT, RecordKind.CLARIFICATION_ANSWER),
        )
        _tamper(
            env, (RecordKind.REVIEW_RESULT, RecordKind.CLARIFICATION_ANSWER), mutate=self._broken
        )
        outcome = _step(env, _action_ports(env)).outcome
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "chain_violation"
        # 根拠portへ届く前に止まる（未検証recordを選ばせない）
        assert env.run_dir.joinpath("actions").exists() is False


class TestKnownRecordDeleted:
    """checkpointが既知としたrecordがGitHubから消えた場合（AC-C06-09）。

    checkpointを渡さないと、消えたrecordは「元から無かった」と区別できず、chainは
    intactに見えてしまう。`ChainRecords`はcheckpointのchain部分とprobe結果を渡す。
    """

    def _env(self, tmp_path) -> RuntimeEnv:
        posted = chain_comments_of([RecordKind.REVIEW_RESULT, RecordKind.FINAL_REPORT])
        env = runtime_env(
            tmp_path,
            state=user_machine_state(Awaiting.USER_INPUT_GATE),
            seeded=(RecordKind.REVIEW_RESULT, RecordKind.FINAL_REPORT),
            extra={"conversation": conversation_section(posted)},
        )
        return env

    def test_the_chain_is_intact_while_every_known_record_is_present(self, tmp_path) -> None:
        """検出が「常にviolation」になっていないことを確かめる（対照）。"""
        env = self._env(tmp_path)
        assert env.ports().records.chain(RUN).is_intact

    def test_a_deleted_known_record_is_detected(self, tmp_path) -> None:
        env = self._env(tmp_path)
        _tamper(
            env,
            (RecordKind.REVIEW_RESULT, RecordKind.FINAL_REPORT),
            mutate=lambda entries: entries[1:],  # seq=1を削除する
        )
        chain = env.ports().records.chain(RUN)
        assert not chain.is_intact
        assert chain.violations

    def test_no_request_is_issued_after_a_deletion(self, tmp_path) -> None:
        env = self._env(tmp_path)
        _tamper(
            env,
            (RecordKind.REVIEW_RESULT, RecordKind.FINAL_REPORT),
            mutate=lambda entries: entries[1:],
        )
        outcome = _step(env).outcome
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "chain_violation"


class TestSubmitIdempotence:
    """受理済みsubmitの再送は、**chainが後から壊れても**以前と同じ結果になる。

    ADR-0015の「受理済み同一submitは以前と同じ結果」は、chainの状態に依存しない。
    gateを冪等判定より前に置くと、同じbytesの再送が`SubmitReplayed`ではなく
    `chain_violation`になり、遅延再送するhostがretryできなくなる。
    """

    def _accepted(self, tmp_path):
        """`HOST_ACTION`へ1件submitを受理させ、そのbytesとenvを返す。"""
        env = runtime_env(
            tmp_path,
            state=user_machine_state(Awaiting.USER_INPUT_GATE),
            seeded=(RecordKind.FINAL_REPORT,),
        )
        ports = round_ports(env)
        host = gate_host(env)
        issued = _step(env, ports).outcome
        assert isinstance(issued, AwaitUser)
        _submit(env, ports, host.execute(issued))
        action = _step(env, ports).outcome
        assert isinstance(action, HostActionIssued)
        raw = host.execute(action)
        assert isinstance(_submit(env, ports, raw), SubmitAccepted)
        return env, ports, raw

    def test_an_exact_replay_survives_a_broken_chain(self, tmp_path) -> None:
        env, ports, raw = self._accepted(tmp_path)
        _tamper(
            env,
            (RecordKind.FINAL_REPORT, RecordKind.GATE_QUESTION, RecordKind.GATE_ANSWER),
            mutate=self._break_first,
        )
        assert not ports.records.chain(RUN).is_intact  # 前提: chainは壊れている
        assert isinstance(_submit(env, ports, raw), SubmitReplayed)

    def test_a_new_submit_is_refused_on_a_broken_chain(self, tmp_path) -> None:
        """未受理のsubmitは消費しない（冪等判定の後にgateがある）。"""
        env = runtime_env(
            tmp_path,
            state=user_machine_state(Awaiting.USER_INPUT_GATE),
            seeded=(RecordKind.FINAL_REPORT,),
        )
        ports = round_ports(env)
        host = gate_host(env)
        issued = _step(env, ports).outcome
        assert isinstance(issued, AwaitUser)
        _submit(env, ports, host.execute(issued))
        action = _step(env, ports).outcome
        assert isinstance(action, HostActionIssued)
        raw = host.execute(action)
        _tamper(
            env,
            (RecordKind.FINAL_REPORT, RecordKind.GATE_QUESTION),
            mutate=self._break_first,
        )
        outcome = _submit(env, ports, raw)
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "chain_violation"

    @staticmethod
    def _break_first(entries: list[dict]) -> list[dict]:
        entries[0]["body"] = str(entries[0]["body"]).replace("record 1", "改竄")
        return entries


def _submit(env: RuntimeEnv, ports: PortSet, raw: bytes):
    return submit_result(
        raw, paths=env.paths, config=env.config, ports=ports, accepted_at=ACCEPTED_AT
    )


class TestMissingCheckpoint:
    def test_an_unreadable_checkpoint_is_treated_as_fresh(self, tmp_path) -> None:
        """既知recordが読めない場合はfresh扱いにする（推測でviolationを作らない）。

        進退の判断は`load_run`が同じcheckpointを読んで行う。ここで別の判断を下さない。
        """
        env = runtime_env(
            tmp_path,
            state=user_machine_state(Awaiting.USER_INPUT_GATE),
            seeded=(RecordKind.FINAL_REPORT,),
        )
        checkpoint_path(env.paths, RUN).unlink()
        assert env.ports().records.chain(RUN).is_intact


class TestEvidencePort:
    """engineのgateとportの検査は**どちらもfail closed**である。"""

    def test_the_port_refuses_a_violated_chain(self, tmp_path) -> None:
        """gateを通った後にchainが壊れる窓を閉じる（portが後から観測しても止まる）。"""
        env = runtime_env(
            tmp_path,
            state=user_machine_state(Awaiting.USER_INPUT_GATE),
            seeded=(RecordKind.REVIEW_RESULT, RecordKind.FINAL_REPORT),
        )
        _tamper(
            env,
            (RecordKind.REVIEW_RESULT, RecordKind.FINAL_REPORT),
            mutate=lambda entries: [
                {**entries[0], "body": str(entries[0]["body"]).replace("record 1", "改竄")},
                entries[1],
            ],
        )
        with pytest.raises(ChainNotIntactError):
            env.ports().evidence.evidence_for(_gate_context(env))

    def test_a_port_that_observes_a_violation_later_stops_the_step(self, tmp_path) -> None:
        """gate通過**後**にchainが壊れた場合（engineのgateは既に通っている）。

        `step`はportの例外をengineのgateと同じ`chain_violation`へ写す。分類が分かれると、
        同じ理由の停止が2つのcodeで現れて呼び出し側が扱いを分けることになる。
        """
        env = runtime_env(
            tmp_path,
            state=user_machine_state(Awaiting.USER_INPUT_GATE),
            seeded=(RecordKind.FINAL_REPORT,),
        )
        ports = dataclasses.replace(env.ports(), evidence=_LateViolation())
        outcome = _step(env, ports).outcome
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "chain_violation"


class _LateViolation:
    """chainがintactに見えた後でviolationを観測するevidence port。"""

    def evidence_for(self, context: object) -> tuple[()]:
        raise ChainNotIntactError("chainにviolationがある（1件）")


def _gate_context(env: RuntimeEnv):
    from claude_code_codex_review_loop.workflow.ports import UserRequestContext

    return UserRequestContext(
        awaiting=Awaiting.USER_INPUT_GATE,
        run_id=RUN,
        repository=env.config.repository,
        number=env.config.number,
        head_sha=env.config.head_sha,
    )
