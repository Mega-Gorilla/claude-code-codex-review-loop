# SPDX-License-Identifier: Apache-2.0
"""C-04 trust ruleの受入test（AC-C04-03）。

fork判定・author判定・agent設定file変更判定が、TrustInputのデータだけで
決定論的に再現できることと、fail closedの意味論（ADR-0006）を固定する。
"""

from __future__ import annotations

import pytest

from claude_code_codex_review_loop.policy import (
    RestrictedAction,
    TrustInput,
    evaluate_trust,
)

_ALL_ACTIONS = frozenset(RestrictedAction)


def _input(
    *,
    base: str = "owner/repo",
    head: str = "owner/repo",
    author: str = "alice",
    trusted: frozenset[str] = frozenset({"alice"}),
    paths: tuple[str, ...] = (),
) -> TrustInput:
    return TrustInput(
        base_repository=base,
        head_repository=head,
        author_login=author,
        trusted_authors=trusted,
        changed_paths=paths,
    )


class TestFork:
    def test_same_repository_is_not_fork(self) -> None:
        assert evaluate_trust(_input()).fork is False

    def test_case_difference_is_not_fork(self) -> None:
        """GitHubのowner/repoはcase-insensitive。case差でfork誤検知しない。"""
        assert evaluate_trust(_input(base="Owner/Repo", head="owner/repo")).fork is False

    def test_different_repository_is_fork(self) -> None:
        result = evaluate_trust(_input(head="fork-owner/repo"))
        assert result.fork is True
        assert result.denied_actions == _ALL_ACTIONS

    @pytest.mark.parametrize("base,head", [("", "owner/repo"), ("owner/repo", ""), ("  ", "owner/repo")])
    def test_missing_repository_is_fork_fail_closed(self, base: str, head: str) -> None:
        """fork元削除等でrepositoryを特定できない場合はforkとして扱う（fail closed）。"""
        assert evaluate_trust(_input(base=base, head=head)).fork is True


class TestAuthor:
    def test_matching_author_is_trusted(self) -> None:
        result = evaluate_trust(_input())
        assert result.untrusted_author is False
        assert result.denied_actions == frozenset()

    def test_unknown_author_is_untrusted(self) -> None:
        result = evaluate_trust(_input(author="mallory"))
        assert result.untrusted_author is True
        assert result.denied_actions == _ALL_ACTIONS

    def test_empty_trusted_set_is_fail_closed(self) -> None:
        assert evaluate_trust(_input(trusted=frozenset())).untrusted_author is True

    def test_case_difference_is_untrusted(self) -> None:
        """author照合は完全一致。case差は不一致（deny側）に倒れる。"""
        assert evaluate_trust(_input(author="Alice")).untrusted_author is True

    def test_empty_author_is_untrusted(self) -> None:
        assert evaluate_trust(_input(author="")).untrusted_author is True


class TestAgentConfigPaths:
    @pytest.mark.parametrize(
        "path",
        [
            "CLAUDE.md",
            "sub/dir/AGENTS.md",
            "claude.md",  # 大小差
            ".claude/settings.json",
            "a/.codex/config.toml",
            "deep/.claude",  # 末尾componentがdirectory名そのもの
            ".github/workflows/test.yml",
            "sub/.github/workflows/x.yml",  # 任意深さ
            ".claude\\settings.local.json",  # Windows区切り
            "./.codex/x",  # `.` component
        ],
    )
    def test_agent_config_paths_are_detected(self, path: str) -> None:
        result = evaluate_trust(_input(paths=(path,)))
        assert result.agent_config_changes == (path,), path  # 元の表記のまま返す

    @pytest.mark.parametrize(
        "path",
        [
            "src/main.py",
            ".claudex/file.txt",  # 部分一致は非該当
            ".github/dependabot.yml",  # workflows対でない
            "docs/claude.md.bak",  # basename不一致
            "myclaude.md.py",
            "",  # 空path
            ".",
        ],
    )
    def test_other_paths_are_not_detected(self, path: str) -> None:
        assert evaluate_trust(_input(paths=(path,))).agent_config_changes == ()

    def test_mixed_paths_preserve_order_and_originals(self) -> None:
        paths = ("src/a.py", "CLAUDE.md", "README.md", ".github/workflows/ci.yml")
        result = evaluate_trust(_input(paths=paths))
        assert result.agent_config_changes == ("CLAUDE.md", ".github/workflows/ci.yml")


class TestEvaluationComposition:
    def test_trusted_same_repo_config_change_displays_without_deny(self) -> None:
        """信頼済み・同一repoの設定file変更は「denied空・目立つ表示あり」（TE L625）。"""
        result = evaluate_trust(_input(paths=("CLAUDE.md",)))
        assert result.denied_actions == frozenset()
        assert result.display_prominently is True

    def test_fully_clean_change(self) -> None:
        result = evaluate_trust(_input(paths=("src/a.py",)))
        assert result.fork is False
        assert result.untrusted_author is False
        assert result.agent_config_changes == ()
        assert result.denied_actions == frozenset()
        assert result.display_prominently is False

    def test_fork_denies_all_restricted_actions(self) -> None:
        """fork PRではagent instructions / hooks / workflow / testの実行を既定拒否（TE L626）。"""
        result = evaluate_trust(_input(head="fork-owner/repo", paths=("src/a.py",)))
        assert result.denied_actions == _ALL_ACTIONS
        assert result.display_prominently is True

    def test_untrusted_author_denies_all_restricted_actions(self) -> None:
        result = evaluate_trust(_input(author="mallory"))
        assert result.denied_actions == _ALL_ACTIONS
        assert result.display_prominently is True

    def test_reproducible_from_input_only(self) -> None:
        """同一入力からは常に同一結果（AC-C04-03の決定論性）。"""
        trust_input = _input(head="fork-owner/repo", paths=("CLAUDE.md", "src/a.py"))
        assert evaluate_trust(trust_input) == evaluate_trust(trust_input)
