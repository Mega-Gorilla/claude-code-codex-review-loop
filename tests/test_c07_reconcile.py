# SPDX-License-Identifier: Apache-2.0
"""head照合と承認失効の受入test（**AC-C07-03**。ADR-0012）。

「外部からheadが更新された場合に旧承認が失効する」を、GitHub由来の値だけで判定できる
ことを固定する。checkpointは変化の分類にしか使わず、分類を誤っても失効した承認が
甦らないことも合わせて検証する。
"""

from __future__ import annotations

import pytest
from c06_support.helpers import HEAD
from c07_support.helpers import approved_review_payload, verified_chain

from claude_code_codex_review_loop.domain.states import State
from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.state import (
    CheckpointHeads,
    HeadChange,
    HeadObservation,
    HeadUnobservable,
    ResumeVerdict,
    collect_approvals,
    observe_head,
    read_checkpoint_heads,
    reconcile_head,
)
from claude_code_codex_review_loop.transport.pull_request import UnverifiedPullRequest

_K = RecordKind
_NEW_HEAD = "b" * 40
_BASE = "c" * 40


def _pull(head: str = HEAD, base: str = _BASE, **overrides: object) -> UnverifiedPullRequest:
    fields: dict[str, object] = {
        "number": 12,
        "state": "open",
        "merged": False,
        "head_sha": head,
        "head_ref": "topic",
        "head_repository": "o/r",
        "base_sha": base,
        "base_ref": "main",
        "base_repository": "o/r",
        "author_login": "alice",
        "updated_at": "2026-08-24T09:00:00Z",
    }
    fields.update(overrides)
    return UnverifiedPullRequest(**fields)  # type: ignore[arg-type]


def _observation(head: str = HEAD, **overrides: object) -> HeadObservation:
    result = observe_head(_pull(head, **overrides))
    assert isinstance(result, HeadObservation)
    return result


def _approved_chain(head: str = HEAD) -> tuple[object, ...]:
    """APPROVEDのreview承認とmerge承認を含む検証済みchainのrecord列。"""
    verification = verified_chain(
        [_K.REVIEW_RESULT, _K.MERGE_APPROVAL],
        head=head,
        payloads={1: approved_review_payload(head)},
    )
    assert verification.is_intact
    return verification.records


class TestObserveHead:
    def test_valid_metadata_becomes_an_observation(self) -> None:
        observation = _observation()
        assert (observation.head_sha, observation.base_sha) == (HEAD, _BASE)
        assert observation.merged is False and observation.pull_state == "open"

    @pytest.mark.parametrize(
        "overrides",
        [{"head_sha": "abc"}, {"head_sha": HEAD.upper()}, {"base_sha": ""}],
        ids=["short_head", "uppercase_head", "empty_base"],
    )
    def test_malformed_sha_stops_judgement(self, overrides: dict[str, object]) -> None:
        """観測が成立しない場合は推測して判定しない。"""
        assert isinstance(observe_head(_pull(**overrides)), HeadUnobservable)


class TestCollectApprovals:
    def test_collects_merge_and_approved_review_only(self) -> None:
        records = verified_chain(
            [_K.REVIEW_RESULT, _K.MERGE_APPROVAL, _K.FIX_RESULT],
            payloads={1: approved_review_payload()},
        ).records
        approvals = collect_approvals(records)
        assert [evidence.kind for evidence in approvals] == [_K.REVIEW_RESULT, _K.MERGE_APPROVAL]
        assert all(evidence.head_sha == HEAD for evidence in approvals)
        assert approvals[0].binding.startswith("cr:")

    def test_changes_requested_is_not_an_approval(self) -> None:
        """representative corpusのREVIEW_RESULTはCHANGES_REQUESTED（承認ではない）。"""
        records = verified_chain([_K.REVIEW_RESULT]).records
        assert collect_approvals(records) == ()


class TestReconcileHead:
    """head変化 × 承認の真理表。"""

    def test_unchanged_head_keeps_approvals(self) -> None:
        result = reconcile_head(
            _observation(),
            records=_approved_chain(),
            heads=CheckpointHeads(observed_sha=HEAD, approved_sha=HEAD),
        )
        assert result.verdict is ResumeVerdict.VALIDATED
        assert result.change is HeadChange.UNCHANGED
        assert len(result.valid_approvals) == 2 and result.voided_approvals == ()

    def test_external_update_voids_approvals(self) -> None:
        """**AC-C07-03**: 外部からheadが更新されると旧承認が失効しfallbackになる。"""
        result = reconcile_head(
            _observation(_NEW_HEAD),
            records=_approved_chain(),
            heads=CheckpointHeads(observed_sha=HEAD, approved_sha=HEAD),
        )
        assert result.verdict is ResumeVerdict.FALLBACK_REQUIRED
        assert result.change is HeadChange.EXTERNAL_UPDATE
        assert len(result.voided_approvals) == 2 and result.valid_approvals == ()

    def test_coder_push_also_voids_approvals(self) -> None:
        """coder自身のpushでも、bind先headが変われば承認は失効する（分類は別問題）。"""
        result = reconcile_head(
            _observation(_NEW_HEAD),
            records=_approved_chain(),
            heads=CheckpointHeads(observed_sha=HEAD, coder_pushed_sha=_NEW_HEAD),
        )
        assert result.verdict is ResumeVerdict.FALLBACK_REQUIRED
        assert result.change is HeadChange.CODER_PUSH
        assert len(result.voided_approvals) == 2

    def test_misclassification_does_not_revive_approvals(self) -> None:
        """checkpointが現在headを『観測済み』と誤って記録していても承認は甦らない。"""
        result = reconcile_head(
            _observation(_NEW_HEAD),
            records=_approved_chain(),
            heads=CheckpointHeads(observed_sha=_NEW_HEAD, approved_sha=_NEW_HEAD),
        )
        assert result.change is HeadChange.UNCHANGED
        assert result.verdict is ResumeVerdict.FALLBACK_REQUIRED
        assert len(result.voided_approvals) == 2

    def test_unknown_change_without_approvals_is_validated(self) -> None:
        """checkpointが無い再開（fresh resume）。失効させる承認が無ければ前進できる。"""
        result = reconcile_head(
            _observation(), records=verified_chain([_K.REVIEW_RESULT]).records, heads=CheckpointHeads()
        )
        assert (result.verdict, result.change) == (ResumeVerdict.VALIDATED, HeadChange.UNKNOWN)

    def test_unknown_change_with_stale_approval_is_fallback(self) -> None:
        result = reconcile_head(
            _observation(_NEW_HEAD), records=_approved_chain(), heads=CheckpointHeads()
        )
        assert (result.verdict, result.change) == (ResumeVerdict.FALLBACK_REQUIRED, HeadChange.UNKNOWN)

    def test_head_change_without_approvals_still_requires_fallback(self) -> None:
        """承認が無くても、継続の前提だったheadが動いていればfresh reviewへ回す。"""
        result = reconcile_head(
            _observation(_NEW_HEAD),
            records=verified_chain([_K.REVIEW_RESULT]).records,
            heads=CheckpointHeads(observed_sha=HEAD),
        )
        assert result.verdict is ResumeVerdict.FALLBACK_REQUIRED
        assert result.approvals == ()

    def test_merge_failed_same_head_is_revalidated(self) -> None:
        result = reconcile_head(
            _observation(),
            records=_approved_chain(),
            heads=CheckpointHeads(observed_sha=HEAD, approved_sha=HEAD),
            checkpoint_state=State.MERGE_FAILED,
        )
        assert result.verdict is ResumeVerdict.SAME_HEAD_VALIDATED
        assert len(result.valid_approvals) == 2

    def test_merge_failed_with_moved_head_is_fallback(self) -> None:
        result = reconcile_head(
            _observation(_NEW_HEAD),
            records=_approved_chain(),
            heads=CheckpointHeads(observed_sha=HEAD, approved_sha=HEAD),
            checkpoint_state=State.MERGE_FAILED,
        )
        assert result.verdict is ResumeVerdict.FALLBACK_REQUIRED

    def test_merge_failed_without_approved_head_is_validated(self) -> None:
        """approved headを記録していなければ『同一head再確認』とは言えない。"""
        result = reconcile_head(
            _observation(),
            records=(),
            heads=CheckpointHeads(observed_sha=HEAD),
            checkpoint_state=State.MERGE_FAILED,
        )
        assert result.verdict is ResumeVerdict.VALIDATED

    def test_observation_facts_travel_with_the_verdict(self) -> None:
        """closed / mergedの事実は判定へ同梱し、消費側（C-10）が見落とせないようにする。"""
        result = reconcile_head(
            _observation(state="closed", merged=True),
            records=(),
            heads=CheckpointHeads(observed_sha=HEAD),
        )
        assert result.observation.merged is True and result.observation.pull_state == "closed"


class TestReadCheckpointHeads:
    def test_reads_heads_and_coder_push(self) -> None:
        heads = read_checkpoint_heads(
            {
                "heads": {"base_sha": _BASE, "observed_sha": HEAD, "approved_sha": HEAD},
                "coder": {"pushed_head_sha": _NEW_HEAD},
            }
        )
        assert heads == CheckpointHeads(
            observed_sha=HEAD, approved_sha=HEAD, base_sha=_BASE, coder_pushed_sha=_NEW_HEAD
        )

    @pytest.mark.parametrize(
        "payload",
        [{}, {"heads": {}}, {"heads": "x", "coder": 1}, {"heads": {"observed_sha": 1}}],
        ids=["absent", "empty", "wrong_types", "non_string_sha"],
    )
    def test_absent_or_unusable_sections_become_none(self, payload: dict[str, object]) -> None:
        assert read_checkpoint_heads(payload) == CheckpointHeads()
