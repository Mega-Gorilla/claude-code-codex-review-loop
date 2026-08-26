# SPDX-License-Identifier: Apache-2.0
"""process entry pointの受入test（Phase 8 PR-3b1。ADR-0020）。

**実subprocess**で`python -m claude_code_codex_review_loop.runtime`を起動する。同一process内で
`main()`を呼ぶだけでは、moduleとしてimportできること・引数解析・終了codeが証明できない。

`AC-C08-03`（制御経路は`step`と`submit`の2つだけ）はASTのcontract testで固定する。
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
from c07_support.helpers import RUN
from c08_support.helpers import integrity_block, machine_state, user_machine_state
from c08_support.runtime import (
    ISSUED_AT,
    FakeIds,
    RuntimeEnv,
    gate_host,
    round_ports,
    runtime_env,
    user_submit_for,
)

from claude_code_codex_review_loop.domain.values import Awaiting, MachineState, RecordKind, State
from claude_code_codex_review_loop.runtime import __main__ as entry
from claude_code_codex_review_loop.runtime import step, submit_result
from claude_code_codex_review_loop.workflow import (
    AwaitUser,
    Blocked,
    ConsumedIntent,
    EngineStopped,
    HostActionIssued,
    Terminal,
    UserInputReplayed,
    UserIntentAlreadyRecorded,
    UserRequestReceipt,
)

MODULE = "claude_code_codex_review_loop.runtime"
GATE_STATE = user_machine_state(Awaiting.USER_INPUT_GATE)


def _run(env: RuntimeEnv, *argv: str) -> tuple[int, dict[str, object]]:
    """entry pointを別processで起動し、終了codeと構造化出力を返す。"""
    completed = subprocess.run(  # noqa: S603 - 起動するのは自分自身のmodule
        [sys.executable, "-m", MODULE, *argv, "--state-root", str(env.paths.root), "--run", RUN],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.stdout, completed.stderr
    payload = json.loads(completed.stdout.splitlines()[-1])
    assert isinstance(payload, dict)
    return completed.returncode, payload


def _gate_env(tmp_path: Path) -> RuntimeEnv:
    return runtime_env(tmp_path, state=GATE_STATE, seeded=(RecordKind.FINAL_REPORT,))


class TestAdvance:
    def test_a_user_request_is_reported_with_its_paths(self, tmp_path: Path) -> None:
        """hostが応答するのに必要なpathとIDが構造化出力で返る。"""
        env = _gate_env(tmp_path)
        code, payload = _run(env, "advance")
        assert code == entry.EXIT_OK
        assert payload["outcome"] == "AWAIT_USER"
        assert payload["awaiting"] == Awaiting.USER_INPUT_GATE.value
        assert payload["reissued"] is False
        assert Path(str(payload["envelope_path"])).is_file()
        assert payload["persisted"] == []
        assert payload["halted"] == 0

    def test_a_terminal_run_is_reported_as_terminal(self, tmp_path: Path) -> None:
        env = runtime_env(tmp_path, state=MachineState(state=State.CANCELLED))
        code, payload = _run(env, "advance")
        assert code == entry.EXIT_OK
        assert payload == {
            "outcome": "TERMINAL",
            "state": "CANCELLED",
            "persisted": [],
            "halted": 0,
        }

    def test_a_missing_port_exits_with_the_stop_code(self, tmp_path: Path) -> None:
        """進めない理由は**構造化outcome**から終了codeを決める（P-003）。"""
        env = runtime_env(tmp_path, state=machine_state(), seeded=(RecordKind.REVIEW_RESULT,))
        code, payload = _run(env, "advance")
        assert code == entry.EXIT_STOPPED
        assert payload["outcome"] == "STOPPED"
        assert payload["code"] == "port_unavailable"

    def test_a_missing_session_config_stops(self, tmp_path: Path) -> None:
        """entry pointは既定値を補わない（設定の解決はC-12の領域）。"""
        env = _gate_env(tmp_path)
        (env.run_dir / "session.json").unlink()
        code, payload = _run(env, "advance")
        assert code == entry.EXIT_STOPPED
        assert payload["code"] == "config_unavailable"


class TestSubmit:
    def test_the_round_trip_advances_the_run(self, tmp_path: Path) -> None:
        """advance -> （hostの応答）-> submit -> advanceで終端まで進む。"""
        env = _gate_env(tmp_path)
        _, issued = _run(env, "advance")
        submit_file = tmp_path / "submit.json"
        submit_file.write_text(
            json.dumps(
                user_submit_for(str(issued["envelope_path"]), str(issued["result_path"]))
            ),
            encoding="utf-8",
        )

        code, accepted = _run(env, "submit", "--result", str(submit_file))
        assert code == entry.EXIT_OK
        assert accepted["outcome"] == "ACCEPTED"
        assert accepted["state"] == State.READY_FOR_HUMAN_MERGE.value
        assert "PersistRecord" in accepted["commands"]

        code, done = _run(env, "advance")
        assert code == entry.EXIT_OK
        assert done["outcome"] == "TERMINAL"
        assert done["state"] == State.CANCELLED.value
        assert len(list(done["persisted"])) == 1  # type: ignore[call-overload]
        assert done["halted"] == 1

    def test_the_same_submit_twice_is_a_replay(self, tmp_path: Path) -> None:
        """同じsubmitの再投入は新しい遷移を起こさない（別processからの再送でも同じ）。"""
        env = _gate_env(tmp_path)
        _, issued = _run(env, "advance")
        submit_file = tmp_path / "submit.json"
        submit_file.write_text(
            json.dumps(
                user_submit_for(str(issued["envelope_path"]), str(issued["result_path"]))
            ),
            encoding="utf-8",
        )
        _run(env, "submit", "--result", str(submit_file))
        code, again = _run(env, "submit", "--result", str(submit_file))
        assert code == entry.EXIT_OK
        assert again["outcome"] == "REPLAYED"

    def test_submit_without_a_result_is_a_usage_error(self, tmp_path: Path) -> None:
        env = _gate_env(tmp_path)
        code, payload = _run(env, "submit")
        assert code == entry.EXIT_USAGE
        assert payload["code"] == "usage"

    def test_an_unreadable_result_stops_instead_of_raising(self, tmp_path: Path) -> None:
        """読込失敗でtracebackにしない（進退は終了codeと構造化結果だけで決まる）。"""
        env = _gate_env(tmp_path)
        code, payload = _run(env, "submit", "--result", str(tmp_path / "missing.json"))
        assert code == entry.EXIT_STOPPED
        assert payload["code"] == "submit_unreadable"

    def test_an_oversized_result_is_refused_before_reading(self, tmp_path: Path) -> None:
        """envelopeはbinding echoとhashだけで、巨大fileを読む理由が無い。"""
        env = _gate_env(tmp_path)
        oversized = tmp_path / "big.json"
        oversized.write_bytes(b"{" + b" " * (entry.MAX_SUBMIT_BYTES + 1) + b"}")
        code, payload = _run(env, "submit", "--result", str(oversized))
        assert code == entry.EXIT_STOPPED
        assert payload["code"] == "submit_too_large"

    def test_an_unknown_command_is_rejected_by_the_parser(self, tmp_path: Path) -> None:
        env = _gate_env(tmp_path)
        completed = subprocess.run(  # noqa: S603 - 起動するのは自分自身のmodule
            [sys.executable, "-m", MODULE, "merge", "--state-root", str(env.paths.root), "--run", RUN],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == entry.EXIT_USAGE
        assert completed.stdout == ""


class TestControlPaths:
    """AC-C08-03: entry pointが呼ぶengine経路は`step`と`submit`の2つだけである。"""

    ENGINE_ONLY = frozenset({"advance", "persist", "halt", "submit", "transition", "drive"})

    def _calls(self) -> set[str]:
        source = Path(entry.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Attribute):
                    names.add(target.attr)
        return names

    def test_only_the_two_control_paths_are_called(self) -> None:
        called = self._calls()
        assert {"step", "submit_result"} <= called
        assert self.ENGINE_ONLY & called == set()

    def test_no_round_loop_lives_in_the_entry_point(self) -> None:
        """round orchestrationは`drive`に1つだけ（P-002）。entry pointはloopを持たない。"""
        tree = ast.parse(Path(entry.__file__).read_text(encoding="utf-8"))
        loops = [node for node in ast.walk(tree) if isinstance(node, (ast.While, ast.For))]
        assert loops == []


class TestRendering:
    """outcomeごとの表示。`advance` / `submit`の全variantが**別のtagで**出ることを固定する。

    表示はentry pointの3責務の1つで、ここが漏れるとhostは`STOPPED`との区別を失う。
    variantによってはsubprocessで作るのが難しいため、写像だけを直接確かめる。
    """

    def test_every_advance_outcome_has_its_own_tag(self, tmp_path: Path) -> None:
        outcomes = [
            _host_action(tmp_path),
            _await_user(tmp_path),
            Terminal(state=State.CANCELLED),
            Blocked(block=integrity_block()),
            EngineStopped("port_unavailable", "C-10が持つ"),
        ]
        payloads = [entry._advance_payload(outcome) for outcome in outcomes]
        assert [payload["outcome"] for payload in payloads] == [
            "HOST_ACTION",
            "AWAIT_USER",
            "TERMINAL",
            "BLOCKED",
            "STOPPED",
        ]
        assert payloads[0]["action_kind"] == "ANSWER_GATE_QUESTION"
        assert payloads[3]["block"] == "RecordIntegrityBlock"

    def test_every_submit_outcome_has_its_own_tag(self) -> None:
        receipt = UserRequestReceipt(
            request_id="req-1", nonce="n-1", submit_hash="s-1", result_hash="r-1"
        )
        consumed = ConsumedIntent(
            intent_key="ui:run-1", binding="cr:run-1:2:gate", route="github_comment"
        )
        outcomes = [
            UserInputReplayed(receipt=receipt),
            UserIntentAlreadyRecorded(consumed=consumed),
            EngineStopped("port_unavailable", "C-13が持つ"),
        ]
        payloads = [entry._submit_payload(outcome) for outcome in outcomes]
        assert [payload["outcome"] for payload in payloads] == [
            "REPLAYED",
            "ALREADY_RECORDED",
            "STOPPED",
        ]
        assert payloads[1]["route"] == "github_comment"


def _await_user(tmp_path: Path) -> AwaitUser:
    env = _gate_env(tmp_path / "await")
    outcome = step(
        paths=env.paths,
        config=env.config,
        ports=env.ports(),
        id_source=FakeIds("req"),
        issued_at=ISSUED_AT,
    ).outcome
    assert isinstance(outcome, AwaitUser)
    return outcome


def _host_action(tmp_path: Path) -> HostActionIssued:
    """gate質問を1 round進めて`HOST_ACTION`を発行させる。"""
    env = _gate_env(tmp_path / "action")
    ports = round_ports(env)
    host = gate_host(env)
    issued = step(
        paths=env.paths,
        config=env.config,
        ports=ports,
        id_source=FakeIds("req"),
        issued_at=ISSUED_AT,
    ).outcome
    assert isinstance(issued, AwaitUser)
    submit_result(
        host.execute(issued),
        paths=env.paths,
        config=env.config,
        ports=ports,
        accepted_at="2026-08-26T09:05:00Z",
    )
    outcome = step(
        paths=env.paths,
        config=env.config,
        ports=ports,
        id_source=FakeIds("act"),
        issued_at=ISSUED_AT,
    ).outcome
    assert isinstance(outcome, HostActionIssued)
    return outcome


@pytest.mark.parametrize("command", ["advance", "submit"])
def test_the_parser_accepts_both_commands(command: str) -> None:
    args = entry._parser().parse_args([command, "--state-root", "root", "--run", RUN])
    assert args.command == command
    assert args.result is None
