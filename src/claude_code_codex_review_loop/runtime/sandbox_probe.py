# SPDX-License-Identifier: Apache-2.0
"""C-09 sandbox preflightのprobe。`codex sandbox`の中で実行され、境界を実測して報告する。

標準libraryだけに依存し、package本体をimportしない（sandbox内で本packageの所在が
読めるとは限らない）。結果は`label=allowed|denied`の行で報告し、終了codeは常に0とする。
終了codeは「sandboxが起動できたか」だけを表し、境界の成否は行で表す。

書込の成否とcleanupの成否は分離する。作成に一度でも成功した書込は、削除に失敗しても
必ず`allowed`と報告し、cleanupの失敗は`cleanup=failed`として別に報告する。cleanup失敗を
`denied`へ畳み込むと、protected rootを変更できたsandboxを安全と誤認する。接続も同様に、
接続が成立した時点で`allowed`とし、close失敗で反転させない。

引数: `<workspace> <credential_file> <network_host> <network_port> <sentinel> [<protected_root>...]`
sentinelはfacadeが1測定ごとに払い出す一意なfile名で、facadeがhost側で残留を確認する。
"""

from __future__ import annotations

import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

PROBE_LABELS: Final = ("workspace_write", "protected_write", "credential_read", "network")
CLEANUP_LABEL: Final = "cleanup"
_NETWORK_TIMEOUT_SECONDS: Final = 5.0


def default_probe_command() -> tuple[str, ...]:
    """現在のinterpreterで本fileを実行するargv prefix。"""
    return (sys.executable, str(Path(__file__).resolve()))


def _attempt(action: Callable[[], object]) -> str:
    try:
        action()
    except Exception:  # sandboxの拒否はOS依存の例外で現れるため種類を限定しない
        return "denied"
    return "allowed"


def _write_outcome(root: Path, sentinel: str, cleanup_failures: list[str]) -> str:
    """作成できたら`allowed`。削除の失敗は結果を変えず、cleanup失敗として記録する。"""
    target = root / sentinel
    outcome = _attempt(lambda: target.write_text("probe", encoding="utf-8"))
    if outcome == "allowed" and _attempt(target.unlink) == "denied":
        cleanup_failures.append(str(target))
    return outcome


def _connect(host: str, port: int) -> None:
    connection = socket.create_connection((host, port), timeout=_NETWORK_TIMEOUT_SECONDS)
    # 接続が成立した時点で境界は開いている。close失敗で`denied`へ反転させない。
    _attempt(connection.close)


def _read(path: Path) -> None:
    path.read_bytes()


def run_probe(
    workspace: Path,
    credential_file: Path,
    host: str,
    port: int,
    sentinel: str,
    protected: tuple[Path, ...],
) -> dict[str, str]:
    """境界ごとの実測結果とcleanupの成否。protected_writeは全rootで拒否された場合だけdeniedになる。"""
    cleanup_failures: list[str] = []
    protected_outcomes = [_write_outcome(root, sentinel, cleanup_failures) for root in protected]
    outcomes = {
        "workspace_write": _write_outcome(workspace, sentinel, cleanup_failures),
        "protected_write": "denied" if protected and all(o == "denied" for o in protected_outcomes) else "allowed",
        "credential_read": _attempt(lambda: _read(credential_file)),
        "network": _attempt(lambda: _connect(host, port)),
    }
    outcomes[CLEANUP_LABEL] = "failed" if cleanup_failures else "ok"
    return outcomes


def main(argv: list[str]) -> int:
    if len(argv) < 5:
        sys.stdout.write("usage=error\n")
        return 0
    workspace, credential_file, host, port, sentinel, *protected = argv
    outcomes = run_probe(
        Path(workspace), Path(credential_file), host, int(port), sentinel, tuple(Path(p) for p in protected)
    )
    for label in (*PROBE_LABELS, CLEANUP_LABEL):
        sys.stdout.write(f"{label}={outcomes[label]}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - subprocessとして起動した場合はtestが子processで計測する
    sys.exit(main(sys.argv[1:]))
