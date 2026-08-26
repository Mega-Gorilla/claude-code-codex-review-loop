# SPDX-License-Identifier: Apache-2.0
"""process entry point（Phase 8 PR-3b1。案B。ADR-0020）。

```
python -m claude_code_codex_review_loop.runtime advance --state-root <path> --run <id>
python -m claude_code_codex_review_loop.runtime submit  --state-root <path> --run <id> --result <path>
```

CLI本体（`cc-review`）の正本はC-12であり、Phase 8が先取りすると二重実装になる。ここが持つのは
**P-002の3責務だけ**である。

1. 引数解析
2. session boundaryの受け渡し（run directoryのsession configとcheckpointを開く）
3. 表示（構造化JSONを標準出力へ）

state遷移・round管理・GitHub投稿の判断はすべて`runtime.session`（さらにその先のengine）に
あり、ここには無い。呼ぶのは`step`と`submit`の2つだけである（AC-C08-03）。

終了codeは**構造化outcomeから決める**（P-003。出力文字列の部分一致で分類しない）。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from ..state import prepare_state_root
from ..workflow import (
    AwaitUser,
    Blocked,
    EngineStopped,
    HostActionIssued,
    SubmitAccepted,
    SubmitReplayed,
    Terminal,
    UserInputAccepted,
    UserInputReplayed,
    UserIntentAlreadyRecorded,
)
from .config import ConfigUnavailable, read_session_config
from .ports import default_ports
from .session import step, submit_result

EXIT_OK = 0
EXIT_STOPPED = 3
EXIT_USAGE = 2


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude_code_codex_review_loop.runtime")
    parser.add_argument("command", choices=("advance", "submit"))
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--result", help="submitの入力（submit envelope file）")
    return parser


def _render(payload: dict[str, object]) -> None:
    """構造化結果を1行のJSONで出す（表示はentry pointの責務）。

    **非ASCIIはescapeする**。この出力は人向けの表示ではなく呼び出し側が解釈する構造化結果で、
    stdoutのencodingはhostのlocale（Windowsのconsole code page等）で決まる。日本語のdetailを
    そのまま書くとlocale次第で化けるか書けなくなるため、JSONのescapeでASCIIへ閉じる。
    """
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")


def _advance_payload(outcome: object) -> dict[str, object]:
    if isinstance(outcome, HostActionIssued):
        return {
            "outcome": "HOST_ACTION",
            "action_kind": outcome.action.action_kind,
            "action_id": outcome.action.action_id,
            "envelope_path": str(outcome.envelope_path),
            "result_path": str(outcome.result_path),
            "reissued": outcome.reissued,
        }
    if isinstance(outcome, AwaitUser):
        return {
            "outcome": "AWAIT_USER",
            "awaiting": outcome.awaiting.value,
            "request_id": outcome.request.request_id,
            "envelope_path": str(outcome.envelope_path),
            "result_path": str(outcome.result_path),
            "reissued": outcome.reissued,
        }
    if isinstance(outcome, Terminal):
        return {"outcome": "TERMINAL", "state": outcome.state.value}
    if isinstance(outcome, Blocked):
        return {"outcome": "BLOCKED", "block": type(outcome.block).__name__}
    stopped = outcome
    assert isinstance(stopped, EngineStopped)
    return {"outcome": "STOPPED", "code": stopped.code, "detail": stopped.detail}


def _submit_payload(outcome: object) -> dict[str, object]:
    if isinstance(outcome, (SubmitAccepted, UserInputAccepted)):
        return {
            "outcome": "ACCEPTED",
            "state": outcome.machine_state.state.value,
            "commands": [type(command).__name__ for command in outcome.commands],
        }
    if isinstance(outcome, (SubmitReplayed, UserInputReplayed)):
        return {"outcome": "REPLAYED"}
    if isinstance(outcome, UserIntentAlreadyRecorded):
        return {"outcome": "ALREADY_RECORDED", "route": outcome.consumed.route}
    stopped = outcome
    assert isinstance(stopped, EngineStopped)
    return {"outcome": "STOPPED", "code": stopped.code, "detail": stopped.detail}


def main(argv: Sequence[str] | None = None) -> int:
    """1回のadvanceまたはsubmitを実行して結果を出す（loopを持たない）。"""
    args = _parser().parse_args(argv)
    paths = prepare_state_root(Path(args.state_root).resolve())
    config = read_session_config(paths, args.run)
    if isinstance(config, ConfigUnavailable):
        _render({"outcome": "STOPPED", "code": "config_unavailable", "detail": config.detail})
        return EXIT_STOPPED
    ports = default_ports(config)
    if args.command == "advance":
        result = step(
            paths=paths,
            config=config,
            ports=ports,
            id_source=lambda: uuid.uuid4().hex,
            issued_at=_now(),
        )
        payload = _advance_payload(result.outcome)
        payload["persisted"] = list(result.trace.persisted)
        payload["halted"] = result.trace.halted
        _render(payload)
        return EXIT_STOPPED if payload["outcome"] == "STOPPED" else EXIT_OK
    if args.result is None:
        _render({"outcome": "STOPPED", "code": "usage", "detail": "submitは--resultを要する"})
        return EXIT_USAGE
    raw = Path(args.result).read_bytes()
    payload = _submit_payload(
        submit_result(raw, paths=paths, config=config, ports=ports, accepted_at=_now())
    )
    _render(payload)
    return EXIT_STOPPED if payload["outcome"] == "STOPPED" else EXIT_OK


if __name__ == "__main__":  # pragma: no cover - process entry pointの起動行
    raise SystemExit(main())
