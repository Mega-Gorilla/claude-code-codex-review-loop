# SPDX-License-Identifier: Apache-2.0
"""PR metadata取得の受入test（AC-C05-05の維持。ADR-0012）。

resume（C-07）がadvertised headを観測するためのread primitive。取得値を加工せず、
未検証metadataとして返すことを固定する。fork元repositoryが取得できない場合は
空文字列（C-04がforkとして扱う入力）になる。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from c05_support.helpers import make_context, make_policy, seed_state

from claude_code_codex_review_loop.errors import ErrorCategory
from claude_code_codex_review_loop.transport import (
    GhApiError,
    RepoRef,
    TransportError,
    get_pull_request,
    pull_request_from_json,
)

_REPO = RepoRef(owner="o", name="r")
_HEAD = "a" * 40
_BASE = "b" * 40


def _pull(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "number": 12,
        "state": "open",
        "merged": False,
        "updated_at": "2026-08-24T09:00:00Z",
        "user": {"login": "alice"},
        "head": {"sha": _HEAD, "ref": "topic", "repo": {"full_name": "o/r"}},
        "base": {"sha": _BASE, "ref": "main", "repo": {"full_name": "o/r"}},
    }
    payload.update(overrides)
    return payload


class TestGetPullRequest:
    def test_returns_advertised_metadata(self, tmp_path: Path) -> None:
        seed_state(tmp_path, pull_requests=[_pull()])
        context = make_context(tmp_path)
        pull = get_pull_request(context, _REPO, 12, policy=make_policy())
        assert (pull.number, pull.state, pull.merged) == (12, "open", False)
        assert (pull.head_sha, pull.head_ref, pull.head_repository) == (_HEAD, "topic", "o/r")
        assert (pull.base_sha, pull.base_ref, pull.base_repository) == (_BASE, "main", "o/r")
        assert (pull.author_login, pull.updated_at) == ("alice", "2026-08-24T09:00:00Z")

    def test_missing_pull_request_is_reported(self, tmp_path: Path) -> None:
        seed_state(tmp_path, pull_requests=[])
        context = make_context(tmp_path)
        with pytest.raises(GhApiError) as excinfo:
            get_pull_request(context, _REPO, 12, policy=make_policy())
        assert excinfo.value.category is ErrorCategory.NOT_FOUND

    def test_transient_failure_is_retried(self, tmp_path: Path) -> None:
        seed_state(tmp_path, pull_requests=[_pull()])
        context = make_context(tmp_path, scenario="s500,ok")
        pull = get_pull_request(context, _REPO, 12, policy=make_policy(backoff_seconds=0.0))
        assert pull.head_sha == _HEAD


class TestParsePullRequest:
    """未検証metadataとしての写し取り（加工しない・欠落は構造errorにする）。"""

    def test_absent_head_repository_becomes_empty(self) -> None:
        """fork元削除等でrepoがnullの場合は空文字列（C-04がforkとして扱う）。"""
        pull = pull_request_from_json(_pull(head={"sha": _HEAD, "ref": "topic", "repo": None}))
        assert pull.head_repository == ""
        assert pull.head_sha == _HEAD

    def test_absent_user_is_preserved_as_none(self) -> None:
        """削除済みaccountはnullのまま保持する（actor判定はC-06。AC-C05-05）。"""
        assert pull_request_from_json(_pull(user=None)).author_login is None

    @pytest.mark.parametrize(
        "payload",
        [
            "not-an-object",
            _pull(number="12"),
            _pull(number=True),
            _pull(merged="false"),
            _pull(state=None),
            _pull(updated_at=None),
            _pull(user="alice"),
            _pull(user={"login": 1}),
            _pull(head=None),
            _pull(head={"sha": _HEAD, "ref": "topic", "repo": "o/r"}),
            _pull(head={"sha": _HEAD, "ref": "topic", "repo": {"full_name": 1}}),
            _pull(head={"sha": None, "ref": "topic", "repo": None}),
            _pull(base={"sha": _BASE, "repo": None}),
        ],
        ids=[
            "not_object", "number_str", "number_bool", "merged_str", "state_null", "updated_null",
            "user_str", "login_int", "head_null", "head_repo_str", "full_name_int", "sha_null",
            "base_ref_missing",
        ],
    )
    def test_malformed_response_is_rejected(self, payload: object) -> None:
        with pytest.raises(TransportError) as excinfo:
            pull_request_from_json(payload)
        assert excinfo.value.category is ErrorCategory.PERMANENT
