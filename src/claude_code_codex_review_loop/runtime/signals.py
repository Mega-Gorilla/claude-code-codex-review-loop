# SPDX-License-Identifier: Apache-2.0
"""緊急停止signalの受け取り（Phase 8 PR-3b2。ADR-0021）。

**handlerの中ではflagを立てるだけ**にする。signal handlerは任意のbytecode境界で走るため、
checkpointの書き込みやprocessの停止をそこで行うと、書きかけのfileやhandleを残し得る。
実際の停止はmain loopが安全点（`step`のengine作業の境目、`drive`のround境界）で行う。

**設置するのはentry pointだけ**である。signal dispositionはprocess全体の状態で、library
codeが勝手に変えてよいものではない。設置は文脈managerにして、退出時に必ず元へ戻す。

C-03（`process`）は「signal handlerの設置はC-08が担う」と定めており、本moduleがその実装である。
"""

from __future__ import annotations

import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import FrameType

# 受け取るsignal。Windowsは`SIGTERM`をconsoleから配送できないため`SIGBREAK`を使う
# （C-03の`job_object`が`CTRL_BREAK_EVENT`で停止を要求するのと対の関係にある）
_EXTRA_SIGNAL = "SIGBREAK" if sys.platform == "win32" else "SIGTERM"


@dataclass
class StopSignal:
    """緊急停止が要求されたかどうか（handlerが立てる唯一の状態）。

    handlerを設置しない場合も、この既定instanceは「要求されていない」を返す。呼び出し側が
    signalの有無で分岐せずに済む（`step`はいつでもこれを受け取れる）。
    """

    received: int | None = field(default=None)
    recorded: bool = field(default=False)

    @property
    def requested(self) -> bool:
        return self.received is not None

    def record(self, signum: int) -> None:
        """最初のsignalだけを保持する（2回目以降で理由が書き換わらない）。"""
        if self.received is None:
            self.received = signum

    def mark_recorded(self) -> None:
        """signalを停止要求へ変換し終えたことを記録する。

        signalは**一度きりのevent**で、flagは立ったままになる。要求をcheckpointへ書いた
        時点で持ち主は台帳へ移るので、以後は同じsignalから要求を作り直さない
        （作り直すと要求と完了を交互に繰り返す）。
        """
        self.recorded = True

    @property
    def pending(self) -> bool:
        """まだ停止要求へ変換していないsignalがあるか。"""
        return self.requested and not self.recorded


def signal_names() -> tuple[str, ...]:
    """設置対象のsignal名（診断とtestの観測点）。"""
    return ("SIGINT", _EXTRA_SIGNAL)


@contextmanager
def install_stop_handler(stop: StopSignal) -> Iterator[StopSignal]:
    """緊急停止signalのhandlerを設置し、退出時に元へ戻す。

    設置できないsignal（platformに無い、main thread以外）は**黙って飛ばす**。signalを
    受け取れないことは停止機構の不在ではなく、その場合も台帳経由の停止経路は動く。
    """
    previous: list[tuple[int, object]] = []

    def _handler(signum: int, frame: FrameType | None) -> None:
        stop.record(signum)

    for name in signal_names():
        signum = getattr(signal, name, None)
        if signum is None:  # pragma: no cover - 対象OSでは両方存在する
            continue
        try:
            previous.append((int(signum), signal.signal(signum, _handler)))
        except (OSError, ValueError):  # pragma: no cover - main thread以外での設置
            continue
    try:
        yield stop
    finally:
        for signum, handler in previous:
            signal.signal(signum, handler)  # type: ignore[arg-type]
