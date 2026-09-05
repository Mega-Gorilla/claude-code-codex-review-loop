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
    EmergencyStopRequired,
    EngineStopped,
    HostActionIssued,
    SubmitAccepted,
    SubmitReplayed,
    Terminal,
    UserInputAccepted,
    UserInputReplayed,
    UserIntentAlreadyRecorded,
)
from .agent_session import AgentExecution
from .config import ConfigUnavailable, read_session_config
from .ports import default_ports
from .session import step, submit_result
from .signals import StopSignal, install_stop_handler

EXIT_OK = 0
EXIT_STOPPED = 3
EXIT_USAGE = 2

# submit envelopeの読込上限。envelopeはbinding echoとhashだけで、結果本体はresult fileにある
MAX_SUBMIT_BYTES = 64 * 1024


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude_code_codex_review_loop.runtime")
    parser.add_argument("command", choices=("advance", "submit"))
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--result", help="submitの入力（submit envelope file）")
    parser.add_argument(
        "--active-provider", choices=("claude", "codex"),
        help="選択済みrunのHOST_ACTION事前検証用。選択なしrun・submitでは未使用（provider設定は変更しない）",
    )
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
        # 待機の識別子は直和（ADR-0023）。`BLOCKED`での介入待ちはawaitingを持たず、
        # 解除対象のblock bindingが識別子になる
        wait: dict[str, object] = (
            {"block_binding": outcome.request.block_binding}
            if outcome.awaiting is None
            else {"awaiting": outcome.awaiting.value}
        )
        return {
            "outcome": "AWAIT_USER",
            **wait,
            "request_id": outcome.request.request_id,
            "envelope_path": str(outcome.envelope_path),
            "result_path": str(outcome.result_path),
            "reissued": outcome.reissued,
        }
    if isinstance(outcome, Terminal):
        return {"outcome": "TERMINAL", "state": outcome.state.value}
    if isinstance(outcome, Blocked):
        return {"outcome": "BLOCKED", "block": type(outcome.block).__name__}
    if isinstance(outcome, EmergencyStopRequired):  # pragma: no cover - `step`が実行してから返す
        # `step`はこれを自分でこなすので、表示へ届くのは上限に達した場合だけである
        return {"outcome": "EMERGENCY_STOP", "requested_at": outcome.request.requested_at}
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


def _read_submit(path: Path) -> bytes | dict[str, object]:
    """submit envelopeを読む（読めなければ構造化結果へ写す）。

    tracebackで落ちるとprocess境界の契約が崩れる。呼び出し側は終了codeと標準出力の
    構造化結果だけで進退を決められなければならない。

    **読む前にsizeを検査する**。envelopeはbinding echoとhashだけの小さなJSONで、
    巨大fileを丸ごとmemoryへ載せる理由が無い（結果本体は`result_hash`で参照され、その
    上限は`max_result_bytes`がsubmit側で検査する）。
    """
    try:
        size = path.stat().st_size
        if size > MAX_SUBMIT_BYTES:
            return {
                "outcome": "STOPPED",
                "code": "submit_too_large",
                "detail": f"submit envelopeが上限{MAX_SUBMIT_BYTES}byteを超える: {size}",
            }
        return path.read_bytes()
    except OSError as error:
        return {
            "outcome": "STOPPED",
            "code": "submit_unreadable",
            "detail": f"submit envelopeを読めない: {error.strerror or type(error).__name__}",
        }


def main(argv: Sequence[str] | None = None) -> int:
    """1回のadvanceまたはsubmitを実行して結果を出す（loopを持たない）。

    2回目のCtrl+Cは`KeyboardInterrupt`として届く（AC-C03-02の即時force要求）。停止の完了は
    `step`が引き受けるので（`_forced_stop`）、ここは**最後の網**である。tracebackで落ちると
    process境界の契約が崩れる。
    """
    stop = StopSignal()
    try:
        return _run(argv, stop)
    except KeyboardInterrupt:
        # **`StopSignal`を見て分類する**。handler設置前や`submit`中に届く通常のCtrl+Cを
        # 「2回目の停止signal」と報告すると、停止の昇格が起きたように読める
        forced = stop.force_requested
        _render(
            {
                "outcome": "STOPPED",
                "code": "forced_stop" if forced else "interrupted",
                "detail": (
                    "2回目の停止signalで中断した"
                    if forced
                    else "停止signalの受け取り前に中断した"
                ),
            }
        )
        return EXIT_STOPPED


def _run(argv: Sequence[str] | None, stop: StopSignal) -> int:
    args = _parser().parse_args(argv)
    paths = prepare_state_root(Path(args.state_root).resolve())
    config = read_session_config(paths, args.run)
    if isinstance(config, ConfigUnavailable):
        _render({"outcome": "STOPPED", "code": "config_unavailable", "detail": config.detail})
        return EXIT_STOPPED
    ports = default_ports(paths, config)
    if args.command == "advance":
        # signal handlerの設置はentry pointの責務（process全体のdispositionを変えるため）。
        # 受け取った後に何をするかは`step`が決める（ADR-0021 決定4）
        with install_stop_handler(stop):
            result = step(
                paths=paths,
                config=config,
                ports=ports,
                id_source=lambda: uuid.uuid4().hex,
                issued_at=_now(),
                stop=stop,
                execution=AgentExecution(active_provider=args.active_provider),
            )
        payload = _advance_payload(result.outcome)
        payload["persisted"] = list(result.trace.persisted)
        payload["halted"] = result.trace.halted
        payload["stop_requested"] = result.trace.stop_requested
        payload["stopped"] = result.trace.stopped
        _render(payload)
        return EXIT_STOPPED if payload["outcome"] == "STOPPED" else EXIT_OK
    if args.result is None:
        _render({"outcome": "STOPPED", "code": "usage", "detail": "submitは--resultを要する"})
        return EXIT_USAGE
    raw = _read_submit(Path(args.result))
    if isinstance(raw, dict):
        _render(raw)
        return EXIT_STOPPED
    payload = _submit_payload(
        submit_result(raw, paths=paths, config=config, ports=ports, accepted_at=_now())
    )
    _render(payload)
    return EXIT_STOPPED if payload["outcome"] == "STOPPED" else EXIT_OK


if __name__ == "__main__":  # pragma: no cover - process entry pointの起動行
    raise SystemExit(main())
