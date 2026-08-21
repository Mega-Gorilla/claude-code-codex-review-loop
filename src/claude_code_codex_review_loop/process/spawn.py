# SPDX-License-Identifier: Apache-2.0
"""C-03の型・契約の正本とspawn facade。

本moduleがC-03の公開契約（SpawnSpec / TreeHandle / TreeRef / 構造化error /
停止outcome）を定義し、OS別backend（job_object / process_group）はここから型を
importして実装する。OS分岐は本module末尾のconditional import 1箇所に閉じる。

契約上の要点:

- argvはlist形式のみを受け、shellを経由しない（P-014）
- envは指定されたmappingだけを子へ渡し、親環境を継承しない（C-06のcredential隔離の
  前提）。Windowsで子がPython等の場合に必要な`SYSTEMROOT`等の基本変数も、呼び出し側が
  明示的に含める必要がある
- stdout / stderrはfileへredirectし、pipeを作らない（deadlockとthread生成の回避、
  別pane等からのlog観測のため）。stdinは常にDEVNULL（非対話。TUIへのキー入力注入は
  行わない）
- timeoutとgrace periodの既定値は持たない（既定値の解決はPhase 12のC-12設定解決）
- 終了・停止の理由は型で区別し、出力文字列の部分一致で分類しない（P-003）
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path
from typing import IO, Protocol


class ProcessError(Exception):
    """C-03の構造化errorの基底。"""


class SpawnError(ProcessError):
    """spawn手順の失敗。stageは失敗した段階の識別子、os_errorはOSのerror code。"""

    def __init__(self, stage: str, detail: str, os_error: int | None = None) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail
        self.os_error = os_error


class StopError(ProcessError):
    """停止手順の失敗。C-01のcancellation契約に従い、停止commandの冪等再発行の対象になる。"""

    def __init__(self, stage: str, detail: str, os_error: int | None = None) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail
        self.os_error = os_error


@dataclass(frozen=True)
class JobObjectRef:
    """Windows treeの再停止identifier。job_nameはnamed Job Objectの名前。

    元のhandleを持たない別processは、job_nameから`OpenJobObjectW`でtreeへ到達する。
    Job ObjectはKILL_ON_JOB_CLOSEを持つため、起動元processが消滅するとtreeは自動で
    全滅し、name不在は「停止済み」を意味する。
    """

    pid: int
    job_name: str


@dataclass(frozen=True)
class ProcessGroupRef:
    """POSIX treeの再停止identifier。

    既知limitation: process groupが完全に消滅した後にpgidが別の新groupへ再利用されると、
    本refでは区別できない（stdlibのみでは完全排除が不可能）。leader pidの再利用は
    `os.getpgid`の照合で緩和する。C-07のresume設計で再訪する。
    """

    pid: int
    pgid: int


TreeRef = JobObjectRef | ProcessGroupRef


@unique
class StopMethod(Enum):
    """tree停止の到達方法（P-003: 型で区別する）。"""

    ALREADY_EXITED = "ALREADY_EXITED"
    GRACEFUL = "GRACEFUL"
    FORCED = "FORCED"


@dataclass(frozen=True)
class StopResult:
    """tree停止の結果。

    - method: GRACEFULは「grace期間内にtreeが消滅した」ことを意味する。並行する
      force要求（2回目のCtrl+C等）との競合の最終確定はC-08が行う
    - graceful_requested: graceful停止の要求がOSに受理されたか。配送保証ではない
      （WindowsのCTRL_BREAK_EVENTは成功してもqueueされただけで、応答はtree生存の
      観測でのみ確認できる）
    """

    method: StopMethod
    graceful_requested: bool


@dataclass(frozen=True)
class Completed:
    """run_treeの正常終了。exit_codeは直接childの終了code。"""

    exit_code: int


@dataclass(frozen=True)
class TimedOut:
    """run_treeのtimeout。treeはstop_resultの方法で停止済み。"""

    stop_result: StopResult


@dataclass(frozen=True)
class SpawnSpec:
    """子process treeの起動仕様。

    - argv: 実行fileと引数のtuple（list形式のみ。P-014）
    - cwd: 子の作業directory
    - env: 子へ渡す環境変数の全体（継承しない）
    - stdout_path / stderr_path: Noneの場合はDEVNULL。fileは0o600相当で作成する
    """

    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    stdout_path: Path | None = None
    stderr_path: Path | None = None

    def __post_init__(self) -> None:
        if not self.argv:
            raise SpawnError("validate", "argvが空である")
        for index, argument in enumerate(self.argv):
            if not isinstance(argument, str) or not argument:
                raise SpawnError("validate", f"argv[{index}]が空、または文字列でない")
        if self.stdout_path is not None and self.stdout_path == self.stderr_path:
            raise SpawnError("validate", "stdout_pathとstderr_pathへ同一pathは指定できない")


class TreeHandle(Protocol):
    """1つの子process treeへの操作interface。OS別backendが実装する。"""

    @property
    def pid(self) -> int:
        """直接childのprocess ID。"""

    @property
    def ref(self) -> TreeRef:
        """別processからの再停止に使えるidentifier。"""

    def poll(self) -> int | None:
        """直接childの終了codeを返す（未終了ならNone）。POSIXではzombieのreapを兼ねる。"""

    def wait(self, timeout_seconds: float) -> int | None:
        """直接childの終了を待つ。timeoutしたらNone（例外にしない）。"""

    def alive_in_tree(self) -> bool:
        """tree内に生存processが1つでもあるか。"""

    def request_graceful_stop(self) -> bool:
        """graceful停止をtree全体へ要求する。戻り値は要求が受理されたか（配送保証ではない）。"""

    def force_stop(self) -> None:
        """tree全体を強制停止する。停止済みのtreeに対しても安全（冪等）。"""

    def close(self) -> None:
        """安全網の強制停止とOS resource（handle / file）の解放。冪等。"""


def _open_output(path: Path | None) -> IO[bytes] | None:
    """redirect先fileを作成者のみ読書き可能な権限で開く。Noneの場合はDEVNULLを意味する。"""
    if path is None:
        return None

    def _opener(target: str, flags: int) -> int:
        return os.open(target, flags, 0o600)

    return open(path, "wb", opener=_opener)


if sys.platform == "win32":  # pragma: no cover - OS dispatch(単一分岐点。各backendは自OSのCIで検証する)
    from . import job_object

    _backend = job_object
else:  # pragma: no cover - OS dispatch(単一分岐点。各backendは自OSのCIで検証する)
    from . import process_group

    _backend = process_group


def spawn_tree(spec: SpawnSpec) -> TreeHandle:
    """specに従い子process treeを起動する。"""
    return _backend.spawn_tree(spec)
