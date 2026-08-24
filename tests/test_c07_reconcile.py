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

from claude_code_codex_review_loop.domain import TransitionRejected, transition
from claude_code_codex_review_loop.domain import events as ev
from claude_code_codex_review_loop.domain.states import State
from claude_code_codex_review_loop.domain.values import MachineState, RecordKind
from claude_code_codex_review_loop.identity.record_chain import VerifiedRecord
from claude_code_codex_review_loop.state import (
    ApprovalEvidence,
    ApprovalState,
    CheckpointHeads,
    HeadChange,
    HeadObservation,
    HeadReconciliation,
    HeadUnobservable,
    ReconciliationStop,
    ReconciliationStopped,
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


def _approved_chain(head: str = HEAD) -> tuple[VerifiedRecord, ...]:
    """APPROVEDのreview承認とmerge承認を含む検証済みchainのrecord列。"""
    verification = verified_chain(
        [_K.REVIEW_RESULT, _K.MERGE_APPROVAL],
        head=head,
        payloads={1: approved_review_payload(head)},
    )
    assert verification.is_intact
    return verification.records


def _reapproved_chain(old_head: str, new_head: str) -> tuple[VerifiedRecord, ...]:
    """旧headの承認の後、現headで再承認したintact chain（append-onlyの履歴）。"""
    verification = verified_chain(
        [_K.REVIEW_RESULT, _K.REVIEW_RESULT],
        head=old_head,
        payloads={1: approved_review_payload(old_head), 2: approved_review_payload(old_head)},
    )
    assert verification.is_intact
    older = verification.records[0]
    newer = verified_chain(
        [_K.REVIEW_RESULT], head=new_head, payloads={1: approved_review_payload(new_head)}
    ).records[0]
    return (older, newer)


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


def _judged(**kwargs: object) -> HeadReconciliation:
    """判定（停止でないこと）を取り出す。"""
    result = reconcile_head(**kwargs)  # type: ignore[arg-type]
    assert isinstance(result, HeadReconciliation)
    return result


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

    def test_merge_failed_without_canonical_approval_stops(self) -> None:
        """**GitHub上の承認recordが無ければmerge gateへ復帰しない**（local cacheだけで戻らない）。

        verdictでは表さない: bareな`MERGE_FAILED`に`ResumeValidated`を受理するruleがC-01に
        無いため、判定として返すと消費側が合法な遷移へ写せない。
        """
        result = reconcile_head(
            _observation(),
            records=(),
            heads=CheckpointHeads(observed_sha=HEAD, approved_sha=HEAD),
            checkpoint_state=State.MERGE_FAILED,
        )
        assert isinstance(result, ReconciliationStopped)
        assert result.reason is ReconciliationStop.MERGE_APPROVAL_UNCONFIRMED
        assert result.missing_approvals == (_K.MERGE_APPROVAL, _K.REVIEW_RESULT)

    @pytest.mark.parametrize(
        "kinds, missing",
        [([_K.MERGE_APPROVAL], _K.REVIEW_RESULT), ([_K.REVIEW_RESULT], _K.MERGE_APPROVAL)],
        ids=["review_missing", "merge_missing"],
    )
    def test_merge_failed_requires_both_approvals(
        self, kinds: list[RecordKind], missing: RecordKind
    ) -> None:
        """merge gateへの復帰は merge承認 と review承認 の両方を現headで確認できる場合だけ。"""
        payloads = {1: approved_review_payload()} if kinds == [_K.REVIEW_RESULT] else None
        result = reconcile_head(
            _observation(),
            records=verified_chain(kinds, payloads=payloads).records,
            heads=CheckpointHeads(observed_sha=HEAD, approved_sha=HEAD),
            checkpoint_state=State.MERGE_FAILED,
        )
        assert isinstance(result, ReconciliationStopped)
        assert result.missing_approvals == (missing,)
        assert missing.value in result.detail

    def test_merge_failed_accepts_external_merge_approval(self) -> None:
        """GitHub直接commentで受理した承認（D-021）もmerge gate復帰の根拠にできる。"""
        external = ApprovalEvidence(
            kind=_K.MERGE_APPROVAL,
            binding="ud:merge:1234",
            head_sha=HEAD,
            comment_id="1234",
            detail="merge",
        )
        result = reconcile_head(
            _observation(),
            records=verified_chain([_K.REVIEW_RESULT], payloads={1: approved_review_payload()}).records,
            heads=CheckpointHeads(observed_sha=HEAD, approved_sha=HEAD),
            checkpoint_state=State.MERGE_FAILED,
            external_approvals=(external,),
        )
        assert result.verdict is ResumeVerdict.SAME_HEAD_VALIDATED

    def test_reapproved_head_does_not_fall_back_forever(self) -> None:
        """旧headの承認は履歴に残るが、現headで再承認済みなら判定へ影響させない。"""
        result = reconcile_head(
            _observation(_NEW_HEAD),
            records=_reapproved_chain(HEAD, _NEW_HEAD),
            heads=CheckpointHeads(observed_sha=_NEW_HEAD, approved_sha=_NEW_HEAD),
        )
        assert result.verdict is ResumeVerdict.VALIDATED
        assert len(result.valid_approvals) == 1 and len(result.superseded_approvals) == 1
        assert result.voided_approvals == ()
        superseded = [s for s in result.approvals if s.state is ApprovalState.SUPERSEDED]
        assert superseded[0].reason is not None

    def test_old_approval_without_reapproval_is_voided(self) -> None:
        """同種の現head承認が無ければ、旧承認は失効のまま（再承認を偽装できない）。"""
        result = reconcile_head(
            _observation(_NEW_HEAD),
            records=_approved_chain(),
            heads=CheckpointHeads(observed_sha=_NEW_HEAD),
        )
        assert result.verdict is ResumeVerdict.FALLBACK_REQUIRED
        assert len(result.voided_approvals) == 2 and result.superseded_approvals == ()

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

class TestMergeFailedOutcomesAreConsumable:
    """`MERGE_FAILED`で返る結果が、C-01で受理される遷移か停止のどちらかへ到達すること。

    verdictはeventそのものではないため、消費側（C-10）が合法な遷移へ写せることを
    componentを跨いで固定する。
    """

    def _machine(self) -> MachineState:
        """merge失敗直後のbareなMachineState（pending / awaitingなし）。"""
        return MachineState(state=State.MERGE_FAILED)

    def test_same_head_validated_returns_to_merge_gate(self) -> None:
        result = _judged(
            observation=_observation(),
            records=_approved_chain(),
            heads=CheckpointHeads(observed_sha=HEAD, approved_sha=HEAD),
            checkpoint_state=State.MERGE_FAILED,
        )
        assert result.verdict is ResumeVerdict.SAME_HEAD_VALIDATED
        after, commands = transition(self._machine(), ev.ResumeSameHeadValidated())
        assert after.state is State.READY_FOR_HUMAN_MERGE and commands == ()

    def test_head_change_maps_to_external_head_change(self) -> None:
        result = _judged(
            observation=_observation(_NEW_HEAD),
            records=_approved_chain(),
            heads=CheckpointHeads(observed_sha=HEAD, approved_sha=HEAD),
            checkpoint_state=State.MERGE_FAILED,
        )
        assert result.verdict is ResumeVerdict.FALLBACK_REQUIRED
        after, commands = transition(self._machine(), ev.HeadChangedExternally())
        assert after.state is State.RUNNING_REVIEW
        assert [type(command).__name__ for command in commands] == [
            "InvalidateApprovals",
            "RequestCodexReview",
        ]

    def test_stop_is_returned_because_no_resume_event_fits(self) -> None:
        """承認不足はverdictにしない: C-01が受理するのはM-SHだけで、その根拠が無い。"""
        result = reconcile_head(
            _observation(),
            records=(),
            heads=CheckpointHeads(observed_sha=HEAD, approved_sha=HEAD),
            checkpoint_state=State.MERGE_FAILED,
        )
        assert isinstance(result, ReconciliationStopped)
        machine = self._machine()
        for event in (ev.ResumeValidated(), ev.ResumeFallbackRequired()):
            with pytest.raises(TransitionRejected):
                transition(machine, event)
        # M-SH自体は承認を検査しないため受理される。発行してよい根拠を持つのはC-07側
        after, _ = transition(machine, ev.ResumeSameHeadValidated())
        assert after.state is State.READY_FOR_HUMAN_MERGE
