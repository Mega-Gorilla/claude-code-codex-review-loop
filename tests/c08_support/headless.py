# SPDX-License-Identifier: Apache-2.0
"""fake headless hostのtest支援（Phase 8 PR-3b3）。

境界を**実行file**にする（fake ghと同じ置き方）。こうするとspawn・待機・stdout回収・
`processes`台帳・redactionはすべて製品codeがそのまま走り、fakeなのは「何を返すか」だけになる。

子は次の契約で動く（`HeadlessHost`が定めた形）。

1. argv末尾のenvelope pathを読む
2. plan fileの次の1件を取り出す（消費順は自分のstate fileで進める）
3. 結果payloadをenvelopeの`result_path`へ書く
4. **submit envelopeをstdoutへ出す**
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from claude_code_codex_review_loop.domain.values import RecordKind

from .helpers import user_record_payload, user_submit_payload

_FAKE_HOST_SCRIPT = '''\
"""fake headless host（testが生成する。実Claudeは起動しない）。"""
import hashlib
import json
import os
import sys

envelope_path = sys.argv[1]
plan_path = os.environ["CC_REVIEW_FAKE_HOST_PLAN"]
state_path = os.environ["CC_REVIEW_FAKE_HOST_STATE"]
note = os.environ.get("CC_REVIEW_FAKE_HOST_STDERR", "")
if note:
    sys.stderr.write(note + "\\n")

# 自分が生きている間のcheckpointを写す。親は`wait`で止まっているので、ここに自分のtreeが
# 見えるなら「登録は待機より先」が成り立っている
ledger_out = os.environ.get("CC_REVIEW_FAKE_HOST_LEDGER_OUT", "")
if ledger_out:
    with open(os.environ["CC_REVIEW_FAKE_HOST_CHECKPOINT"], encoding="utf-8") as handle:
        seen = json.load(handle).get("processes")
    with open(ledger_out, "w", encoding="utf-8") as handle:
        json.dump(seen, handle)

with open(envelope_path, encoding="utf-8") as handle:
    envelope = json.load(handle)
with open(plan_path, encoding="utf-8") as handle:
    plan = json.load(handle)
try:
    with open(state_path, encoding="utf-8") as handle:
        consumed = json.load(handle)["consumed"]
except (OSError, ValueError):
    consumed = 0

entry = plan[consumed]
with open(state_path, "w", encoding="utf-8") as handle:
    json.dump({"consumed": consumed + 1}, handle)

result_path = os.path.join(os.path.dirname(envelope_path), os.path.basename(envelope["result_path"]))
text = json.dumps(entry["result"], ensure_ascii=False)
with open(result_path, "w", encoding="utf-8") as handle:
    handle.write(text)
with open(result_path, "rb") as handle:
    digest = hashlib.sha256(handle.read()).hexdigest()

submit = dict(entry["submit"])
submit["result_hash"] = digest
if "request_id" in envelope:
    submit["request_id"] = envelope["request_id"]
    submit["awaiting"] = envelope["awaiting"]
else:
    submit["action_id"] = envelope["action_id"]
    submit["action_kind"] = envelope["action_kind"]
submit["nonce"] = envelope["nonce"]
submit["expected_head_sha"] = envelope["expected_head_sha"]
submit["run_id"] = envelope["run_id"]
sys.stdout.write(json.dumps(submit, ensure_ascii=False))

sys.exit(int(os.environ.get("CC_REVIEW_FAKE_HOST_EXIT", "0")))
'''

_HANG_SCRIPT = '''\
"""終了しないfake host（timeout経路の観測用）。"""
import signal
import sys
import time

stop_signal = signal.SIGBREAK if hasattr(signal, "SIGBREAK") else signal.SIGTERM
signal.signal(stop_signal, signal.SIG_IGN)
sys.stderr.write("hanging\\n")
sys.stderr.flush()
for _ in range(6000):
    time.sleep(0.05)
'''


def write_fake_host(directory: Path, *, hang: bool = False) -> Path:
    """fake headless host scriptを生成してpathを返す。"""
    script = directory / ("fake_host_hang.py" if hang else "fake_host.py")
    script.write_text(_HANG_SCRIPT if hang else _FAKE_HOST_SCRIPT, encoding="utf-8")
    return script


def user_entry(kind: RecordKind, *, awaiting_value: str = "USER_INPUT_GATE") -> dict[str, object]:
    """ユーザー入力待ちへの応答1件（`AWAIT_USER`のresult）。"""
    return {
        "result": user_record_payload(kind),
        "submit": user_submit_payload(
            request_id="filled-by-child",
            nonce="filled-by-child",
            result_hash="filled-by-child",
            result_kind=kind.value,
        )
        | {"awaiting": awaiting_value},
    }


def action_entry(kind: RecordKind, payload: Mapping[str, object]) -> dict[str, object]:
    """`HOST_ACTION`への応答1件。"""
    from .helpers import submit_payload

    return {
        "result": dict(payload),
        "submit": submit_payload(
            action_id="filled-by-child",
            nonce="filled-by-child",
            result_hash="filled-by-child",
            result_kind=kind.value,
        ),
    }


def write_plan(directory: Path, entries: Sequence[Mapping[str, object]]) -> Path:
    """fake hostが順に消費する応答planを書く。"""
    path = directory / "fake-host-plan.json"
    path.write_text(json.dumps(list(entries), ensure_ascii=False), encoding="utf-8")
    return path


def host_env(
    plan: Path,
    state: Path,
    *,
    stderr_note: str = "",
    exit_code: int = 0,
    ledger_out: Path | None = None,
    checkpoint: Path | None = None,
) -> dict[str, str]:
    """子へ渡す環境変数（`SpawnSpec`は継承しないので必要なものを明示する）。

    `ledger_out`を渡すと、子は**自分が生きている間**のcheckpointの`processes` sectionを
    そこへ写す。親は`wait`で止まっているので、自分のtreeが見えれば「登録は待機より先」である。
    """
    from c03_support.helpers import child_env

    env = dict(child_env())
    env["CC_REVIEW_FAKE_HOST_PLAN"] = str(plan)
    env["CC_REVIEW_FAKE_HOST_STATE"] = str(state)
    env["CC_REVIEW_FAKE_HOST_STDERR"] = stderr_note
    env["CC_REVIEW_FAKE_HOST_EXIT"] = str(exit_code)
    if ledger_out is not None and checkpoint is not None:
        env["CC_REVIEW_FAKE_HOST_LEDGER_OUT"] = str(ledger_out)
        env["CC_REVIEW_FAKE_HOST_CHECKPOINT"] = str(checkpoint)
    return env


__all__ = [
    "action_entry",
    "host_env",
    "user_entry",
    "write_fake_host",
    "write_plan",
]
