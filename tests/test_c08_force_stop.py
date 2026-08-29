# SPDX-License-Identifier: Apache-2.0
"""AC-C03-02のE2E: 2回目のCtrl+Cでgrace待機を打ち切り即時forceへ昇格する。

ADR-0005は停止primitiveだけをC-03に置き、「1回目 = graceful -> grace -> force、
2回目 = force即時」の**wiringはC-08の責務**と定めた（決定6 / Consequences）。PR-3b2が
signal handlerを設置したことで、この昇格が実際に成立するかを確かめる責任が生じる。

ここでは**実際に終了しないprocess treeへ実signalを2回送る**。2回目が届く窓は2つあり、
どちらもtreeを残さないことを固定する。

| 2回目が届く位置 | test |
| --- | --- |
| grace待機中（`stop_trees`の内側） | `test_a_second_signal_forces_a_tree_that_ignores_graceful_stop` |
| 最初の安全点より前（要求すら未保存） | `test_a_second_signal_before_the_first_safe_point_still_stops_the_tree` |

unit testは`test_c08_signals.py`が持つ（`TestForceEscalation` / `TestForceOutsideTheStop`）が、
grace待機が本当に中断されるかは、C-03の停止primitiveへ実signalが届く経路でしか確かめられない。

**製品codeはprocessを起動しない**（PR-3b2の範囲）。treeを起動するのはこのtestで、C-03の
公開API（`spawn_tree`）を使う。Controllerはtest driverのsubprocessである。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from c03_support.helpers import child_argv, child_env, read_pids, tree_gone, write_child_script
from c07_support.helpers import RUN
from c08_support.helpers import machine_state
from c08_support.runtime import runtime_env

from claude_code_codex_review_loop.process import SpawnSpec, spawn_tree
from claude_code_codex_review_loop.state import CheckpointLoaded, checkpoint_path, load_checkpoint
from claude_code_codex_review_loop.workflow import (
    read_active_trees,
    read_stop_request,
    with_active_trees,
)

DRIVER = Path(__file__).resolve().parent / "c08_support" / "driver.py"

# grace periodは「昇格しなければ絶対に終わらない」長さにする。この値を待ってしまう実装は
# testが打ち切るより先に失敗する
GRACE_MS = 120_000
# 2回目を送るまでの待ち。driverがgrace待機へ入るのに十分で、testの実行時間には響かない
BEFORE_FORCE_SECONDS = 2.0
# 昇格後の全滅を待つ上限。graceより桁違いに短い
FORCE_LIMIT_SECONDS = 30.0


def _force_signal() -> int:
    return signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT


def _popen(state_root: Path, *argv: str) -> subprocess.Popen[str]:
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        # WindowsはSIGINTをsubprocessへ送れない（C-03のjob_objectと同じ手段を使う）
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(  # noqa: S603 - 起動するのは自分たちのtest driver
        [sys.executable, str(DRIVER), str(state_root), *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        **kwargs,
    )


@pytest.mark.skipif(
    os.environ.get("CC_REVIEW_SKIP_PROCESS_TESTS") == "1",
    reason="実processを起動するtestを外した環境",
)
def test_a_second_signal_forces_a_tree_that_ignores_graceful_stop(tmp_path: Path) -> None:
    """graceful要求を無視するtreeを、2回目のsignalでgrace満了を待たずに全滅させる。"""
    script = write_child_script(tmp_path)
    pidfile = tmp_path / "pids.txt"
    spec = SpawnSpec(
        # `ignore`はgraceful要求を無視する子。孫は使わない（zombieのreapを単純に保つ）
        argv=child_argv(script, "ignore", pidfile, grandchild=False),
        cwd=tmp_path,
        env=child_env(),
    )
    handle = spawn_tree(spec)
    try:
        child_pid, _ = read_pids(pidfile)
        env = runtime_env(
            tmp_path,
            state=machine_state(),
            extra=with_active_trees({}, [handle.ref]),
            config_overrides={"halt_grace_ms": GRACE_MS},
        )
        assert read_active_trees(_payload(env)) == (handle.ref,)

        driver = _popen(env.paths.root, "wait-for-signal-real-stop")
        started = time.monotonic()
        try:
            assert driver.stdout is not None
            assert driver.stdout.readline().strip() == "READY"

            driver.send_signal(_force_signal())  # 1回目: graceful -> grace待機へ入る
            time.sleep(BEFORE_FORCE_SECONDS)
            assert driver.poll() is None, "graceを待たずに終了している"
            assert not tree_gone(child_pid), "graceful要求を無視するtreeが消えている"

            driver.send_signal(_force_signal())  # 2回目: 即時forceへ昇格する
            assert _wait_for_exit(driver, handle), "昇格後もdriverが終了しない"
            elapsed = time.monotonic() - started
        finally:
            if driver.poll() is None:  # pragma: no cover - 失敗時の後始末
                driver.kill()
            stdout, stderr = driver.communicate(timeout=FORCE_LIMIT_SECONDS)

        assert driver.returncode == 0, stderr
        assert "Traceback" not in stderr, stderr
        # grace（120秒）を待っていない
        assert elapsed < FORCE_LIMIT_SECONDS, elapsed
        assert tree_gone(child_pid), "treeが残っている"
        payload = json.loads(stdout.splitlines()[-1])
        assert payload["outcome"] in {"TERMINAL", "STOPPED"}, payload
        # 停止意図は消費されている（停止が完了した場合）
        if payload["outcome"] == "TERMINAL":
            assert read_stop_request(_payload(env)) is None
            assert read_active_trees(_payload(env)) == ()
    finally:
        handle.close()


@pytest.mark.skipif(
    os.environ.get("CC_REVIEW_SKIP_PROCESS_TESTS") == "1",
    reason="実processを起動するtestを外した環境",
)
def test_a_second_signal_before_the_first_safe_point_still_stops_the_tree(
    tmp_path: Path,
) -> None:
    """2回目が**最初の安全点より前**に届いてもtreeを残さない（AC-C03-01 / 02）。

    driverは1回目を観測した直後・停止要求を保存する**前**で待つ（`ARMED`）。そこへ2回目を
    送るので、`KeyboardInterrupt`は`stop_trees`の外側——要求すらまだdurableでない窓——へ落ちる。
    """
    script = write_child_script(tmp_path)
    pidfile = tmp_path / "pids.txt"
    spec = SpawnSpec(
        argv=child_argv(script, "ignore", pidfile, grandchild=False),
        cwd=tmp_path,
        env=child_env(),
    )
    handle = spawn_tree(spec)
    try:
        child_pid, _ = read_pids(pidfile)
        env = runtime_env(
            tmp_path,
            state=machine_state(),
            extra=with_active_trees({}, [handle.ref]),
            config_overrides={"halt_grace_ms": GRACE_MS},
        )
        driver = _popen(env.paths.root, "wait-for-signal-early-force", "10")
        try:
            assert driver.stdout is not None
            assert driver.stdout.readline().strip() == "READY"
            driver.send_signal(_force_signal())  # 1回目
            assert driver.stdout.readline().strip() == "ARMED"  # 安全点の手前で待っている
            driver.send_signal(_force_signal())  # 2回目（要求の保存より前）
            assert _wait_for_exit(driver, handle), "driverが終了しない"
        finally:
            if driver.poll() is None:  # pragma: no cover - 失敗時の後始末
                driver.kill()
            stdout, stderr = driver.communicate(timeout=FORCE_LIMIT_SECONDS)

        assert driver.returncode == 0, stderr
        assert "Traceback" not in stderr, stderr
        assert tree_gone(child_pid), "treeが残っている"
        payload = json.loads(stdout.splitlines()[-1])
        assert payload["outcome"] == "STOPPED"
        assert payload["code"] == "forced_stop", payload
        # 停止まで完了しているので要求は消費されている（台帳にも残らない）
        assert read_stop_request(_payload(env)) is None
        assert read_active_trees(_payload(env)) == ()
    finally:
        handle.close()


def _wait_for_exit(driver: subprocess.Popen[str], handle: object) -> bool:
    """driverの終了を待つ間、treeのzombieを回収し続ける。

    POSIXではtreeの親は**このtest process**なので、reapしないとdriver側の
    `kill(-pgid, 0)`が生存として観測し続け、強制停止の確認が終わらない。
    """
    deadline = time.monotonic() + FORCE_LIMIT_SECONDS
    while time.monotonic() < deadline:
        handle.poll()  # type: ignore[attr-defined]
        if driver.poll() is not None:
            return True
        time.sleep(0.05)
    return False


def _payload(env: object) -> dict[str, object]:
    loaded = load_checkpoint(checkpoint_path(env.paths, RUN))  # type: ignore[attr-defined]
    assert isinstance(loaded, CheckpointLoaded), loaded
    return loaded.payload
