# SPDX-License-Identifier: Apache-2.0
"""role設定の初期化・resume binding（ADR-0026）。native adapterはここでは作らない。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from ..identity.errors import IdentityError
from ..identity.fs_permissions import replace_private_text, verify_private_file
from ..schema.envelope import CHECKPOINT
from ..schema.projection import canonical_json
from ..schema.registry import validate_object
from ..schema.session import SESSION_CONFIG
from ..state import (
    CheckpointLoaded,
    CheckpointMissing,
    CheckpointStoreError,
    StatePaths,
    checkpoint_guard,
    checkpoint_path,
    load_checkpoint,
)
from ..workflow import EngineStopped
from .agent_adapter import AdapterProbe, preflight_selection
from .agent_selection import AgentProvider, AgentSelection, SelectionRejected, decode_selection
from .config import ConfigUnavailable, SessionConfig, config_path, read_session_config


@dataclass(frozen=True)
class AgentExecution:
    """呼出側の明示的な実行文脈。active providerは自己申告であり認証ではない。"""

    active_provider: AgentProvider | None = None
    probes: tuple[AdapterProbe, ...] = ()


def initialize_agent_session(
    paths: StatePaths, *, selection: AgentSelection,
    session: Mapping[str, object], checkpoint: Mapping[str, object],
) -> SelectionRejected | None:
    """新run専用。preparing -> checkpoint -> ready。中断は同じ入力でだけ再試行する。"""
    validated = decode_selection(selection.to_bytes())
    if isinstance(validated, SelectionRejected):
        return validated
    for payload in (session, checkpoint):
        if (payload.get("run_id"), payload.get("repository"), payload.get("number")) != (
            selection.run_id, selection.repository, selection.number,
        ):
            return SelectionRejected("selection_target_mismatch")
    bound = dict(checkpoint, agent_selection={"digest": selection.digest})
    prepared = dict(
        session, agent_selection=json.loads(selection.to_bytes()), agent_initialization="preparing",
        agent_initial_checkpoint=hashlib.sha256(canonical_json(bound).encode("utf-8")).hexdigest(),
    )
    if not validate_object(SESSION_CONFIG, prepared).ok or not validate_object(CHECKPOINT, bound).ok:
        return SelectionRejected("agent_initialization_invalid")
    try:
        path = checkpoint_path(paths, selection.run_id)
        session_path = config_path(paths, selection.run_id)
        with checkpoint_guard(path):
            current = load_checkpoint(path)
            if session_path.exists():
                verify_private_file(session_path)
                # retryは完全一致の準備中sessionだけ。完了済みrunの初期状態への巻戻しは禁止。
                if session_path.read_bytes() != canonical_json(prepared).encode("utf-8"):
                    return SelectionRejected("agent_initialization_conflict_new_run_required")
            elif not isinstance(current, CheckpointMissing):
                return SelectionRejected("agent_initialization_conflict_new_run_required")
            if not isinstance(current, CheckpointMissing):
                if not isinstance(current, CheckpointLoaded) or current.payload != bound:
                    return SelectionRejected("agent_initialization_conflict_new_run_required")
            replace_private_text(session_path, canonical_json(prepared))
            replace_private_text(path, canonical_json(bound))
            replace_private_text(session_path, canonical_json(dict(prepared, agent_initialization="ready")))
    except (OSError, IdentityError, CheckpointStoreError):
        return SelectionRejected("agent_initialization_unavailable")
    return None


def check_agent_binding(paths: StatePaths, config: SessionConfig) -> EngineStopped | None:
    """毎操作の前にdiskと呼出側snapshotを再照合。旧runへ設定を推測しない。"""
    try:
        loaded = load_checkpoint(checkpoint_path(paths, config.run_id))
        binding = loaded.payload.get("agent_selection") if isinstance(loaded, CheckpointLoaded) else None
        current = read_session_config(paths, config.run_id)
    except (OSError, IdentityError):
        return EngineStopped("agent_binding_unavailable", "role設定を安全に照合できない")
    # 従来のin-memory callerはsession fileが無い場合もある。両方未選択なら旧契約を維持。
    selected = config.agent_selection
    if selected is None and binding is None:
        if isinstance(current, SessionConfig) and current.agent_selection is not None:
            return EngineStopped("selection_changed_new_run_required", "role設定が呼出側と一致しない")
        # preparing等の選択ありfileはlegacyへ迂回しない。
        if isinstance(current, ConfigUnavailable) and config_path(paths, config.run_id).exists():
            return EngineStopped("agent_binding_unavailable", current.detail)
        return None
    if not isinstance(current, SessionConfig):
        return EngineStopped("agent_binding_unavailable", "保存済みrole設定を復元できない")
    if selected is None or current.agent_selection != selected:
        return EngineStopped("selection_changed_new_run_required", "role設定の変更には新runが必要")
    validated = decode_selection(selected.to_bytes())
    if isinstance(validated, SelectionRejected):
        return EngineStopped(validated.code, "role設定が不正")
    if (selected.run_id, selected.repository, selected.number) != (config.run_id, config.repository, config.number):
        return EngineStopped("selection_target_mismatch", "role設定の対象が一致しない")
    if not isinstance(loaded, CheckpointLoaded):
        return EngineStopped("agent_binding_unavailable", "checkpointを復元できない")
    if (loaded.payload.get("run_id"), loaded.payload.get("repository"), loaded.payload.get("number")) != (
        config.run_id, config.repository, config.number,
    ):
        return EngineStopped("selection_target_mismatch", "checkpointの対象が一致しない")
    if binding != {"digest": selected.digest}:
        return EngineStopped("selection_binding_mismatch", "role設定とcheckpointが一致しない")
    return None


def check_agent_execution(config: SessionConfig, execution: AgentExecution) -> EngineStopped | None:
    """エージェントへ作業を返す前に検証。旧runにnative対応を推測して追加しない。"""
    if config.agent_selection is None:
        return None
    rejected = preflight_selection(
        config.agent_selection, execution.probes, active_provider=execution.active_provider,
    )
    if rejected is None:
        return None
    paths = ",".join(error.path for error in rejected.errors)
    return EngineStopped(rejected.code, f"role事前検証に失敗（{paths}）")
