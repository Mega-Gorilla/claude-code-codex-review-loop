# SPDX-License-Identifier: Apache-2.0
"""既知record probeの受入test（AC-C06-08の材料収集。fake gh使用 = P-011）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from c05_support.helpers import SleepRecorder, make_context, make_policy, seed_state
from c06_support.helpers import chain_comments, seed_dict

from claude_code_codex_review_loop.errors import ErrorCategory
from claude_code_codex_review_loop.identity import (
    ChainCheckpoint,
    KnownRecord,
    ProbeFound,
    ProbeMissing,
    probe_known_records,
)
from claude_code_codex_review_loop.transport import GhApiError, RepoRef

_REPO = RepoRef(owner="octo", name="repo")


class TestProbeKnownRecords:
    def test_present_ids_are_skipped_without_io(self, tmp_path: Path) -> None:
        """取得窓に現れた既知IDはGETしない（scenarioがe2でも失敗しない = 呼ばれていない）。"""
        context = make_context(tmp_path, scenario="e2")
        checkpoint = ChainCheckpoint(high_water_mark=1, known_records=(KnownRecord(1, "1001", "a" * 64),))
        outcomes = probe_known_records(
            context, _REPO, checkpoint, present_comment_ids=frozenset({"1001"}), policy=make_policy()
        )
        assert outcomes == {}

    def test_found_and_missing_are_distinguished(self, tmp_path: Path) -> None:
        comments = chain_comments(2)
        seed_state(tmp_path, comments=[seed_dict(comments[0])])
        context = make_context(tmp_path)
        checkpoint = ChainCheckpoint(
            high_water_mark=2,
            known_records=(
                KnownRecord(1, "1001", comments[0].body_hash),
                KnownRecord(2, "1002", comments[1].body_hash),
            ),
        )
        outcomes = probe_known_records(
            context, _REPO, checkpoint, present_comment_ids=frozenset(), policy=make_policy()
        )
        found = outcomes["1001"]
        assert isinstance(found, ProbeFound)
        assert found.comment.body_hash == comments[0].body_hash
        assert found.comment.comment_id == "1001"
        assert outcomes["1002"] == ProbeMissing(comment_id="1002")

    def test_transient_exhaustion_propagates_not_fabricated(self, tmp_path: Path) -> None:
        """一時障害（5xx）の枯渇はGhApiErrorのまま伝播し、violationを捏造しない。"""
        context = make_context(tmp_path, scenario="s500")
        checkpoint = ChainCheckpoint(high_water_mark=1, known_records=(KnownRecord(1, "1001", "a" * 64),))
        with pytest.raises(GhApiError) as excinfo:
            probe_known_records(
                context,
                _REPO,
                checkpoint,
                present_comment_ids=frozenset(),
                policy=make_policy(max_attempts=2, sleep=SleepRecorder()),
            )
        assert excinfo.value.category is ErrorCategory.TRANSIENT
