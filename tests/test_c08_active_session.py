# SPDX-License-Identifier: Apache-2.0
"""active hostの扱いの受入test（Phase 8 PR-3b1。ADR-0020）。

- **AC-C08-01**: 同一のClaude Code sessionが複数roundを担当する。observation pointは
  「hostのinstanceが1つのまま」で、roundごとに作り直されないことである
- **AC-C08-02**: Controllerは主経路のClaude coderを**subprocess化せず、キー入力も注入しない**。
  fakeのcounterだけでは「今回のtestで起きなかった」しか言えないため、`runtime` packageが
  process起動もkey注入も**構造的に持たない**ことをAST contractで固定する
"""

from __future__ import annotations

import ast
from pathlib import Path

from c08_support.helpers import user_machine_state
from c08_support.runtime import RuntimeEnv, fixed_clock, gate_host, round_ports, runtime_env

from claude_code_codex_review_loop import runtime as runtime_package
from claude_code_codex_review_loop.domain.values import Awaiting, RecordKind, State
from claude_code_codex_review_loop.runtime import drive
from claude_code_codex_review_loop.workflow import Terminal

# processを起動する / TUIへ入力を送る手段。**主経路のmoduleはどちらも持たない**
SPAWN_MODULES = frozenset({"subprocess", "multiprocessing", "os", "pty", "winpty"})
KEY_INJECTION_MODULES = frozenset({"pyautogui", "pywinauto", "keyboard", "pynput", "msvcrt"})
# headless経路のadapter。Controllerが起動する側なので、spawnの検査から明示的に外す
# （implementation plan Section 4: 「Claude coder（headless経路）はControllerが
# subprocessとして起動するadapter」。ADR-0022）
HEADLESS_MODULE = "host_headless.py"
# reviewer用の独立processでのみcheckoutを作るadapter。active hostの主経路ではないため、
# C-09固有のfilesystem操作はこの契約の対象外とする。
CHECKOUT_MODULE = "checkout.py"


def _gate_env(tmp_path: Path) -> RuntimeEnv:
    return runtime_env(
        tmp_path,
        state=user_machine_state(Awaiting.USER_INPUT_GATE),
        seeded=(RecordKind.FINAL_REPORT,),
    )


class TestSameSessionAcrossRounds:
    """AC-C08-01。"""

    def test_one_host_instance_serves_every_round(self, tmp_path: Path) -> None:
        env = _gate_env(tmp_path)
        host = gate_host(env)
        result = drive(
            host,
            paths=env.paths,
            config=env.config,
            ports=round_ports(env),
            clock=fixed_clock(),
            max_rounds=6,
        )
        assert result.outcome == Terminal(state=State.CANCELLED)
        assert result.rounds == 3
        # 3 roundすべてが同じinstanceへ届いた（roundごとに作り直されていない）
        assert len(host.executed) == 3
        assert host.session_id == "active-session-1"

    def test_a_round_can_depend_on_the_previous_turn(self, tmp_path: Path) -> None:
        """後のroundが前のroundのcontextを見られる（session memoryが切れていない）。

        `HOST_ACTION`側の`ANSWER_GATE_QUESTION`は、同じinstanceが先に扱った質問turnを
        自分の履歴から読む。sessionが作り直されていればここで失敗する。
        """
        env = _gate_env(tmp_path)
        host = gate_host(env)
        seen: list[tuple[str, ...]] = []
        original = host.execute

        def observing(work: object) -> bytes:
            seen.append(tuple(host.executed))
            return original(work)  # type: ignore[arg-type]

        host.execute = observing  # type: ignore[method-assign, assignment]
        drive(
            host,
            paths=env.paths,
            config=env.config,
            ports=round_ports(env),
            clock=fixed_clock(),
            max_rounds=6,
        )
        assert seen == [(), ("user:GATE_QUESTION",), ("user:GATE_QUESTION", "action:ANSWER_GATE_QUESTION")]


class TestNoSpawnNoKeyInjection:
    """AC-C08-02。"""

    def test_the_fake_host_is_never_spawned_or_typed_into(self, tmp_path: Path) -> None:
        env = _gate_env(tmp_path)
        host = gate_host(env)
        drive(
            host,
            paths=env.paths,
            config=env.config,
            ports=round_ports(env),
            clock=fixed_clock(),
            max_rounds=6,
        )
        assert host.spawned == 0
        assert host.key_injections == 0

    def test_the_active_path_cannot_spawn(self) -> None:
        """**主経路のmoduleはprocessを起動する手段を持たない**（counterではなく構造）。

        AC-C08-02が禁じるのは主経路でのsubprocess起動であって、headless経路の起動ではない
        （headlessはControllerが起動する設計。implementation plan Section 4）。またC-09の
        `checkout.py`は隔離reviewer runtimeを準備するadapterでありactive hostではない。そこで
        これらだけを対象外にし、**他のどのmoduleにも起動手段が無い**ことを固定する。
        `host_headless.py`へ起動が集まっていること自体が、主経路に起動が無いことの裏返しになる。
        """
        offending: dict[str, set[str]] = {}
        for source in _runtime_modules():
            if source.name in {HEADLESS_MODULE, CHECKOUT_MODULE}:
                continue
            hits = _imported_modules(source) & SPAWN_MODULES
            if hits:
                offending[source.name] = hits
        assert offending == {}

    def test_no_module_can_inject_keystrokes(self) -> None:
        """キー入力注入はheadless経路でも行わない（対象外にしない）。"""
        offending = {
            source.name: hits
            for source in _runtime_modules()
            if (hits := _imported_modules(source) & KEY_INJECTION_MODULES)
        }
        assert offending == {}

    def test_only_the_headless_adapter_starts_a_tree(self) -> None:
        """起動は1 moduleへ閉じる。ここが増えたら主経路の契約を見直す合図である。"""
        starters = {
            source.name
            for source in _runtime_modules()
            if "spawn_tree" in _imported_names(source)
        }
        assert starters == {HEADLESS_MODULE}

    def test_process_control_stays_behind_c03(self) -> None:
        """停止も起動も**C-03経由**で、`subprocess`を直接触るmoduleは無い。"""
        directory = Path(runtime_package.__file__).parent
        assert "stop_tree_by_ref" in _imported_names(directory / "ports.py")
        for source in _runtime_modules():
            assert "subprocess" not in _imported_modules(source), source.name


def _runtime_modules() -> list[Path]:
    return sorted(Path(runtime_package.__file__).parent.glob("*.py"))


def _imported_modules(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def _imported_names(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
