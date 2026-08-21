# SPDX-License-Identifier: Apache-2.0
"""C-04のtrust rule（fork PR / trusted author / agent設定file変更の純粋な判定）。

判定はTrustInputのデータだけで決定論的に再現できる（AC-C04-03）。GitHubへの
問い合わせ・actor解決は行わない。承認受理のallowlist照合（D-031、fail closed）は
C-06 / identityの責務であり、本moduleのtrusted_authorsは「fork PRや信頼されていない
authorでagent instructions等の実行を既定拒否する」（target experience）ためのtrust
gating用の集合である。fail closedの意味論はADR-0006を正本とする。

- trusted_authorsが空なら常にuntrusted、head / base repositoryが空ならforkとして扱う
- author照合は完全一致（case-sensitive）。case差は不一致（deny側）に倒れる。
  loginの正規化が必要ならC-06が集合の構築時に行う
- changed_pathsの`..` componentは解決しない（GitHub APIのchanged file一覧には
  現れない前提）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import Final


@unique
class RestrictedAction(Enum):
    """fork PR / untrusted authorで既定拒否する実行の種類（target experience Section 9）。"""

    AGENT_INSTRUCTIONS = "AGENT_INSTRUCTIONS"
    HOOKS = "HOOKS"
    WORKFLOWS = "WORKFLOWS"
    TESTS = "TESTS"


@dataclass(frozen=True)
class TrustInput:
    """trust判定の入力。この値だけで判定が再現できる（AC-C04-03）。

    - base_repository / head_repository: `owner/repo`形式。fork元削除等でheadが
      取得できない場合は空文字列を渡す（fork扱いになる）
    - trusted_authors: trust gating用のauthor login集合（承認受理用allowlistとは別）
    - changed_paths: 対象PRの変更file path一覧（repository root相対）
    """

    base_repository: str
    head_repository: str
    author_login: str
    trusted_authors: frozenset[str]
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class TrustEvaluation:
    """trust判定の結果。

    - agent_config_changes: agent設定fileに該当した変更path（入力の表記のまま。表示用）
    - denied_actions: 既定拒否する実行の集合（fork or untrusted_authorで全種）
    - display_prominently: 目立つ表示が必要か（設定file変更、fork、untrustedのいずれか）
    """

    fork: bool
    untrusted_author: bool
    agent_config_changes: tuple[str, ...]
    denied_actions: frozenset[RestrictedAction]
    display_prominently: bool


# agent設定fileの判定はpath component境界で行う（`.claudex`等の部分一致を誤検出しない）
_AGENT_CONFIG_BASENAMES: Final = frozenset({"claude.md", "agents.md"})
_AGENT_CONFIG_DIRECTORIES: Final = frozenset({".claude", ".codex"})


def _normalize_components(path: str) -> tuple[str, ...]:
    """`\\`区切りと大小差を正規化し、空・`.` componentを除いたpath componentsを返す。"""
    normalized = path.replace("\\", "/").casefold()
    return tuple(component for component in normalized.split("/") if component not in ("", "."))


def _is_agent_config_path(path: str) -> bool:
    components = _normalize_components(path)
    if not components:
        return False
    if components[-1] in _AGENT_CONFIG_BASENAMES:
        return True
    if any(component in _AGENT_CONFIG_DIRECTORIES for component in components):
        return True
    # `.github/workflows/`配下は隣接component対で判定する（`.github/dependabot.yml`は非該当）
    return any(
        first == ".github" and second == "workflows"
        for first, second in zip(components, components[1:], strict=False)
    )


def _is_fork(base_repository: str, head_repository: str) -> bool:
    base = base_repository.strip().casefold()
    head = head_repository.strip().casefold()
    if not base or not head:
        return True  # repositoryを特定できない変更はforkとして扱う（fail closed）
    return base != head


def evaluate_trust(trust_input: TrustInput) -> TrustEvaluation:
    """TrustInputだけからfork / author / 設定file変更を判定する（純粋・決定論的）。"""
    fork = _is_fork(trust_input.base_repository, trust_input.head_repository)
    untrusted_author = (
        not trust_input.trusted_authors or trust_input.author_login not in trust_input.trusted_authors
    )
    agent_config_changes = tuple(
        path for path in trust_input.changed_paths if _is_agent_config_path(path)
    )
    restricted = fork or untrusted_author
    denied_actions = frozenset(RestrictedAction) if restricted else frozenset()
    return TrustEvaluation(
        fork=fork,
        untrusted_author=untrusted_author,
        agent_config_changes=agent_config_changes,
        denied_actions=denied_actions,
        display_prominently=restricted or bool(agent_config_changes),
    )
