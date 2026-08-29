# SPDX-License-Identifier: Apache-2.0
"""CI artifactの収集範囲のcontract test（Phase 8 PR-3b1。ADR-0020）。

Issue #13の契約: **checkpoint / canonical record / envelope / redact済みlogを対象にし、
credential・token・未redact入力は含めない**。

収集するのは**checkpoint / envelope / redact済みlog**の3種である（PR-3b1で前2種、
PR-3b3で`host.log`。ADR-0022）。canonical recordのlocal artifact（`artifact_records`）だけは
**producerが未実装**で、C-09以降が入れる。ここが検査するのは「収集しているものが契約の
範囲内か」であって、「4種すべてを収集しているか」ではない。

`result.json`はhostが返した実行結果とユーザーの入力そのもので、redactを通っていない。
workflowのpathが`*.json`のようなwildcardへ広がった瞬間に混ざるため、収集file名を
**製品定数と突き合わせて**固定する。

workflowのYAMLはpyyamlを持ち込まずに読む（開発用依存はexact pinで最小に保つ方針）。
対象は自分たちが書いたfileで形が安定しており、形が変わればここがfailする。
"""

from __future__ import annotations

from pathlib import Path

from claude_code_codex_review_loop.runtime.host_headless import (
    LOG_FILE as HOST_LOG,
)
from claude_code_codex_review_loop.runtime.host_headless import (
    STDERR_FILE as HOST_STDERR,
)
from claude_code_codex_review_loop.runtime.host_headless import (
    STDOUT_FILE as HOST_STDOUT,
)
from claude_code_codex_review_loop.state import CHECKPOINT_FILE_NAME
from claude_code_codex_review_loop.workflow.engine import ACTIONS_DIR
from claude_code_codex_review_loop.workflow.engine import ENVELOPE_FILE as ACTION_ENVELOPE
from claude_code_codex_review_loop.workflow.engine import RESULT_FILE as ACTION_RESULT
from claude_code_codex_review_loop.workflow.user_input import ENVELOPE_FILE as REQUEST_ENVELOPE
from claude_code_codex_review_loop.workflow.user_input import REQUESTS_DIR
from claude_code_codex_review_loop.workflow.user_input import RESULT_FILE as REQUEST_RESULT

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "test.yml"
STEP_NAME = "Collect run state on failure"


def _collected_paths() -> tuple[str, ...]:
    """artifact収集stepの`path:`blockを読む（block scalarの行だけを取る）。"""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {STEP_NAME}")
    path_line = next(
        i for i in range(start, len(lines)) if lines[i].strip() in ("path: |", "path: |-")
    )
    indent = len(lines[path_line]) - len(lines[path_line].lstrip())
    collected: list[str] = []
    for line in lines[path_line + 1 :]:
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        collected.append(line.strip())
    return tuple(collected)


def test_the_step_exists_and_declares_paths() -> None:
    """parseが空振りしたまま「漏れていない」と主張しないようにする。"""
    assert _collected_paths(), WORKFLOW


def test_only_checkpoints_envelopes_and_redacted_logs_are_collected() -> None:
    """収集するfile名は製品定数の4つだけ（wildcardへ広げない）。"""
    allowed = {CHECKPOINT_FILE_NAME, ACTION_ENVELOPE, REQUEST_ENVELOPE, HOST_LOG}
    names = {pattern.rsplit("/", 1)[-1] for pattern in _collected_paths()}
    assert names == allowed


def test_unredacted_files_are_never_collected() -> None:
    """未redactのhost出力・ユーザー入力・logは収集しない（Issue #13の契約）。

    `result.json`はhostが返した結果とユーザーの入力そのもの、`host.stdout`はsubmit envelope、
    `host.stderr`は**redact前**のlogである。redactを通った`host.log`だけを集める。
    """
    forbidden = {ACTION_RESULT, REQUEST_RESULT, HOST_STDOUT, HOST_STDERR}
    for pattern in _collected_paths():
        name = pattern.rsplit("/", 1)[-1]
        assert name not in forbidden, pattern
        # `*.json`のようなwildcardは`result.json`も引く
        assert "*" not in name, pattern


def test_each_envelope_directory_is_covered() -> None:
    """actionとuser requestの両方のenvelopeが対象に入っている（片側の取りこぼし防止）。"""
    patterns = _collected_paths()
    for directory in (ACTIONS_DIR, REQUESTS_DIR):
        names = {
            pattern.rsplit("/", 1)[-1] for pattern in patterns if f"{directory}/" in pattern
        }
        assert names == {
            ACTION_ENVELOPE if directory == ACTIONS_DIR else REQUEST_ENVELOPE,
            HOST_LOG,
        }, (directory, names)


def test_the_session_config_is_not_collected() -> None:
    """session configは`gh_env`（実行環境の環境変数）を持つため対象外にする。"""
    from claude_code_codex_review_loop.runtime import CONFIG_FILE

    assert all(CONFIG_FILE not in pattern for pattern in _collected_paths())
