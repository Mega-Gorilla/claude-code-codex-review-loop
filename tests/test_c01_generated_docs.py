# SPDX-License-Identifier: Apache-2.0
"""R5系列: 生成した遷移表・遷移図と文書のsnapshot照合（AC-C01-01）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RENDERER_PATH = ROOT / "tools" / "render_c01_docs.py"
DOCUMENT_PATH = ROOT / "docs" / "architecture" / "c01-state-machine.md"


def _load_renderer():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("render_c01_docs", RENDERER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_document_matches_registry_snapshot() -> None:
    """文書がregistryから導出した内容と一致する（乖離したら`python tools/render_c01_docs.py`で再生成する）。"""
    renderer = _load_renderer()
    expected = renderer.render_document()
    actual = DOCUMENT_PATH.read_text(encoding="utf-8")
    detail = "docs/architecture/c01-state-machine.mdがregistryと乖離している。再生成して差分をPRへ含める"
    assert actual == expected, detail


def test_rendering_is_deterministic() -> None:
    renderer = _load_renderer()
    assert renderer.render_document() == renderer.render_document()


def test_diagram_covers_all_states() -> None:
    """遷移図に17 stateすべてが現れる（到達可能性の可視化が退化しない）。"""
    renderer = _load_renderer()
    diagram = renderer.render_state_diagram()
    from claude_code_codex_review_loop.domain import State

    for state in State:
        assert state.value in diagram, state.value
