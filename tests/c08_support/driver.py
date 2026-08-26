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
"""

from __future__ import annotations

import dataclasses
import json
import sys
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
from claude_code_codex_review_loop.state import prepare_state_root  # noqa: E402
from claude_code_codex_review_loop.workflow import (  # noqa: E402
    AwaitUser,
    EngineStopped,
    HostActionIssued,
    Terminal,
)

RUN = "run-1"


def _ports(paths, config, *, stop_fails: bool):
    """まだ実装の無い2 portだけをfakeで補う（残りは製品実装）。"""
    from c08_support.runtime import QUESTION_COMMENT_ID, AgentRecordBody

    stop = FakeStopPort(fails=frozenset({job_object_ref()}) if stop_fails else frozenset())
    return dataclasses.replace(
        default_ports(paths, config),
        payload=FakeActionPayloads(
            {"ANSWER_GATE_QUESTION": {"question_comment_id": QUESTION_COMMENT_ID}}
        ),
        body=AgentRecordBody(),
        stop=stop,
    )


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
