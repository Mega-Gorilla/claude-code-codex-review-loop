# SPDX-License-Identifier: Apache-2.0
"""AC-C08-06: 別processからのresume（Phase 8 PR-3b1。ADR-0020）。

**1つのrunを4つの別processが順に進めて完走させる**。同一process内でengine関数を呼び直す
だけでは、in-memory stateへの依存が残っていても通ってしまうため証明にならない。

processが共有するのは**state root（checkpointとsession config）とGitHub**だけである。
各processはcwdも違い、前のprocessのobjectを一切引き継がない。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from c07_support.helpers import RUN
from c08_support.helpers import user_machine_state
from c08_support.runtime import RuntimeEnv, runtime_env, user_submit_for

from claude_code_codex_review_loop.domain.values import Awaiting, RecordKind, State
from claude_code_codex_review_loop.runtime import __main__ as entry

MODULE = "claude_code_codex_review_loop.runtime"


def _process(env: RuntimeEnv, cwd: Path, *argv: str) -> tuple[int, dict[str, object]]:
    """新しいprocessでentry pointを1回だけ実行する（cwdも変える）。"""
    cwd.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(  # noqa: S603 - 起動するのは自分自身のmodule
        [sys.executable, "-m", MODULE, *argv, "--state-root", str(env.paths.root), "--run", RUN],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
        check=False,
    )
    assert completed.stdout, completed.stderr
    payload = json.loads(completed.stdout.splitlines()[-1])
    assert isinstance(payload, dict)
    return completed.returncode, payload


def test_four_processes_carry_one_run_to_the_end(tmp_path: Path) -> None:
    """発行 -> 再提示 -> 応答 -> 完走を、それぞれ別のprocessが行う。"""
    env = runtime_env(
        tmp_path,
        state=user_machine_state(Awaiting.USER_INPUT_GATE),
        seeded=(RecordKind.FINAL_REPORT,),
    )
    before = len(env.comments())

    # process A: ユーザー入力を要求して終了する
    code, issued = _process(env, tmp_path / "a", "advance")
    assert code == entry.EXIT_OK
    assert issued["outcome"] == "AWAIT_USER"
    assert issued["reissued"] is False

    # process B: 応答が無いまま再開する。**同じrequestを再提示**し、新しい要求を作らない
    code, again = _process(env, tmp_path / "b", "advance")
    assert code == entry.EXIT_OK
    assert again["request_id"] == issued["request_id"]
    assert again["reissued"] is True
    assert again["envelope_path"] == issued["envelope_path"]
    assert len(env.comments()) == before  # 再提示でGitHubへは何も足さない

    # process C: 別processが用意した応答を受理する
    submit_file = tmp_path / "submit.json"
    submit_file.write_text(
        json.dumps(user_submit_for(str(again["envelope_path"]), str(again["result_path"]))),
        encoding="utf-8",
    )
    code, accepted = _process(env, tmp_path / "c", "submit", "--result", str(submit_file))
    assert code == entry.EXIT_OK
    assert accepted["outcome"] == "ACCEPTED"

    # process D: 永続化と停止をこなして終端まで進める
    code, done = _process(env, tmp_path / "d", "advance")
    assert code == entry.EXIT_OK
    assert done["outcome"] == "TERMINAL"
    assert done["state"] == State.CANCELLED.value
    assert len(list(done["persisted"])) == 1  # type: ignore[call-overload]
    assert done["halted"] == 1
    assert len(env.comments()) == before + 1


def test_a_finished_run_is_reported_the_same_by_any_process(tmp_path: Path) -> None:
    """終端runへの再開は冪等である（重複投稿も新しい要求も起こさない）。"""
    env = runtime_env(
        tmp_path,
        state=user_machine_state(Awaiting.USER_INPUT_GATE),
        seeded=(RecordKind.FINAL_REPORT,),
    )
    _, issued = _process(env, tmp_path / "a", "advance")
    submit_file = tmp_path / "submit.json"
    submit_file.write_text(
        json.dumps(user_submit_for(str(issued["envelope_path"]), str(issued["result_path"]))),
        encoding="utf-8",
    )
    _process(env, tmp_path / "b", "submit", "--result", str(submit_file))
    _, first = _process(env, tmp_path / "c", "advance")
    posted = len(env.comments())

    _, second = _process(env, tmp_path / "d", "advance")
    assert first["state"] == second["state"] == State.CANCELLED.value
    assert second["persisted"] == []
    assert second["halted"] == 0
    assert len(env.comments()) == posted
