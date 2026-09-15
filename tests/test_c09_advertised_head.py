# SPDX-License-Identifier: Apache-2.0
"""`AdvertisedHeadPort`のC-05実装のtest。fake `gh`（c05_support）を使い、実GitHubへは接続しない。"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from c05_support.helpers import make_context, make_policy, seed_state

from claude_code_codex_review_loop.runtime.advertised_head import GitHubAdvertisedHead
from claude_code_codex_review_loop.runtime.reviewer_turn import AdvertisedHeadPort, TurnError

_HEAD = "a" * 40
_BASE = "b" * 40


def _pull(number: int = 12, head: str = _HEAD) -> dict[str, object]:
    return {
        "number": number,
        "state": "open",
        "merged": False,
        "updated_at": "2026-09-15T09:00:00Z",
        "user": {"login": "alice"},
        "head": {"sha": head, "ref": "topic", "repo": {"full_name": "o/r"}},
        "base": {"sha": _BASE, "ref": "main", "repo": {"full_name": "o/r"}},
    }


def _port(tmp_path: Path, scenario: str = "ok") -> GitHubAdvertisedHead:
    return GitHubAdvertisedHead(
        context=make_context(tmp_path, scenario=scenario), policy=make_policy(backoff_seconds=0.0)
    )


class TestGitHubAdvertisedHead:
    def test_returns_the_current_head_sha_unchanged(self, tmp_path: Path) -> None:
        seed_state(tmp_path, pull_requests=[_pull()])
        port: AdvertisedHeadPort = _port(tmp_path)
        assert port.advertised_head(repository="o/r", number=12) == _HEAD

    def test_reads_github_on_every_call(self, tmp_path: Path) -> None:
        """snapshotを持たない。呼出ごとにGitHubを読むため、pushされた新しいheadが見える。"""
        seed_state(tmp_path, pull_requests=[_pull()])
        port = _port(tmp_path)
        assert port.advertised_head(repository="o/r", number=12) == _HEAD
        seed_state(tmp_path, pull_requests=[_pull(head="c" * 40)])
        assert port.advertised_head(repository="o/r", number=12) == "c" * 40

    def test_missing_pull_request_is_a_fixed_stage(self, tmp_path: Path) -> None:
        seed_state(tmp_path, pull_requests=[])
        with pytest.raises(TurnError) as stopped:
            _port(tmp_path).advertised_head(repository="o/r", number=12)
        assert stopped.value.stage == "advertised_head:not_found"
        assert "o/r" not in str(stopped.value) and "12" not in str(stopped.value)

    def test_transient_failure_is_retried_within_the_policy(self, tmp_path: Path) -> None:
        seed_state(tmp_path, pull_requests=[_pull()])
        assert _port(tmp_path, scenario="s500,ok").advertised_head(repository="o/r", number=12) == _HEAD

    def test_exhausted_transient_failures_are_reported(self, tmp_path: Path) -> None:
        seed_state(tmp_path, pull_requests=[_pull()])
        with pytest.raises(TurnError) as stopped:
            _port(tmp_path, scenario="s500,s500,s500,s500").advertised_head(repository="o/r", number=12)
        assert stopped.value.stage == "advertised_head:transient"

    def test_auth_failure_is_a_fixed_stage(self, tmp_path: Path) -> None:
        seed_state(tmp_path, pull_requests=[_pull()])
        with pytest.raises(TurnError) as stopped:
            _port(tmp_path, scenario="a401").advertised_head(repository="o/r", number=12)
        assert stopped.value.stage == "advertised_head:auth"

    def test_repository_with_invalid_characters_is_rejected_by_c05_validation(self, tmp_path: Path) -> None:
        seed_state(tmp_path, pull_requests=[_pull()])
        with pytest.raises(TurnError) as stopped:
            _port(tmp_path).advertised_head(repository="o/r name", number=12)
        assert stopped.value.stage == "advertised_head:permanent"

    @pytest.mark.parametrize("repository", ("no-slash", "/r", "o/", "o/r/x", ""))
    def test_invalid_repository_slug_is_rejected_without_calling_gh(self, tmp_path: Path, repository: str) -> None:
        seed_state(tmp_path, pull_requests=[_pull()])
        with pytest.raises(TurnError) as stopped:
            _port(tmp_path).advertised_head(repository=repository, number=12)
        assert stopped.value.stage == "advertised_head:repository"

    @pytest.mark.parametrize("number", (0, -1, True))
    def test_invalid_number_is_rejected_without_calling_gh(self, tmp_path: Path, number: int) -> None:
        seed_state(tmp_path, pull_requests=[_pull()])
        with pytest.raises(TurnError) as stopped:
            _port(tmp_path).advertised_head(repository="o/r", number=number)
        assert stopped.value.stage == "advertised_head:number"

    def test_port_shape_matches_the_protocol(self) -> None:
        assert set(inspect.signature(GitHubAdvertisedHead.advertised_head).parameters) == {
            "self", "repository", "number",
        }
