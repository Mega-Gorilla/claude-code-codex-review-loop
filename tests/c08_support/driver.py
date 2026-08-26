# SPDX-License-Identifier: Apache-2.0
"""別processでruntimeを1 stepだけ動かすdriver（AC-C08-06のtest用）。

`python -m ...runtime`は`default_ports`を使うため、まだ実装の無い2 port
（action payloadとagent recordの本文）を要する経路——`HOST_ACTION`と、停止に失敗した
状態を作る停止port——を通せない。そこで**同じ製品関数**（`step` / `submit_result` /
`halt`）をtest所有のportで呼ぶdriverを用意する。

processとしては`__main__`と同じで、共有するのはstate rootとGitHubだけである。**resume
機構そのものは製品code**で、fakeなのはまだ実装が無いportに限る。

```
python tests/c08_support/driver.py <state-root> <command> [args...]
```

`wait-for-signal`は実signalを受け取るためのmodeで、handlerを設置してから`READY`を1行
出力する。呼び出し側はその行を待ってからsignalを送るので、timingに依存しない。
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[1]
if str(_TESTS) not in sys.path:  # pragma: no cover - 別processでのimport path解決
    sys.path.insert(0, str(_TESTS))

from c08_support.helpers import FakeStopPort, job_object_ref  # noqa: E402
from c08_support.runtime import (  # noqa: E402
    ACCEPTED_AT,
    ISSUED_AT,
    FakeActionPayloads,
    FakeIds,
)
from claude_code_codex_review_loop.runtime import (  # noqa: E402
    default_ports,
    read_session_config,
    step,
    submit_result,
)
from claude_code_codex_review_loop.runtime.signals import (  # noqa: E402
    StopSignal,
    install_stop_handler,
)
from claude_code_codex_review_loop.state import prepare_state_root  # noqa: E402
from claude_code_codex_review_loop.workflow import (  # noqa: E402
    AwaitUser,
    EngineStopped,
    HostActionIssued,
    Terminal,
)

RUN = "run-1"


def _ports(paths, config, *, stop_fails: bool, real_stop: bool = False):
    """まだ実装の無い2 portだけをfakeで補う（残りは製品実装）。

    `real_stop`は製品の`TreeStopper`をそのまま使う（AC-C03-02のE2E）。台帳へ実在する
    treeのrefが入っている場合だけ使ってよい。
    """
    from c08_support.runtime import QUESTION_COMMENT_ID, AgentRecordBody

    ports = dataclasses.replace(
        default_ports(paths, config),
        payload=FakeActionPayloads(
            {"ANSWER_GATE_QUESTION": {"question_comment_id": QUESTION_COMMENT_ID}}
        ),
        body=AgentRecordBody(),
    )
    if real_stop:
        return ports
    stop = FakeStopPort(fails=frozenset({job_object_ref()}) if stop_fails else frozenset())
    return dataclasses.replace(ports, stop=stop)


def _describe(outcome: object) -> dict[str, object]:
    if isinstance(outcome, HostActionIssued):
        return {
            "outcome": "HOST_ACTION",
            "action_id": outcome.action.action_id,
            "action_kind": outcome.action.action_kind,
            "envelope_path": str(outcome.envelope_path),
            "result_path": str(outcome.result_path),
            "reissued": outcome.reissued,
        }
    if isinstance(outcome, AwaitUser):
        return {
            "outcome": "AWAIT_USER",
            "request_id": outcome.request.request_id,
            "envelope_path": str(outcome.envelope_path),
            "result_path": str(outcome.result_path),
            "reissued": outcome.reissued,
        }
    if isinstance(outcome, Terminal):
        return {"outcome": "TERMINAL", "state": outcome.state.value}
    if isinstance(outcome, EngineStopped):
        return {"outcome": "STOPPED", "code": outcome.code, "detail": outcome.detail}
    return {"outcome": type(outcome).__name__}


def _wait_for_signal(paths, config, ports) -> int:
    """handlerを設置し、実signalを受け取ってから1 stepだけ進める。

    `READY`を出してから待つので、呼び出し側は「handler設置済み」を確かめてsignalを送れる。
    """
    with install_stop_handler(StopSignal()) as stop:
        print("READY", flush=True)
        while not stop.requested:
            time.sleep(0.02)
        try:
            result = step(
                paths=paths,
                config=config,
                ports=ports,
                id_source=FakeIds("sig"),
                issued_at=ISSUED_AT,
                stop=stop,
            )
        except KeyboardInterrupt:
            # 2回目のsignalが停止の外側へ届いた場合。要求は台帳へ残っており、次のresumeが
            # 停止をやり直す（`__main__`と同じ最後の網）
            print(json.dumps({"outcome": "STOPPED", "code": "forced_stop"}, sort_keys=True))
            return 0
    payload = _describe(result.outcome)
    payload["stop_requested"] = result.trace.stop_requested
    payload["stopped"] = result.trace.stopped
    print(json.dumps(payload, sort_keys=True))
    return 0


def main(argv: list[str]) -> int:
    state_root, command = argv[1], argv[2]
    paths = prepare_state_root(Path(state_root).resolve())
    config = read_session_config(paths, RUN)
    stop_fails = command == "advance-stop-fails"
    ports = _ports(paths, config, stop_fails=stop_fails)
    if command.startswith("advance"):
        result = step(
            paths=paths,
            config=config,
            ports=ports,
            id_source=FakeIds(argv[3] if len(argv) > 3 else "drv"),
            issued_at=ISSUED_AT,
        )
        payload = _describe(result.outcome)
        payload["persisted"] = list(result.trace.persisted)
        payload["halted"] = result.trace.halted
        payload["stop_requested"] = result.trace.stop_requested
        payload["stopped"] = result.trace.stopped
    elif command == "wait-for-signal":
        return _wait_for_signal(paths, config, ports)
    elif command == "wait-for-signal-real-stop":
        return _wait_for_signal(paths, config, _ports(paths, config, stop_fails=False, real_stop=True))
    elif command == "submit":
        raw = Path(argv[3]).read_bytes()
        outcome = submit_result(
            raw, paths=paths, config=config, ports=ports, accepted_at=ACCEPTED_AT
        )
        payload = {"outcome": type(outcome).__name__}
        if isinstance(outcome, EngineStopped):
            payload = {"outcome": "STOPPED", "code": outcome.code, "detail": outcome.detail}
    else:  # pragma: no cover - 呼び出し側の誤り
        raise SystemExit(f"未知のcommand: {command}")
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - 別processの起動行
    raise SystemExit(main(sys.argv))
