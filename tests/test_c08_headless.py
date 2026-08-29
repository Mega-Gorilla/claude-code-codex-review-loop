# SPDX-License-Identifier: Apache-2.0
"""headless adapterの受入test（Phase 8 PR-3b3。ADR-0022）。

境界は**実行file**なので、spawn・待機・stdout回収・`processes`台帳・redactionはすべて
製品codeが走る。fakeなのは「何を返すか」だけである。**実Claudeは起動しない**。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from c07_support.helpers import RUN
from c08_support.headless import host_env, user_entry, write_fake_host, write_plan
from c08_support.helpers import job_object_ref, user_machine_state
from c08_support.runtime import ISSUED_AT, FakeIds, RuntimeEnv, runtime_env

from claude_code_codex_review_loop.domain.values import Awaiting, RecordKind
from claude_code_codex_review_loop.policy.permission_profile import ForbiddenFlagError
from claude_code_codex_review_loop.runtime import HeadlessError, HeadlessHost, step, submit_result
from claude_code_codex_review_loop.runtime.host_headless import LOG_FILE, STDERR_FILE, STDOUT_FILE
from claude_code_codex_review_loop.state import (
    CheckpointLoaded,
    checkpoint_path,
    load_checkpoint,
    save_checkpoint,
)
from claude_code_codex_review_loop.workflow import (
    AwaitUser,
    SectionUnavailable,
    UserInputAccepted,
    read_active_trees,
    with_active_trees,
    with_tree_added,
    with_tree_removed,
)

ACCEPTED_AT = "2026-08-26T09:05:00Z"
# 例示用のtoken（実物ではない。redactが効くことを見るためだけの文字列）
FAKE_TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyzAB"


def _env(tmp_path: Path, **extra: object) -> RuntimeEnv:
    return runtime_env(
        tmp_path,
        state=user_machine_state(Awaiting.USER_INPUT_GATE),
        seeded=(RecordKind.FINAL_REPORT,),
        **extra,  # type: ignore[arg-type]
    )


def _issue(env: RuntimeEnv) -> AwaitUser:
    outcome = step(
        paths=env.paths,
        config=env.config,
        ports=env.ports(),
        id_source=FakeIds("req"),
        issued_at=ISSUED_AT,
    ).outcome
    assert isinstance(outcome, AwaitUser), outcome
    return outcome


def _host(runtime: RuntimeEnv, tmp_path: Path, **overrides: object) -> HeadlessHost:
    script = write_fake_host(tmp_path)
    plan = write_plan(tmp_path, [user_entry(RecordKind.USER_CANCEL)])
    values: dict[str, object] = {
        "paths": runtime.paths,
        "run_id": RUN,
        "command": (sys.executable, str(script)),
        "workdir": tmp_path,
        "env": host_env(plan, tmp_path / "fake-host-state.json"),
        "timeout_seconds": 60.0,
        "grace_seconds": 1.0,
    }
    values.update(overrides)
    return HeadlessHost(**values)  # type: ignore[arg-type]


def _ledger(env: RuntimeEnv):
    loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
    assert isinstance(loaded, CheckpointLoaded), loaded
    return read_active_trees(loaded.payload)


class TestCommand:
    def test_a_relative_command_is_refused(self, tmp_path: Path) -> None:
        """envを継承しないためPATH解決に依存できない（auto_modeと同じ検査）。"""
        env = _env(tmp_path)
        with pytest.raises(HeadlessError, match="絶対path"):
            _host(env, tmp_path, command=("python",))

    def test_an_empty_command_is_refused(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        with pytest.raises(HeadlessError, match="絶対path"):
            _host(env, tmp_path, command=())

    def test_a_forbidden_flag_is_refused(self, tmp_path: Path) -> None:
        """P-006のruntime choke point。禁止flagはここで落ちる。"""
        env = _env(tmp_path)
        work = _issue(env)
        host = _host(env, tmp_path, command=(sys.executable, "-c", "pass", "--dangerously" + "-skip-permissions"))
        with pytest.raises(ForbiddenFlagError):
            host.execute(work)

    def test_the_envelope_path_is_the_last_argument(self, tmp_path: Path) -> None:
        """子はenvelope pathだけを受け取って結果を組み立てる。"""
        env = _env(tmp_path)
        work = _issue(env)
        raw = _host(env, tmp_path).execute(work)
        submit = json.loads(raw)
        assert submit["request_id"] == work.request.request_id
        assert submit["nonce"] == work.request.nonce


class TestResult:
    def test_the_stdout_is_the_submit_envelope(self, tmp_path: Path) -> None:
        """製品経路で受理されるところまで通す。"""
        env = _env(tmp_path)
        work = _issue(env)
        raw = _host(env, tmp_path).execute(work)
        accepted = submit_result(
            raw, paths=env.paths, config=env.config, ports=env.ports(), accepted_at=ACCEPTED_AT
        )
        assert isinstance(accepted, UserInputAccepted)

    def test_an_empty_stdout_is_refused(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        work = _issue(env)
        host = _host(env, tmp_path, command=(sys.executable, "-c", "pass"))
        with pytest.raises(HeadlessError, match="submit envelopeを出力していない"):
            host.execute(work)

    def test_an_oversized_stdout_is_refused(self, tmp_path: Path) -> None:
        """envelopeはbinding echoとhashだけで、巨大fileを読む理由が無い。"""
        env = _env(tmp_path)
        work = _issue(env)
        host = _host(
            env,
            tmp_path,
            command=(sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"),
        )
        with pytest.raises(HeadlessError, match="上限"):
            host.execute(work)

    def test_a_nonzero_exit_is_refused(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        work = _issue(env)
        script = write_fake_host(tmp_path)
        plan = write_plan(tmp_path, [user_entry(RecordKind.USER_CANCEL)])
        host = _host(
            env,
            tmp_path,
            command=(sys.executable, str(script)),
            env=host_env(plan, tmp_path / "st.json", exit_code=3),
        )
        with pytest.raises(HeadlessError, match="異常終了"):
            host.execute(work)

    def test_a_missing_command_is_reported(self, tmp_path: Path) -> None:
        """起動失敗も構造化する（tracebackにしない）。"""
        env = _env(tmp_path)
        work = _issue(env)
        host = _host(env, tmp_path, command=(str(tmp_path / "does-not-exist.exe"),))
        with pytest.raises(HeadlessError, match="起動できない"):
            host.execute(work)


class TestProcessLedger:
    """ADR-0019 決定10の**書き手**。PR-3b1 / 3b2は読み手だけを持っていた。"""

    def test_the_tree_is_registered_before_waiting(self, tmp_path: Path) -> None:
        """子が自分を台帳の中に見る（親は`wait`で止まっている）。"""
        env = _env(tmp_path)
        work = _issue(env)
        script = write_fake_host(tmp_path)
        plan = write_plan(tmp_path, [user_entry(RecordKind.USER_CANCEL)])
        seen = tmp_path / "ledger-seen.json"
        host = _host(
            env,
            tmp_path,
            command=(sys.executable, str(script)),
            env=host_env(
                plan,
                tmp_path / "st.json",
                ledger_out=seen,
                checkpoint=checkpoint_path(env.paths, RUN),
            ),
        )
        host.execute(work)

        section = json.loads(seen.read_text(encoding="utf-8"))
        assert section is not None, "実行中に台帳へ載っていない"
        assert len(section["trees"]) == 1

    def test_the_tree_is_removed_after_it_exits(self, tmp_path: Path) -> None:
        """残すとpid再利用で別treeへ到達し得る（ADR-0019 決定11）。"""
        env = _env(tmp_path)
        work = _issue(env)
        _host(env, tmp_path).execute(work)
        assert _ledger(env) == ()

    def test_other_trees_are_kept(self, tmp_path: Path) -> None:
        """`with_active_trees`は全置換なので、read-modify-writeでないと他を消す。"""
        other = job_object_ref(pid=9999, job_name="cc-review-other")
        env = _env(tmp_path, extra=with_active_trees({}, [other]))
        work = _issue(env)
        _host(env, tmp_path).execute(work)
        assert _ledger(env) == (other,)

    def test_registration_and_removal_are_idempotent(self, tmp_path: Path) -> None:
        """登録も除去も冪等（同じrefを二重に載せない / 無いrefを外しても失敗しない）。

        adapterは`finally`で除去するので、途中で失敗した経路でも同じ手順を通る。
        """
        ref = job_object_ref()
        added = with_tree_added(with_active_trees({}, [ref]), ref)
        assert not isinstance(added, SectionUnavailable)
        assert read_active_trees(added) == (ref,)

        removed = with_tree_removed(added, ref)
        assert not isinstance(removed, SectionUnavailable)
        assert read_active_trees(removed) == ()
        again = with_tree_removed(removed, ref)
        assert not isinstance(again, SectionUnavailable)
        assert read_active_trees(again) == ()

    def test_an_unreadable_checkpoint_is_reported(self, tmp_path: Path) -> None:
        env = _env(tmp_path)
        work = _issue(env)
        host = _host(env, tmp_path)
        checkpoint_path(env.paths, RUN).unlink()
        with pytest.raises(HeadlessError, match="台帳を更新できない"):
            host.execute(work)

    def test_an_unreadable_ledger_is_reported(self, tmp_path: Path) -> None:
        """停止対象を推測しない（reader側のfail closedをそのまま返す）。"""
        env = _env(tmp_path)
        work = _issue(env)
        host = _host(env, tmp_path)
        loaded = load_checkpoint(checkpoint_path(env.paths, RUN))
        assert isinstance(loaded, CheckpointLoaded)
        payload = dict(loaded.payload)
        payload["processes"] = {"trees": [{"kind": "JOB_OBJECT", "pid": 4242}]}
        save_checkpoint(checkpoint_path(env.paths, RUN), payload)
        with pytest.raises(HeadlessError, match="台帳を更新できない"):
            host.execute(work)


class TestTimeout:
    def test_a_hanging_host_times_out_and_leaves_no_tree(self, tmp_path: Path) -> None:
        """AC-C03-01: timeout経路でもtreeを残さず、台帳も空へ戻す。"""
        env = _env(tmp_path)
        work = _issue(env)
        script = write_fake_host(tmp_path, hang=True)
        plan = write_plan(tmp_path, [user_entry(RecordKind.USER_CANCEL)])
        host = _host(
            env,
            tmp_path,
            command=(sys.executable, str(script)),
            env=host_env(plan, tmp_path / "st.json"),
            timeout_seconds=1.0,
            grace_seconds=0.3,
        )
        with pytest.raises(HeadlessError, match="timeout"):
            host.execute(work)
        assert _ledger(env) == ()


class TestLog:
    def test_the_log_is_redacted(self, tmp_path: Path) -> None:
        """`policy.redaction`が「C-08のlog」を消費者に挙げている実装地点である。"""
        env = _env(tmp_path)
        work = _issue(env)
        script = write_fake_host(tmp_path)
        plan = write_plan(tmp_path, [user_entry(RecordKind.USER_CANCEL)])
        host = _host(
            env,
            tmp_path,
            command=(sys.executable, str(script)),
            env=host_env(plan, tmp_path / "st.json", stderr_note=f"token={FAKE_TOKEN}"),
        )
        host.execute(work)

        directory = work.envelope_path.parent
        log = (directory / LOG_FILE).read_text(encoding="utf-8")
        assert FAKE_TOKEN not in log
        assert "[REDACTED:" in log
        # rawは残る（収集対象ではない。artifact contract testが固定する）
        assert FAKE_TOKEN in (directory / STDERR_FILE).read_text(encoding="utf-8")

    def test_the_streams_are_separate_files(self, tmp_path: Path) -> None:
        """stdoutはデータ、stderrはlog。混ぜるとsubmit envelopeが壊れる。"""
        env = _env(tmp_path)
        work = _issue(env)
        _host(env, tmp_path).execute(work)
        directory = work.envelope_path.parent
        assert json.loads((directory / STDOUT_FILE).read_text(encoding="utf-8"))["run_id"] == RUN
        assert (directory / LOG_FILE).exists()
