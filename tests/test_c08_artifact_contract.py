# SPDX-License-Identifier: Apache-2.0
"""CI artifactの収集範囲のcontract test（Phase 8 PR-3b1。ADR-0020）。

Issue #13の契約: **checkpoint / canonical record / envelope / redact済みlogを対象にし、
credential・token・未redact入力は含めない**。

PR-3b1が収集するのは**checkpointとenvelopeの2種**である。canonical recordのlocal
artifact（`artifact_records`）とredact済みlogは**producerが未実装**で、収集はPR-3b2が
producerと同じPRで入れる（ADR-0020 決定30-a / 30-b）。ここが検査するのは
「収集しているものが契約の範囲内か」であって、「4種すべてを収集しているか」ではない。

`result.json`はhostが返した実行結果とユーザーの入力そのもので、redactを通っていない。
workflowのpathが`*.json`のようなwildcardへ広がった瞬間に混ざるため、収集file名を
**製品定数と突き合わせて**固定する。

workflowのYAMLはpyyamlを持ち込まずに読む（開発用依存はexact pinで最小に保つ方針）。
対象は自分たちが書いたfileで形が安定しており、形が変わればここがfailする。
"""

from __future__ import annotations

from pathlib import Path

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


def test_only_checkpoints_and_envelopes_are_collected() -> None:
    """収集するfile名は製品定数の3つだけ（wildcardへ広げない）。"""
    allowed = {CHECKPOINT_FILE_NAME, ACTION_ENVELOPE, REQUEST_ENVELOPE}
    names = {pattern.rsplit("/", 1)[-1] for pattern in _collected_paths()}
    assert names == allowed


def test_result_files_are_never_collected() -> None:
    """`result.json`には未redactのhost出力とユーザー入力が入る（Issue #13の契約）。"""
    forbidden = {ACTION_RESULT, REQUEST_RESULT}
    for pattern in _collected_paths():
        name = pattern.rsplit("/", 1)[-1]
        assert name not in forbidden, pattern
        # `*.json`のようなwildcardは`result.json`も引く
        assert "*" not in name, pattern


def test_each_envelope_directory_is_covered() -> None:
    """actionとuser requestの両方のenvelopeが対象に入っている（片側の取りこぼし防止）。"""
    patterns = _collected_paths()
    assert any(f"{ACTIONS_DIR}/" in pattern for pattern in patterns), patterns
    assert any(f"{REQUESTS_DIR}/" in pattern for pattern in patterns), patterns


def test_the_session_config_is_not_collected() -> None:
    """session configは`gh_env`（実行環境の環境変数）を持つため対象外にする。"""
    from claude_code_codex_review_loop.runtime import CONFIG_FILE

    assert all(CONFIG_FILE not in pattern for pattern in _collected_paths())
