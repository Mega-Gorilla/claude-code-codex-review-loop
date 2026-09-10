# SPDX-License-Identifier: Apache-2.0
"""C-09 sandbox preflightのprobe。`codex sandbox`の中で実行され、境界を実測して報告する。

標準libraryだけに依存し、package本体をimportしない（sandbox内で本packageの所在が
読めるとは限らない）。結果は`label=allowed|denied`の行で報告し、終了codeは常に0とする。
終了codeは「sandboxが起動できたか」だけを表し、境界の成否は行で表す。

引数: `<workspace> <credential_file> <network_host> <network_port> [<protected_root>...]`
"""

from __future__ import annotations

import socket
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Final

PROBE_LABELS: Final = ("workspace_write", "protected_write", "credential_read", "network")
_NETWORK_TIMEOUT_SECONDS: Final = 5.0


def default_probe_command() -> tuple[str, ...]:
    """現在のinterpreterで本fileを実行するargv prefix。"""
    return (sys.executable, str(Path(__file__).resolve()))


def _attempt(action: Callable[[], None]) -> str:
    try:
        action()
    except Exception:  # sandboxの拒否はOS依存の例外で現れるため種類を限定しない
        return "denied"
    return "allowed"


def _write_then_remove(root: Path) -> None:
    target = root / f".cc-review-probe-{uuid.uuid4().hex}"
    target.write_text("probe", encoding="utf-8")
    # 書けた場合は必ず消す。隔離checkoutをdirtyにせず、protected rootへ痕跡を残さない。
    target.unlink()


def _connect(host: str, port: int) -> None:
    connection = socket.create_connection((host, port), timeout=_NETWORK_TIMEOUT_SECONDS)
    connection.close()


def _read(path: Path) -> None:
    path.read_bytes()


def _write_outcome(root: Path) -> str:
    return _attempt(lambda: _write_then_remove(root))


def run_probe(
    workspace: Path, credential_file: Path, host: str, port: int, protected: tuple[Path, ...]
) -> dict[str, str]:
    """境界ごとの実測結果。protected_writeは全rootで拒否された場合だけdeniedになる。"""
    protected_outcomes = [_write_outcome(root) for root in protected]
    return {
        "workspace_write": _write_outcome(workspace),
        "protected_write": "denied" if protected and all(o == "denied" for o in protected_outcomes) else "allowed",
        "credential_read": _attempt(lambda: _read(credential_file)),
        "network": _attempt(lambda: _connect(host, port)),
    }


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        sys.stdout.write("usage=error\n")
        return 0
    workspace, credential_file, host, port, *protected = argv
    outcomes = run_probe(Path(workspace), Path(credential_file), host, int(port), tuple(Path(p) for p in protected))
    for label in PROBE_LABELS:
        sys.stdout.write(f"{label}={outcomes[label]}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - subprocessとして起動した場合はtestが子processで計測する
    sys.exit(main(sys.argv[1:]))
