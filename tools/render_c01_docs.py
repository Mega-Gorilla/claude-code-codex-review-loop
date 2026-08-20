# SPDX-License-Identifier: Apache-2.0
"""C-01 registryから遷移表・遷移図を生成する（AC-C01-01）。

生成先は`docs/architecture/c01-state-machine.md`。testが同一内容とのsnapshot照合を行うため、
出力は決定論的でなければならない。再生成は`python tools/render_c01_docs.py`。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from claude_code_codex_review_loop.domain._ruledefs import Match, Rule  # noqa: E402
from claude_code_codex_review_loop.domain.machine import REGISTRY  # noqa: E402
from claude_code_codex_review_loop.domain.states import State  # noqa: E402

OUTPUT_PATH = ROOT / "docs" / "architecture" / "c01-state-machine.md"

_SECTION_TITLES: dict[str, str] = {
    "record": "canonical record（PRODUCED規約）",
    "workflow": "main workflow",
    "progress": "bounded progress（block進入）",
    "decision": "decision flow",
    "ci": "CI",
    "report": "final report",
    "gate": "merge gate",
    "merge": "merge transaction",
    "cancel": "cancellation",
    "failure": "失敗（EV_RUN_FAILED）",
    "procedure": "横断規則（手続き中の冪等再発行）",
    "resume": "resume",
    "integrity": "integrity violation",
    "incident": "incident監査",
    "block": "block解消",
}


def _fmt_states(states: frozenset[State]) -> str:
    if states == frozenset(State) - {State.MERGED, State.CANCELLED}:
        return "terminal以外の全state"
    return " / ".join(sorted(s.value for s in states))


def _fmt_set(values: frozenset[object] | None) -> str | None:
    if values is None:
        return None
    return " / ".join(sorted("None" if v is None else getattr(v, "value", str(v)) for v in values))


def _fmt_guard(match: Match) -> str:
    parts: list[str] = []
    procedures = _fmt_set(frozenset(match.procedures))
    if procedures != "NORMAL":
        parts.append(f"procedure = {procedures}")
    for label, value in (
        ("awaiting", _fmt_set(match.awaiting)),
        ("pending", _fmt_set(match.pending)),
        ("record kind", _fmt_set(match.record_kinds)),
        ("progress", _fmt_set(match.progress)),
        ("binding", _fmt_set(match.binding)),
        ("block", _fmt_set(match.block_kinds)),
        ("block reason", _fmt_set(match.block_reasons)),
        ("coverage", _fmt_set(match.coverage)),
    ):
        if value is not None:
            parts.append(f"{label} = {value}")
    if match.deferred_nonempty is not None:
        parts.append("deferredあり" if match.deferred_nonempty else "deferredなし")
    if match.recovery_present is not None:
        parts.append("recovery_toあり" if match.recovery_present else "recovery_toなし")
    return "、".join(parts) if parts else "—"


def _table_row(rule: Rule) -> str:
    commands = "、".join(rule.command_names) if rule.command_names else "—"
    return (
        f"| {rule.rule_id} | {_fmt_states(rule.match.states)} | {rule.match.event_type.__name__} "
        f"| {_fmt_guard(rule.match)} | {rule.to_state} | {commands} |"
    )


def render_transition_table() -> str:
    lines: list[str] = []
    seen_sections: list[str] = []
    for rule in REGISTRY:
        if rule.section not in seen_sections:
            seen_sections.append(rule.section)
    for section in seen_sections:
        lines.append(f"### {_SECTION_TITLES.get(section, section)}")
        lines.append("")
        lines.append("| Rule | From | Event | Guard | To | Commands |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for rule in REGISTRY:
            if rule.section == section:
                lines.append(_table_row(rule))
        lines.append("")
    return "\n".join(lines)


def render_state_diagram() -> str:
    """可視stateの遷移図（mermaid）。to_stateが具体的なstateのruleだけを辺にする。"""
    state_values = {s.value for s in State}
    edges: dict[tuple[str, str], list[str]] = {}
    for rule in REGISTRY:
        if rule.to_state not in state_values:
            continue
        for from_state in rule.match.states:
            if from_state.value == rule.to_state:
                continue
            edges.setdefault((from_state.value, rule.to_state), []).append(rule.rule_id)
    lines = [
        "```mermaid",
        "stateDiagram-v2",
        "    [*] --> RUNNING_REVIEW: initialize(preflight OK)",
        "    [*] --> FAILED: initialize(preflight NG)",
    ]
    for (src, dst), rule_ids in sorted(edges.items()):
        lines.append(f"    {src} --> {dst}: {', '.join(sorted(rule_ids))}")
    lines.append("    MERGED --> [*]")
    lines.append("    CANCELLED --> [*]")
    lines.append("```")
    return "\n".join(lines)


def render_document() -> str:
    header = (
        "<!-- SPDX-License-Identifier: Apache-2.0 -->\n\n"
        "# C-01 state machine（生成文書）\n\n"
        "| Field | Value |\n"
        "| --- | --- |\n"
        "| Status | **生成file** — 手動で編集しない。正本はC-01実装のcode registry"
        "（`src/claude_code_codex_review_loop/domain/machine.py`のREGISTRY）と"
        "property / sequence test |\n"
        "| 再生成 | `python tools/render_c01_docs.py`（snapshot照合testが一致を強制する） |\n"
        "| 契約の正本 | [Phase 1計画](../plans/phase-01-domain-state-machine.md) / "
        "[implementation plan](../plans/implementation-plan.md)のC-01節 |\n\n"
        f"registryのrule数: **{len(REGISTRY)}**。guard列は有限discriminatorのみで構成され、"
        "一致ruleが常に0件または1件であることをproperty testが全数列挙で検証する（AC-C01-08）。\n"
        "eventの受理には、VERIFIED系はpending（単一slot）とのkind / binding一致、解消系は"
        "対象blockとの完全一致が追加で要求される（表のpending / binding列）。\n\n"
        "## 遷移図（可視stateの辺のみ。同一state内の遷移とcommandは遷移表を参照）\n\n"
    )
    footer = "\n## 遷移表（registryから生成）\n\n"
    return header + render_state_diagram() + "\n" + footer + render_transition_table()


def main() -> None:
    OUTPUT_PATH.write_text(render_document(), encoding="utf-8", newline="\n")
    print(f"generated: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
