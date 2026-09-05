# SPDX-License-Identifier: Apache-2.0
"""provider設定のimmutableなcodecとresume比較（ADR-0025）。

本moduleはI/Oを行わず、session.jsonやcheckpointの保存経路へはまだ接続しない。
旧設定のspeaker/modelからproviderを推測せず、旧runの暗黙変換も行わない。
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Literal, cast

from ..schema.agents import AGENT_SELECTION
from ..schema.projection import ProjectionError, canonical_json, validate_run_id
from ..schema.registry import validate
from ..schema.validate import PublicError
from ..transport.gh import RepoRef, TransportError

AgentRole = Literal["coder", "reviewer"]
AgentProvider = Literal["claude", "codex"]
AgentMode = Literal["active", "headless", "fresh"]


@dataclass(frozen=True)
class RoleSelection:
    """1 roleの明示設定。秘密値・任意argv・環境変数は持たない。"""

    provider: AgentProvider
    model: str
    mode: AgentMode
    safety_profile: str
    adapter_contract_version: int


@dataclass(frozen=True)
class AgentSelection:
    """runへbindする解決済みsnapshot。nested値も不変である。"""

    run_id: str
    repository: str
    number: int
    coder: RoleSelection
    reviewer: RoleSelection

    def to_bytes(self) -> bytes:
        """固定v1のcanonical表現。保存側はdecodeで再検証してから使う。"""
        return canonical_json({"schema_version": 1, **asdict(self)}).encode("utf-8")

    @property
    def digest(self) -> str:
        """変更検出用。認証やsecret保護を提供するhashではない。"""
        return hashlib.sha256(self.to_bytes()).hexdigest()


@dataclass(frozen=True)
class SelectionRejected:
    """入力値を含まない構造化拒否。"""

    code: str
    errors: tuple[PublicError, ...] = ()


def _role(data: object) -> RoleSelection:
    values = cast(dict[str, object], data)
    return RoleSelection(
        provider=cast(AgentProvider, values["provider"]),
        model=cast(str, values["model"]),
        mode=cast(AgentMode, values["mode"]),
        safety_profile=cast(str, values["safety_profile"]),
        adapter_contract_version=cast(int, values["adapter_contract_version"]),
    )


def decode_selection(raw: bytes) -> AgentSelection | SelectionRejected:
    """共通C-02 validatorで検証し、表示名から選択を補完しない。"""
    result = validate(AGENT_SELECTION, raw)
    if not result.ok or result.payload is None:
        return SelectionRejected("selection_invalid", result.errors)
    payload = result.payload
    run_id, repository = str(payload["run_id"]), str(payload["repository"])
    try:
        validate_run_id(run_id)
        owner, separator, name = repository.partition("/")
        if not separator:
            return SelectionRejected("selection_target_invalid")
        RepoRef(owner=owner, name=name)
    except (TransportError, ProjectionError):
        return SelectionRejected("selection_target_invalid")
    return AgentSelection(
        run_id=run_id,
        repository=repository,
        number=cast(int, payload["number"]),
        coder=_role(payload["coder"]),
        reviewer=_role(payload["reviewer"]),
    )


def restore_selection(
    raw: bytes | None,
    *,
    run_id: str,
    repository: str,
    number: int,
    requested: AgentSelection | None = None,
) -> AgentSelection | SelectionRejected:
    """別processで読んだsnapshotを復元し、targetや設定の切替を拒否する。

    snapshotを持たない旧runは説明付き拒否とする。この関数を使わない既存C-08の
    resume経路は変えない。callerは新runを案内し、旧recordや未完了本文を書き換えない。
    """
    if raw is None:
        return SelectionRejected("selection_missing_new_run_required")
    selection = decode_selection(raw)
    if isinstance(selection, SelectionRejected):
        return selection
    if (selection.run_id, selection.repository, selection.number) != (run_id, repository, number):
        return SelectionRejected("selection_target_mismatch")
    if requested is not None and selection != requested:
        return SelectionRejected("selection_changed_new_run_required")
    return selection
