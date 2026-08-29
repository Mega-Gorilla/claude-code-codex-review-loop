# SPDX-License-Identifier: Apache-2.0
"""headless経路のhost adapter（Phase 8 PR-3b3。ADR-0022）。

対話型sessionが存在しない復旧経路のClaude coderを、Controllerがsubprocessとして起動する
（implementation plan Section 4の役割表）。engineから見たinterfaceは主経路と**同一**で、
`HostPort`を実装するだけである。この同一性がAC-C08-04（active / headlessの同値性）を
実装の一致ではなく構造で担保する。

```
spawn_tree -> 台帳へ登録 -> wait -> 停止 / close -> 台帳から除去 -> stdoutをsubmitとして返す
```

- **起動commandは呼び出し側が渡す**。既定値を持たない（解決はC-12）。`identity.auto_mode`の
  `probe_auto_mode`と同じ形で、argvの先頭は絶対path、`ensure_argv_allowed`を通す（P-006）
- **台帳への登録は待機より先**（ADR-0019 決定10）。待っている間に落ちたtreeを、次のprocessが
  台帳から止められる
- **例外を呼び出し側へ飛ばさない**。起動失敗・timeout・出力不正はすべて構造化結果へ写す
  （`HostPort.execute`はbytesを返す契約なので、失敗は`HeadlessError`として明示する）
- stdoutは**submit envelope**、stderrは**log**。logは`redact`を通してから`host.log`へ書く
  （`policy.redaction`が「C-08のlog」を消費者として名指ししている実装地点）
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..identity.errors import IdentityError
from ..identity.fs_permissions import replace_private_text
from ..policy.permission_profile import ensure_argv_allowed
from ..policy.redaction import redact
from ..process import Completed, ProcessError, SpawnSpec, TreeHandle, spawn_tree, stop_tree
from ..state import CheckpointLoaded, StatePaths, checkpoint_path, load_checkpoint, save_checkpoint
from ..workflow import SectionUnavailable, with_tree_added, with_tree_removed
from .session import HostWork

# 子の出力file名。`host.log`だけがredact済みで、CIのartifactが集めるのはこれだけである
STDOUT_FILE: Final = "host.stdout"
STDERR_FILE: Final = "host.stderr"
LOG_FILE: Final = "host.log"

# submit envelopeの読込上限（entry pointの`MAX_SUBMIT_BYTES`と同じ理由・同じ値）
MAX_SUBMIT_BYTES: Final = 64 * 1024


LedgerChange = Callable[[Mapping[str, object]], "dict[str, object] | SectionUnavailable"]


class HeadlessError(Exception):
    """headless hostが結果を返せなかった。detailは構造化された理由。"""


@dataclass(frozen=True)
class HeadlessHost:
    """headless Claude coderをsubprocessとして1作業ずつ実行する。

    既定値は持たない（起動command・作業directory・env・timeoutはすべて必須）。envは
    **継承しない**（`SpawnSpec`の契約）ので、必要なものは呼び出し側が明示する。
    """

    paths: StatePaths
    run_id: str
    command: tuple[str, ...]
    workdir: Path
    env: Mapping[str, str]
    timeout_seconds: float
    grace_seconds: float

    def __post_init__(self) -> None:
        if not self.command or not os.path.isabs(self.command[0]):
            raise HeadlessError("host commandの先頭は絶対pathでなければならない")

    def execute(self, work: HostWork) -> bytes:
        """1つのhost作業を子processで実行し、submit envelopeを返す。"""
        directory = work.envelope_path.parent
        argv = (*self.command, str(work.envelope_path))
        ensure_argv_allowed(argv)  # P-006のruntime choke point
        spec = SpawnSpec(
            argv=argv,
            cwd=self.workdir,
            env=self.env,
            stdout_path=directory / STDOUT_FILE,
            stderr_path=directory / STDERR_FILE,
        )
        try:
            handle = spawn_tree(spec)
        except ProcessError as error:
            raise HeadlessError(f"headless hostを起動できない: {error}") from error
        try:
            completed = self._run(handle)
        finally:
            # `run_tree`と同じ安全網。孫が残っていてもここで落とす
            handle.close()
            self._forget(handle)
            self._write_log(directory)
        if completed is None:
            raise HeadlessError(f"headless hostがtimeoutした（{self.timeout_seconds}秒）")
        if completed.exit_code != 0:
            raise HeadlessError(f"headless hostが異常終了した（exit={completed.exit_code}）")
        # submit envelopeを先に確かめる。何も出力しなかった子へ「result fileが無い」と
        # 報告しても、実際に起きたこと（結果を返していない）が伝わらない
        raw = _read_submit(directory / STDOUT_FILE)
        _harden_result(work.result_path)
        return raw

    def _run(self, handle: TreeHandle) -> Completed | None:
        """台帳へ登録してから待つ（登録が待機より先。ADR-0019 決定10）。timeoutはNone。"""
        self._remember(handle)
        exit_code = handle.wait(self.timeout_seconds)
        if exit_code is None:
            _stop_timed_out(handle, self.grace_seconds)
            return None
        return Completed(exit_code=exit_code)

    def _remember(self, handle: TreeHandle) -> None:
        self._update(lambda payload: with_tree_added(payload, handle.ref))

    def _forget(self, handle: TreeHandle) -> None:
        """**停止を確認してから**台帳から外す（`close`の後に呼ぶ）。"""
        self._update(lambda payload: with_tree_removed(payload, handle.ref))

    def _update(self, change: LedgerChange) -> None:
        """台帳をread-modify-writeする（他componentのtreeを消さない）。"""
        path = checkpoint_path(self.paths, self.run_id)
        loaded = load_checkpoint(path)
        if not isinstance(loaded, CheckpointLoaded):
            raise HeadlessError(f"台帳を更新できない: checkpointを読めない（{type(loaded).__name__}）")
        updated = change(loaded.payload)
        if isinstance(updated, SectionUnavailable):
            raise HeadlessError(f"台帳を更新できない: {updated.detail}")
        save_checkpoint(path, updated)

    def _write_log(self, directory: Path) -> None:
        """stderrをredactして`host.log`へ書く（rawは残すが収集対象にしない）。

        **作成者限定で書く**。`host.log`はCIのartifactが集めるfileで、private repositoryの
        内容が写り込み得る（AC-C06-05 / P-009）。C-03はredirect先を0o600で開くので、raw側は
        既に作成者限定である。
        """
        raw = directory / STDERR_FILE
        try:
            text = raw.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - 直前にredirect先として作成したfile
            return
        replace_private_text(directory / LOG_FILE, redact(text).text)


def _stop_timed_out(handle: TreeHandle, grace_seconds: float) -> None:
    """timeoutしたtreeを止める（止められなくても`close`の安全網が残る）。"""
    try:
        stop_tree(handle, grace_seconds)
    except ProcessError:  # pragma: no cover - closeが強制停止を引き受ける
        pass


def _harden_result(path: Path) -> None:
    """子が書いたresult fileを**作成者限定へ揃える**。

    result fileにはprivate repositoryのdiffとreview内容が入り得るため、engineは作成者限定で
    あることを検証してから読む（`read_result` -> `verify_private_file`。AC-C06-05）。子は
    外部programで自分のumaskで書く（POSIXの既定は`0o644`）ので、**境界であるadapterが揃える**。

    内容はbytesとして読み直してから書き戻す。`result_hash`は子が同じbytesで計算しており、
    text経由の改行変換で1 byteでも変わるとhash照合が落ちる。
    """
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise HeadlessError(f"result fileを読めない: {error}") from error
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HeadlessError(f"result fileがUTF-8でない: {error}") from error
    try:
        replace_private_text(path, text)
    except IdentityError as error:
        raise HeadlessError(f"result fileを作成者限定にできない: {error}") from error


def _read_submit(path: Path) -> bytes:
    """stdoutをsubmit envelopeとして読む（読む前にsizeを検査する）。"""
    try:
        size = path.stat().st_size
    except OSError as error:  # pragma: no cover - 直前にredirect先として作成したfile
        raise HeadlessError(f"headless hostの出力を読めない: {error}") from error
    if size == 0:
        raise HeadlessError("headless hostがsubmit envelopeを出力していない")
    if size > MAX_SUBMIT_BYTES:
        raise HeadlessError(f"submit envelopeが上限{MAX_SUBMIT_BYTES}byteを超える: {size}")
    return path.read_bytes()
