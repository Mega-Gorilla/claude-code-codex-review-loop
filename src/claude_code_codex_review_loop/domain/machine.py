# SPDX-License-Identifier: Apache-2.0
"""C-01 domain state machineのengine。

単一のregistry（REGISTRY）が完全遷移の正本であり、`transition`はそれを解釈するだけの
純粋関数である。到達可能な組合せ × 各eventに一致するruleは0件または1件で、0件は構造化error、
2件以上はregistryの欠陥として拒否する（優先順位による解決はしない。AC-C01-02 / 08）。
"""

from __future__ import annotations

from ._ruledefs import GuardKey, Match, PendingMatch, ProcedureKind, Rule, derive_guard_key
from ._rules_integrity import INTEGRITY_RULES
from ._rules_recovery import RECOVERY_RULES
from ._rules_workflow import PRODUCED_RULES, WORKFLOW_RULES
from .commands import CodexPurpose, Command, RequestCodexReview
from .events import Event, PreflightEvent, PreflightOk
from .states import TERMINAL_STATES, State
from .values import Awaiting, MachineState, RegistryIntegrityError, TransitionRejected

__all__ = [
    "REGISTRY",
    "GuardKey",
    "Match",
    "PendingMatch",
    "ProcedureKind",
    "Rule",
    "check_registry",
    "derive_guard_key",
    "initialize",
    "select_rule",
    "transition",
]

REGISTRY: tuple[Rule, ...] = PRODUCED_RULES + WORKFLOW_RULES + RECOVERY_RULES + INTEGRITY_RULES


def check_registry(rules: tuple[Rule, ...]) -> None:
    """registryの基本自己検査。rule_idの重複とterminal stateを起点とするruleを拒否する。"""
    seen: set[str] = set()
    for rule in rules:
        if rule.rule_id in seen:
            raise RegistryIntegrityError(f"rule_id重複: {rule.rule_id}")
        seen.add(rule.rule_id)
        if rule.match.states & TERMINAL_STATES:
            raise RegistryIntegrityError(f"terminal stateを起点にできない: {rule.rule_id}")


check_registry(REGISTRY)

_INDEX: dict[type, tuple[Rule, ...]] = {}
for _rule in REGISTRY:
    _INDEX[_rule.match.event_type] = _INDEX.get(_rule.match.event_type, ()) + (_rule,)


def initialize(preflight_event: PreflightEvent) -> tuple[MachineState, tuple[Command, ...]]:
    """runの開始。preflight成功でRUNNING_REVIEWへ入り、最初のCodex reviewを必ず開始する。

    失敗時はcommandなしのFAILED（resume系eventを拒否し、復帰は新runのinitializeのみ）。
    可視stateに「未開始」は追加しない（Phase 1計画の節4）。
    """
    if isinstance(preflight_event, PreflightOk):
        return (
            MachineState(state=State.RUNNING_REVIEW, awaiting=Awaiting.CODEX_CODE_REVIEW),
            (RequestCodexReview(CodexPurpose.CODE_REVIEW),),
        )
    return MachineState(state=State.FAILED), ()


def select_rule(rules: tuple[Rule, ...], machine_state: MachineState, event: Event) -> Rule:
    """一致ruleの選択。0件は構造化error、2件以上は優先順位で解決せずregistry欠陥として拒否する。"""
    key = derive_guard_key(machine_state, event)
    matched = [rule for rule in rules if rule.match.matches(machine_state.state, event, key)]
    if not matched:
        raise TransitionRejected(machine_state.state, type(event).__name__, f"一致するruleがない（guard: {key}）")
    if len(matched) > 1:
        ids = ", ".join(rule.rule_id for rule in matched)
        raise RegistryIntegrityError(f"複数ruleが一致した（優先順位による解決はしない）: {ids}")
    return matched[0]


def transition(machine_state: MachineState, event: Event) -> tuple[MachineState, tuple[Command, ...]]:
    """遷移判断。未定義の(state, event, guard値)はsilent no-opにせず構造化errorで拒否する。"""
    if machine_state.state in TERMINAL_STATES:
        raise TransitionRejected(machine_state.state, type(event).__name__, "terminal stateは全eventを拒否する")
    rule = select_rule(_INDEX.get(type(event), ()), machine_state, event)
    return rule.effect(machine_state, event)
