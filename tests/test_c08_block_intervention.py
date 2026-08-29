# SPDX-License-Identifier: Apache-2.0
"""`BLOCK_INTERVENTION`搬送路の受入test（Phase 8 PR-3c。ADR-0023）。

`BLOCKED`のrunへユーザーが介入する経路を固定する。C-01は既に受け口を持っており
（P-22 / P-23がrecordを受理し、B-IV1 / B-IV2が**保存した継続を1回だけ再現**する）、
本PRが足すのはそこへ入力を運ぶC-08側の搬送路である。

`AWAIT_USER`との違いは**待機の識別子**だけである。あちらはC-01の`awaiting` + `since_seq`、
こちらは解除対象の**block attempt binding**が識別子になる。
"""

from __future__ import annotations

from typing import Any

import pytest
from c07_support.helpers import verified_chain
from c08_support.helpers import (
    HEAD,
    NEW_HEAD,
    RUN,
    FakeEvidencePort,
    FakeIds,
    FakeRecordSource,
    blocked_machine_state,
    external_block,
    integrity_block,
    limit_block,
    progress_block,
    raw,
    user_env,
    user_record_payload,
    user_submit_payload,
    write_result,
)

from claude_code_codex_review_loop.domain.values import MachineState, RecordKind, State
from claude_code_codex_review_loop.state import (
    CheckpointLoaded,
    checkpoint_path,
    load_checkpoint,
    save_checkpoint,
)
from claude_code_codex_review_loop.workflow import (
    AwaitUser,
    Blocked,
    EngineStopped,
    RecordPersisted,
    UserInputAccepted,
    advance,
    persist,
    read_user_request,
    submit,
    with_machine_state,
)

PROGRESS_BINDING = "cr:run-1:1:progress"
# 既定のseed（FINAL_REPORT 1件）より長いchain。`since_seq`が動いたことを観測するために使う
_LONGER_CHAIN = [RecordKind.FINAL_REPORT, RecordKind.REVIEW_RESULT, RecordKind.FIX_RESULT]
EXTERNAL_BINDING = "cr:run-1:1:external"


def _env(tmp_path: Any, block: Any, **overrides: Any) -> Any:
    return user_env(tmp_path, block=block, **overrides)


def _issue(env: Any, **overrides: Any) -> Any:
    return advance(**env.advance_kwargs(**overrides))


def _await(env: Any, **overrides: Any) -> AwaitUser:
    outcome = _issue(env, **overrides)
    assert isinstance(outcome, AwaitUser), outcome
    return outcome


def _answer(
    env: Any,
    issued: AwaitUser,
    *,
    kind: RecordKind = RecordKind.BLOCK_INTERVENTION,
    binding: str | None = PROGRESS_BINDING,
    head: str = HEAD,
    block_binding: str | None = None,
) -> Any:
    """ユーザーの応答を書いてsubmitする（既定は対象blockに一致する介入record）。"""
    payload = user_record_payload(kind, head=head, target_block_binding=binding)
    result_hash = write_result(env.run_dir, issued.request.result_path, payload)
    envelope = user_submit_payload(
        request_id=issued.request.request_id,
        nonce=issued.request.nonce,
        result_hash=result_hash,
        awaiting=None,
        block_binding=block_binding or issued.request.block_binding,
        result_kind=kind.value,
        head=head,
    )
    return submit(raw(envelope), **env.submit_kwargs())


class TestWhichBlocksOpenTheTransport:
    """介入requestを出すのは、**C-01が`BLOCK_INTERVENTION`を受理するblock**だけである。"""

    @pytest.mark.parametrize(
        ("block", "binding"),
        [(progress_block(), PROGRESS_BINDING), (external_block(), EXTERNAL_BINDING)],
        ids=["no_progress", "external_dependency"],
    )
    def test_a_resolvable_block_asks_the_user(self, tmp_path: Any, block: Any, binding: str) -> None:
        """P-22 / P-23が受理するblockでは、解除対象を束ねたrequestが出る。"""
        issued = _await(_env(tmp_path, block))
        assert issued.waits_for_block
        assert issued.awaiting is None
        assert issued.request.block_binding == binding
        assert issued.envelope["block_binding"] == binding
        # 待機の識別子は排他（schemaのcross-field ruleと同じ規則）
        assert "awaiting" not in issued.envelope

    @pytest.mark.parametrize(
        "block", [limit_block(), integrity_block()], ids=["limit_reached", "integrity"]
    )
    def test_a_block_without_an_intervention_exit_stays_blocked(
        self, tmp_path: Any, block: Any
    ) -> None:
        """出口がlimit引き上げ / 復元 / salvageのblockへは、受理されない応答を提示しない。"""
        outcome = _issue(_env(tmp_path, block))
        assert isinstance(outcome, Blocked) and outcome.block == block

    def test_the_offered_kinds_are_the_ones_c01_accepts(self, tmp_path: Any) -> None:
        """`BLOCK_INTERVENTION`（P-22 / P-23）と`USER_CANCEL`（P-21がBLOCKEDを覆う）。"""
        issued = _await(_env(tmp_path, progress_block()))
        offered = issued.envelope["accepted_result_kinds"]
        assert isinstance(offered, list)
        assert set(offered) == {
            RecordKind.BLOCK_INTERVENTION.value,
            RecordKind.USER_CANCEL.value,
        }

    def test_the_external_block_carries_its_record_as_evidence(self, tmp_path: Any) -> None:
        """何を待っているかを書いたrecordを根拠として同梱する（AC-C08-07と同型）。"""
        env = _env(tmp_path, external_block(), seeded=(RecordKind.EXTERNAL_DEPENDENCY,))
        issued = _await(env)
        assert issued.envelope["verified_records"]


class TestChainGate:
    def test_a_broken_chain_stops_before_asking(self, tmp_path: Any) -> None:
        """壊れたchainの上でユーザーへ判断を求めない（ADR-0018 決定13）。"""
        env = _env(tmp_path, progress_block())
        broken = FakeRecordSource(records=env.records, violations=("gap",))
        outcome = _issue(env, records_port=broken)
        assert isinstance(outcome, EngineStopped) and outcome.code == "chain_violation"

    def test_a_block_without_an_intervention_exit_does_not_read_the_chain(
        self, tmp_path: Any
    ) -> None:
        """状態を報告するだけの経路はchainを読まない（新しいturnを起こさないため）。"""
        env = _env(tmp_path, integrity_block())
        broken = FakeRecordSource(records=env.records, violations=("gap",))
        outcome = _issue(env, records_port=broken)
        assert isinstance(outcome, Blocked)


class TestInstanceIdentity:
    """未応答requestを再提示するのは、それが**今のblock**を指す場合だけである。"""

    def test_the_same_block_is_reissued(self, tmp_path: Any) -> None:
        env = _env(tmp_path, progress_block())
        first = _await(env)
        again = _await(env, id_source=FakeIds("second"))
        assert again.reissued
        assert again.request.request_id == first.request.request_id

    def test_a_growing_chain_does_not_replace_the_request(self, tmp_path: Any) -> None:
        """`since_seq`は判定に使わない。blockのbindingがinstanceそのものである（決定2）。

        chainが伸びただけで同じblockのrequestを作り直すと、前instanceの消費済みintentを
        毎回捨てることになる。`AWAIT_USER`側は`since_seq`が変われば新規発行するが、
        blockの待機はbindingで決まるため、この違いは意図したものである。
        """
        env = _env(tmp_path, progress_block())
        first = _await(env)
        grown = FakeRecordSource(records=verified_chain(_LONGER_CHAIN).records)
        assert grown.chain(RUN).max_seq > FakeRecordSource(records=env.records).chain(RUN).max_seq
        again = _await(env, records_port=grown, id_source=FakeIds("second"))
        assert again.reissued and again.request.request_id == first.request.request_id

    def test_another_block_replaces_the_request(self, tmp_path: Any) -> None:
        """blockへ入り直せばbindingが変わり、前instanceのrequestは指さなくなる。"""
        env = _env(tmp_path, progress_block())
        first = _await(env)
        moved = user_env(tmp_path / "second", block=external_block())
        second = _await(moved, id_source=FakeIds("second"))
        assert not second.reissued
        assert second.request.block_binding != first.request.block_binding

    def test_a_moved_head_replaces_the_request(self, tmp_path: Any) -> None:
        """headが動けばrecordのbind先が変わる（head binding。D-031）。"""
        env = _env(tmp_path, progress_block())
        _await(env)
        second = _await(env, head_sha=NEW_HEAD, id_source=FakeIds("second"))
        assert not second.reissued and second.request.expected_head_sha == NEW_HEAD


class TestSubmitBinding:
    """AC-C08-05と同じ規則を、blockの識別子でも成立させる。"""

    def test_the_answer_is_accepted_once(self, tmp_path: Any) -> None:
        env = _env(tmp_path, progress_block())
        issued = _await(env)
        outcome = _answer(env, issued)
        assert isinstance(outcome, UserInputAccepted)
        assert outcome.machine_state.pending_record is not None
        assert outcome.machine_state.pending_record.kind is RecordKind.BLOCK_INTERVENTION

    def test_a_submit_for_another_block_is_refused(self, tmp_path: Any) -> None:
        """別のblockを指すsubmitは、対象と一致しない解消evidenceを作らせる。"""
        env = _env(tmp_path, progress_block())
        issued = _await(env)
        outcome = _answer(env, issued, block_binding=EXTERNAL_BINDING)
        assert isinstance(outcome, EngineStopped) and outcome.code == "block_mismatch"

    def test_a_result_targeting_another_block_is_refused_before_posting(
        self, tmp_path: Any
    ) -> None:
        """投稿してから気付くのではなく、**受理の時点で止める**（決定6）。

        通してしまうと、recordはGitHubへ出るのに解消eventが対象blockと一致せず、runは
        `BLOCKED`のまま詰まる。
        """
        env = _env(tmp_path, progress_block())
        issued = _await(env)
        before = len(env.comments())
        outcome = _answer(env, issued, binding="cr:run-1:9:other")
        assert isinstance(outcome, EngineStopped) and outcome.code == "record_block_mismatch"
        assert len(env.comments()) == before

    def test_a_result_for_another_head_is_refused(self, tmp_path: Any) -> None:
        env = _env(tmp_path, progress_block())
        issued = _await(env)
        outcome = _answer(env, issued, head=NEW_HEAD)
        assert isinstance(outcome, EngineStopped) and outcome.code == "head_mismatch"

    def test_a_submit_without_a_pending_request_is_refused(self, tmp_path: Any) -> None:
        env = _env(tmp_path, progress_block())
        issued = _await(env)
        envelope = user_submit_payload(
            request_id="ghost",
            nonce=issued.request.nonce,
            result_hash="deadbeef",
            awaiting=None,
            block_binding=PROGRESS_BINDING,
            result_kind=RecordKind.BLOCK_INTERVENTION.value,
        )
        outcome = submit(raw(envelope), **env.submit_kwargs())
        assert isinstance(outcome, EngineStopped) and outcome.code == "stale_request"

    def test_cancel_is_accepted_from_a_blocked_run(self, tmp_path: Any) -> None:
        """P-21（awaiting不問・非terminal全state）が`BLOCKED`を覆うため、cancelも正規である。"""
        env = _env(tmp_path, progress_block())
        issued = _await(env)
        outcome = _answer(env, issued, kind=RecordKind.USER_CANCEL, binding=None)
        assert isinstance(outcome, UserInputAccepted)
        assert outcome.machine_state.pending_record is not None
        assert outcome.machine_state.pending_record.kind is RecordKind.USER_CANCEL


class TestCheckpointRoundTrip:
    def test_the_request_survives_a_reload(self, tmp_path: Any) -> None:
        """別processからのresumeで同じrequestを復元できる（AC-C08-06と同型）。"""
        env = _env(tmp_path, progress_block())
        issued = _await(env)
        loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
        assert isinstance(loaded, CheckpointLoaded)
        stored = read_user_request(loaded.payload)
        assert stored is not None and not isinstance(stored, EngineStopped)
        assert stored.block_binding == issued.request.block_binding
        assert stored.awaiting is None


def _restate(env: Any, machine_state: Any) -> None:
    """checkpointのstateだけを差し替える（未応答requestは保持する）。"""
    loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
    assert isinstance(loaded, CheckpointLoaded)
    save_checkpoint(
        checkpoint_path(env.paths, RUN), with_machine_state(loaded.payload, machine_state)
    )


class TestTheBlockChangesUnderTheRequest:
    """requestを出してから応答が届くまでの間に、blockが動くことがある。

    どの場合も**推測せず停止する**。古いrequestの応答を、別のblockや解消後のrunへ
    流し込ませない（決定12）。
    """

    def test_a_resolved_block_refuses_the_answer(self, tmp_path: Any) -> None:
        """別経路で解消された後に届いた応答は受理しない。"""
        env = _env(tmp_path, progress_block())
        issued = _await(env)
        _restate(env, MachineState(state=State.READY_FOR_HUMAN_MERGE))
        outcome = _answer(env, issued)
        assert isinstance(outcome, EngineStopped) and outcome.code == "request_superseded"

    def test_a_replaced_block_refuses_the_answer(self, tmp_path: Any) -> None:
        """別のblockへ入り直した後は、bindingが違うので受理しない。"""
        env = _env(tmp_path, progress_block())
        issued = _await(env)
        _restate(env, blocked_machine_state(external_block()))
        outcome = _answer(env, issued)
        assert isinstance(outcome, EngineStopped) and outcome.code == "request_superseded"

    def test_a_block_that_stopped_accepting_intervention_refuses(self, tmp_path: Any) -> None:
        """介入を受理しないblockへ変わっていれば、契約が引けないので停止する。"""
        env = _env(tmp_path, progress_block())
        issued = _await(env)
        _restate(env, blocked_machine_state(integrity_block()))
        outcome = _answer(env, issued)
        assert isinstance(outcome, EngineStopped) and outcome.code == "not_user_input"

    def test_a_kind_the_block_does_not_accept_is_refused(self, tmp_path: Any) -> None:
        """blockが受理しないrecord種別（merge gate用など）は結果として受け取らない。"""
        env = _env(tmp_path, progress_block())
        issued = _await(env)
        outcome = _answer(env, issued, kind=RecordKind.GATE_QUESTION, binding=None)
        assert isinstance(outcome, EngineStopped)
        assert outcome.code == "result_kind_not_allowed"


class TestIssueFailures:
    @pytest.mark.parametrize("both", [True, False], ids=["both", "neither"])
    def test_an_ambiguous_pending_request_stops(self, tmp_path: Any, both: bool) -> None:
        """待機の識別子が決まらないpendingは**推測せずfail closeする**（決定1）。

        schemaは`USER_REQUEST` / `USER_SUBMIT`のcross-field ruleで一方だけを要求するが、
        checkpointのsectionは両fieldがoptionalなので、readerが同じ規則を持つ必要がある。
        """
        env = _env(tmp_path, progress_block())
        _await(env)
        loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
        assert isinstance(loaded, CheckpointLoaded)
        payload = dict(loaded.payload)
        section = dict(payload["user_request"])  # type: ignore[arg-type]
        pending = dict(section["pending"])  # type: ignore[arg-type]
        if both:
            pending["awaiting"] = "USER_INPUT_GATE"
        else:
            del pending["block_binding"]
        section["pending"] = pending
        payload["user_request"] = section
        save_checkpoint(checkpoint_path(env.paths, RUN), payload)
        outcome = _issue(env)
        assert isinstance(outcome, EngineStopped) and outcome.code == "user_request_unavailable"

    def test_evidence_of_the_wrong_kind_stops(self, tmp_path: Any) -> None:
        """registryが宣言していない種別の根拠は同梱しない（AC-C08-07）。"""
        env = _env(tmp_path, external_block(), seeded=(RecordKind.FINAL_REPORT,))
        outcome = _issue(env, evidence_port=FakeEvidencePort(env.records))
        assert isinstance(outcome, EngineStopped) and outcome.code == "evidence_kind"


class TestEndToEnd:
    """`BLOCKED`から**継続の再現**まで、搬送路が1本で通ることを見る。

    C-01の受け口（P-22 -> B-IV1）へ、C-08の搬送路が実際に入力を運べるかという主張である。
    解消eventの写像はportが担うため（ADR-0023 決定7）、ここで使うfakeがC-10 / C-11の
    実装が置かれる位置を示している。
    """

    def test_the_run_leaves_blocked_and_replays_its_continuation(self, tmp_path: Any) -> None:
        env = _env(tmp_path, progress_block())

        issued = _await(env)
        assert issued.request.block_binding == PROGRESS_BINDING

        accepted = _answer(env, issued)
        assert isinstance(accepted, UserInputAccepted)
        # recordはまだ投稿されていない（`PersistRecord`がこれから走る）
        assert accepted.machine_state.state is State.BLOCKED
        assert accepted.transaction is not None

        persisted = persist(**env.persist_kwargs())
        assert isinstance(persisted, RecordPersisted), persisted
        # blockが解け、保存されていた継続が1回だけ再現される（B-IV1）
        assert persisted.machine_state.block is None
        assert persisted.machine_state.state is not State.BLOCKED
        assert persisted.machine_state.pending_record is None

    def test_the_intervention_reaches_github_once(self, tmp_path: Any) -> None:
        """canonical recordとしてGitHubへ残る（未永続化の出力を根拠にしない）。"""
        env = _env(tmp_path, progress_block())
        before = len(env.comments())
        accepted = _answer(env, _await(env))
        assert isinstance(accepted, UserInputAccepted)
        persist(**env.persist_kwargs())
        posted = env.comments()
        assert len(posted) == before + 1
