# SPDX-License-Identifier: Apache-2.0
"""provider adapterの事前検証契約。実CLI起動・host駆動は行わない（ADR-0025）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from .agent_selection import (
    AgentMode,
    AgentProvider,
    AgentRole,
    AgentSelection,
    RoleSelection,
    SelectionRejected,
    decode_selection,
)


@dataclass(frozen=True)
class AdapterKey:
    """providerだけでなくrole・実行方式・安全契約・versionまで一致させる。"""

    role: AgentRole
    provider: AgentProvider
    mode: AgentMode
    safety_profile: str
    contract_version: int


ProbeFailure = Literal["cli_missing", "authentication_unavailable", "capability_unavailable", "version_unsupported"]


@dataclass(frozen=True)
class AdapterReadiness:
    """adapterがnative CLI・認証・安全機能・modelを検証した結果。秘密値は持たない。"""

    failure: ProbeFailure | None


class AdapterProbe(Protocol):
    """起動前検証だけのinterface。executeはrole別に後続PRで接続する。"""

    @property
    def key(self) -> AdapterKey: ...

    def check(self, selection: RoleSelection) -> AdapterReadiness: ...


def preflight_selection(
    selection: AgentSelection,
    probes: Sequence[AdapterProbe],
    *,
    active_provider: AgentProvider | None,
) -> SelectionRejected | None:
    """全roleが対応済みである場合だけ成功。既定probeはなく、無断fallbackしない。

    成功はprobeの申告を検証した結果であり、隔離の実証ではない。実装側が信用する
    probeだけを渡すこと。same-providerでもrole別probeを要求し、instanceを共有しない。
    """
    validated = decode_selection(selection.to_bytes())
    if isinstance(validated, SelectionRejected):
        return validated
    if selection.coder.mode == "active":
        if active_provider != selection.coder.provider:
            return SelectionRejected("active_provider_mismatch")
    elif active_provider is not None:
        return SelectionRejected("active_host_in_headless_mode")
    registered: dict[AdapterKey, AdapterProbe] = {}
    for probe in probes:
        if probe.key in registered:
            return SelectionRejected("adapter_duplicate")
        registered[probe.key] = probe
    roles: tuple[tuple[AgentRole, RoleSelection], ...] = (("coder", selection.coder), ("reviewer", selection.reviewer))
    for role, selected in roles:
        key = AdapterKey(
            role=role,
            provider=selected.provider,
            mode=selected.mode,
            safety_profile=selected.safety_profile,
            contract_version=selected.adapter_contract_version,
        )
        selected_probe = registered.get(key)
        if selected_probe is None:
            return SelectionRejected("adapter_unavailable")
        readiness = selected_probe.check(selected)
        if readiness.failure is not None:
            return SelectionRejected(readiness.failure)
    return None
