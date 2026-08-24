# SPDX-License-Identifier: Apache-2.0
"""crash matrixの受入test（**AC-C07-01 / 02 / 06**。ADR-0013）。

中断位置4種（投稿前 / 投稿後・確認前 / 確認後・checkpoint前 / checkpoint後）で
`observe_resume` -> `build_resume_context`を通し、**GitHub上の当該recordが常に1件**である
ことを固定する。process境界のfake gh越しに製品経路だけで検証し、実GitHubへは接続しない。

C-07自身は投稿経路を持たない（ADR-0013）。位置1では、返ったdirectiveをC-05の
`ensure_comment_posted`（C-08が使う製品関数）へ渡して投稿し、再resumeが「投稿済み」に
なることまで確認する。
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
from c05_support.helpers import make_context, make_policy, read_state, seed_state
from c06_support.helpers import PRODUCER, make_comment, seed_dict
from c07_support.helpers import (
    NUMBER,
    REPOSITORY,
    RUN,
    chain_comments_of,
    checkpoint_payload,
    conversation_section,
    pending_fixture,
    state_paths,
)

from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.identity import DecisionAllowlist, DecisionContext, ProducerAllowlist
from claude_code_codex_review_loop.identity.fs_permissions import write_private_text
from claude_code_codex_review_loop.state import (
    ArtifactStatus,
    DirectAnswerAccepted,
    PendingAbsent,
    PendingAlreadyPosted,
    PendingReissueRequired,
    ResumeContext,
    StatePaths,
    build_resume_context,
    checkpoint_path,
    observe_resume,
    run_directory,
    save_checkpoint,
)
from claude_code_codex_review_loop.transport import (
    PostRoute,
    PostVerified,
    RepoRef,
    body_hash_of,
    ensure_comment_posted,
)
from claude_code_codex_review_loop.transport.conversation import UnverifiedComment

_K = RecordKind
_REPO = RepoRef(owner="owner", name="repo")
_HEAD = "a" * 40
_PRODUCERS = ProducerAllowlist(logins=frozenset({PRODUCER}))
_ALLOWLIST = DecisionAllowlist(logins=frozenset({"Mega-Gorilla"}))
_DECISION_CONTEXT = DecisionContext(
    kind=RecordKind.MERGE_APPROVAL,
    repository=REPOSITORY,
    number=NUMBER,
    head_sha=_HEAD,
    merge_method="merge",
)
# 投稿済みの先行record（seq=1）と、中断したclarification質問（seq=2）
_ARTIFACT = "reviewer payload cache"
_POSTED = chain_comments_of([_K.REVIEW_RESULT])
_PENDING = pending_fixture(seq=2, prev=body_hash_of(_POSTED[0].body))


def _pull_json() -> dict[str, object]:
    return {
        "number": NUMBER,
        "state": "open",
        "merged": False,
        "updated_at": "2026-08-24T09:00:00Z",
        "user": {"login": "alice"},
        "head": {"sha": _HEAD, "ref": "topic", "repo": {"full_name": REPOSITORY}},
        "base": {"sha": "c" * 40, "ref": "main", "repo": {"full_name": REPOSITORY}},
    }


def _seed_github(directory: Path, posted: tuple[UnverifiedComment, ...]) -> None:
    seed_state(
        directory,
        comments=[seed_dict(comment, issue=NUMBER) for comment in posted],
        pull_requests=[_pull_json()],
    )


def _seed_checkpoint(
    paths: StatePaths,
    *,
    transaction: bool,
    confirmed: tuple[UnverifiedComment, ...],
    verified: bool = False,
    artifact_records: list[dict[str, object]] | None = None,
) -> None:
    sections: dict[str, object] = {"conversation": conversation_section(confirmed)}
    if artifact_records is not None:
        sections["artifact_records"] = artifact_records
    if transaction:
        sections["transaction"] = _PENDING.transaction
        sections["mutation"] = {"read_after_write_verified": verified}
    run_directory(paths, RUN)
    save_checkpoint(checkpoint_path(paths, RUN), checkpoint_payload(**sections))


def _resume(directory: Path, paths: StatePaths, *, answers: bool = False) -> ResumeContext:
    observation = observe_resume(
        make_context(directory),
        _REPO,
        NUMBER,
        paths=paths,
        producers=_PRODUCERS,
        policy=make_policy(),
        max_pages=5,
        decision_context=_DECISION_CONTEXT if answers else None,
        decision_allowlist=_ALLOWLIST if answers else None,
    )
    context = build_resume_context(observation)
    assert isinstance(context, ResumeContext), context
    return context


def _comments(directory: Path) -> list[dict[str, object]]:
    comments = read_state(directory).get("comments", [])
    assert isinstance(comments, list)
    return comments


def _records_with_pending(directory: Path) -> list[dict[str, object]]:
    """fake gh上で、中断recordのidempotency keyを持つcommentを数える。"""
    comments = _comments(directory)
    return [
        comment
        for comment in comments
        if isinstance(comment, dict) and _PENDING.binding in str(comment.get("body", ""))
    ]


class TestCrashMatrix:
    """中断位置4種。いずれも当該recordはGitHub上で1件に収束する。"""

    @pytest.mark.parametrize(
        "posted_count, transaction, verified, expected",
        [
            (1, True, False, "reissue"),
            (2, True, False, "posted"),
            (2, True, True, "posted"),
            (2, False, False, "absent"),
        ],
        ids=["before_post", "after_post_before_verify", "after_verify_before_checkpoint", "after_checkpoint"],
    )
    def test_pending_outcome_per_position(
        self, tmp_path: Path, posted_count: int, transaction: bool, verified: bool, expected: str
    ) -> None:
        posted = _POSTED + (_PENDING.comment,) if posted_count == 2 else _POSTED
        confirmed = posted if not transaction else _POSTED
        paths = state_paths(tmp_path)
        _seed_github(tmp_path, posted)
        _seed_checkpoint(paths, transaction=transaction, confirmed=confirmed, verified=verified)

        context = _resume(tmp_path, paths)

        assert len(_records_with_pending(tmp_path)) == (0 if posted_count == 1 else 1)
        if expected == "reissue":
            assert isinstance(context.pending, PendingReissueRequired)
            assert context.pending.body == _PENDING.body  # 中断前の完成形とbyte一致
        elif expected == "posted":
            assert isinstance(context.pending, PendingAlreadyPosted)
            assert context.pending.record.key == _PENDING.binding
        else:
            assert isinstance(context.pending, PendingAbsent)
        assert context.run_id == RUN and context.next_seq == posted_count + 1

    def test_direct_answer_is_fetched_without_posting(self, tmp_path: Path) -> None:
        """**AC-C07-06**: 直接回答を取得しても、その取得自体は投稿のtriggerにならない。"""
        paths = state_paths(tmp_path)
        answer = make_comment(
            3001,
            "approve",
            author="mega-gorilla",
            created_at="2026-08-24T12:00:00Z",
            repository=REPOSITORY,
            number=NUMBER,
        )
        _seed_github(tmp_path, (*_POSTED, answer))
        _seed_checkpoint(paths, transaction=True, confirmed=_POSTED)
        before = len(_comments(tmp_path))

        context = _resume(tmp_path, paths, answers=True)
        assert isinstance(context.direct_answer, DirectAnswerAccepted)
        assert context.direct_answer.decision.comment_id == "3001"
        # 中断recordのdirectiveは返るが、resume自体は投稿しない
        assert isinstance(context.pending, PendingReissueRequired)
        assert len(_comments(tmp_path)) == before
        assert _records_with_pending(tmp_path) == []

    def test_artifact_bound_to_the_posted_record_is_usable(self, tmp_path: Path) -> None:
        """**AC-C07-05**: run directory配下のartifactが、投稿済みrecordとheadへbindされる。"""
        paths = state_paths(tmp_path)
        directory = run_directory(paths, RUN)
        write_private_text(directory / "review.json", _ARTIFACT)
        marker = _POSTED[0].marker
        assert marker is not None and marker.payload is not None
        _seed_github(tmp_path, _POSTED)
        _seed_checkpoint(
            paths,
            transaction=False,
            confirmed=_POSTED,
            artifact_records=[
                {
                    "path": "review.json",
                    "kind": "REVIEW_RESULT",
                    "content_hash": sha256(_ARTIFACT.encode("utf-8")).hexdigest(),
                    "approved_head_sha": _HEAD,
                    "record_binding": str(marker.payload["key"]),
                    "comment_id": _POSTED[0].comment_id,
                }
            ],
        )
        context = _resume(tmp_path, paths)
        assert [check.status for check in context.artifacts] == [ArtifactStatus.BOUND]
        assert len(context.usable_artifacts) == 1

    def test_resume_itself_never_posts(self, tmp_path: Path) -> None:
        """resumeはいかなる段階でもGitHubへ投稿しない（C-07は投稿経路を持たない）。"""
        paths = state_paths(tmp_path)
        _seed_github(tmp_path, _POSTED)
        _seed_checkpoint(paths, transaction=True, confirmed=_POSTED)
        before = len(_comments(tmp_path))
        _resume(tmp_path, paths)
        _resume(tmp_path, paths)
        assert len(_comments(tmp_path)) == before

    def test_directive_posts_exactly_once_and_then_resolves(self, tmp_path: Path) -> None:
        """**AC-C07-02**: 中断した質問がdirective経由で1件だけ投稿され、再resumeで投稿済みになる。"""
        paths = state_paths(tmp_path)
        _seed_github(tmp_path, _POSTED)
        _seed_checkpoint(paths, transaction=True, confirmed=_POSTED)

        context = _resume(tmp_path, paths)
        assert isinstance(context.pending, PendingReissueRequired)
        outcome = ensure_comment_posted(
            make_context(tmp_path),
            _REPO,
            NUMBER,
            context.pending.body,
            search_since=None,
            search_attempts=2,
            search_backoff_seconds=0.0,
            search_max_pages=5,
            policy=make_policy(),
        )
        assert isinstance(outcome, PostVerified) and outcome.route is PostRoute.POSTED
        assert len(_records_with_pending(tmp_path)) == 1

        resumed = _resume(tmp_path, paths)
        assert isinstance(resumed.pending, PendingAlreadyPosted)
        assert resumed.next_seq == 3

    def test_repeated_directive_does_not_duplicate(self, tmp_path: Path) -> None:
        """同一directiveを再投稿しても、search-firstで既存が見つかり1件のまま。"""
        paths = state_paths(tmp_path)
        _seed_github(tmp_path, _POSTED)
        _seed_checkpoint(paths, transaction=True, confirmed=_POSTED)
        context = _resume(tmp_path, paths)
        assert isinstance(context.pending, PendingReissueRequired)
        for expected_route in (PostRoute.POSTED, PostRoute.FOUND_EXISTING):
            outcome = ensure_comment_posted(
                make_context(tmp_path),
                _REPO,
                NUMBER,
                context.pending.body,
                search_since=None,
                search_attempts=2,
                search_backoff_seconds=0.0,
                search_max_pages=5,
                policy=make_policy(),
            )
            assert isinstance(outcome, PostVerified) and outcome.route is expected_route
        assert len(_records_with_pending(tmp_path)) == 1
