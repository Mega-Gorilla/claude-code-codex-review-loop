# SPDX-License-Identifier: Apache-2.0
"""AC-C08-06: 別processからのresume（Phase 8 PR-3b1。ADR-0020）。

同一process内でengine関数を呼び直すだけでは、in-memory stateへの依存が残っていても
通ってしまうため証明にならない。processが共有するのは**state root（checkpointと
session config）とGitHub**だけで、各processはcwdも違う。

Issue #13が挙げる中断点を3つとも覆う。

| 中断点 | test |
| --- | --- |
| pending user request | `test_four_processes_carry_one_run_to_the_end` |
| pending `HOST_ACTION` | `test_a_pending_host_action_is_re_presented_by_another_process` |
| 停止procedureの途中 | `test_a_halt_procedure_resumes_in_another_process` |

後ろ2つは**test所有のdriver process**を使う。`python -m ...runtime`は`default_ports`を
使うため、まだ実装の無い2 port（action payloadとagent recordの本文）を要する経路を
通せないためである。fakeなのはそのportだけで、resume機構は製品codeが動く。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from c07_support.helpers import RUN
from c08_support.helpers import cancelling, job_object_ref, user_machine_state
from c08_support.runtime import RuntimeEnv, runtime_env, user_submit_for

from claude_code_codex_review_loop.domain.values import Awaiting, RecordKind, State
from claude_code_codex_review_loop.runtime import __main__ as entry
from claude_code_codex_review_loop.workflow import with_active_trees

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


DRIVER = Path(__file__).resolve().parent / "c08_support" / "driver.py"


def _driver(env: RuntimeEnv, cwd: Path, *argv: str) -> dict[str, object]:
    """test所有のdriverを別processで1回だけ動かす。

    `python -m ...runtime`は`default_ports`を使うため、まだ実装の無い2 port
    （action payloadとagent recordの本文）を要する経路を通せない。driverはその2つと
    停止portだけをfakeにして、**同じ製品関数**（`step` / `submit_result`）を呼ぶ。
    """
    cwd.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(  # noqa: S603 - 起動するのは自分たちのtest driver
        [sys.executable, str(DRIVER), str(env.paths.root), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.splitlines()[-1])
    assert isinstance(payload, dict)
    return payload


def test_a_pending_host_action_is_re_presented_by_another_process(tmp_path: Path) -> None:
    """**AC-C08-06（pending action）**: 未完了`HOST_ACTION`を別processが再提示する。

    ADR-0014 決定22の「resumeは同じactionを再提示し、新しいactionを作らない」が
    process境界を越えて成り立つことを固定する。
    """
    env = runtime_env(
        tmp_path,
        state=user_machine_state(Awaiting.USER_INPUT_GATE),
        seeded=(RecordKind.FINAL_REPORT,),
    )
    # process A: gate質問を受け取り、`HOST_ACTION`を発行したところで終了する
    issued = _driver(env, tmp_path / "a", "advance", "a")
    submit_file = tmp_path / "question.json"
    submit_file.write_text(
        json.dumps(
            user_submit_for(
                str(issued["envelope_path"]), str(issued["result_path"]), RecordKind.GATE_QUESTION
            )
        ),
        encoding="utf-8",
    )
    _driver(env, tmp_path / "b", "submit", str(submit_file))
    action = _driver(env, tmp_path / "c", "advance", "c")
    assert action["outcome"] == "HOST_ACTION"
    assert action["reissued"] is False

    # process D: 応答が無いまま再開する。**同じaction IDを再提示**する
    again = _driver(env, tmp_path / "d", "advance", "d")
    assert again["outcome"] == "HOST_ACTION"
    assert again["action_id"] == action["action_id"]
    assert again["reissued"] is True
    assert again["envelope_path"] == action["envelope_path"]


def test_a_halt_procedure_resumes_in_another_process(tmp_path: Path) -> None:
    """**AC-C08-06（procedure途中）**: 停止に失敗したrunを別processが停止し直す。

    停止意図はcheckpointに残り（`CancellingProcedure`）、次のprocessがC-01のX系列ruleで
    `HaltRun`を冪等に再発行して完了させる。
    """
    ref = job_object_ref()
    env = runtime_env(
        tmp_path, state=cancelling(), extra=with_active_trees({}, [ref])
    )
    # process A: treeを止められず、stateは手続き中のまま残る
    failed = _driver(env, tmp_path / "a", "advance-stop-fails", "a")
    assert failed["outcome"] == "STOPPED"
    assert failed["code"] == "halt_failed"

    # process B: 同じrunを引き継ぎ、停止をやり直して終端へ進める
    done = _driver(env, tmp_path / "b", "advance", "b")
    assert done["outcome"] == "TERMINAL"
    assert done["state"] == State.CANCELLED.value
    assert done["halted"] == 1


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
