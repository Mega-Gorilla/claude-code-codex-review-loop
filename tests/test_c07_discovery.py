# SPDX-License-Identifier: Apache-2.0
"""run候補の列挙と決定論的選択の受入test（ADR-0012）。

- 列挙はlocal（state root配下）とGitHub（markerが名乗るrun）の両側から行う
- 選択は**検証済み**観測だけを見て、非terminalな候補がちょうど1つの場合に限る
- 0件・2件以上・checkpointが読めない場合は推測せず停止する
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from c06_support.helpers import PRODUCER, chain_comments, make_comment
from c07_support.helpers import (
    NUMBER,
    REPOSITORY,
    RUN,
    checkpoint_payload,
    state_paths,
    verified_chain,
)

from claude_code_codex_review_loop.domain.states import State
from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.identity.fs_permissions import create_private_dir, write_private_text
from claude_code_codex_review_loop.state import (
    RunAmbiguous,
    RunDiscoveryError,
    RunNotFound,
    RunSelected,
    RunSummary,
    RunUnavailable,
    StatePaths,
    checkpoint_path,
    discover_local_runs,
    discovery,
    enumerate_github_runs,
    enumerate_run_candidates,
    run_directory,
    save_checkpoint,
    select_run,
)
from claude_code_codex_review_loop.state.store import CheckpointLoaded, CheckpointMissing, load_checkpoint

_K = RecordKind


def _seed_run(paths: StatePaths, run_id: str, **overrides: object) -> Path:
    """run directoryとcheckpointを用意する（製品の保存経路を通す）。"""
    run_directory(paths, run_id)
    path = checkpoint_path(paths, run_id)
    save_checkpoint(path, checkpoint_payload(run_id=run_id, **overrides))
    return path


def _loaded(paths: StatePaths, run_id: str) -> CheckpointLoaded:
    result = load_checkpoint(checkpoint_path(paths, run_id))
    assert isinstance(result, CheckpointLoaded)
    return result


class TestLocalDiscovery:
    def test_lists_runs_with_load_results(self, tmp_path: Path) -> None:
        paths = state_paths(tmp_path)
        _seed_run(paths, "run-b")
        _seed_run(paths, "run-a")
        runs = discover_local_runs(paths)
        assert [run.run_id for run in runs] == ["run-a", "run-b"]
        assert all(isinstance(run.result, CheckpointLoaded) for run in runs)

    def test_run_without_checkpoint_is_reported_as_missing(self, tmp_path: Path) -> None:
        """directoryだけがある状態も候補として列挙し、結果はMissingで表す。"""
        paths = state_paths(tmp_path)
        run_directory(paths, RUN)
        runs = discover_local_runs(paths)
        assert len(runs) == 1 and isinstance(runs[0].result, CheckpointMissing)

    def test_invalid_entries_are_not_runs(self, tmp_path: Path) -> None:
        """run IDとして不正な名前とfileはrunと解釈しない。"""
        paths = state_paths(tmp_path)
        _seed_run(paths, RUN)
        create_private_dir(paths.runs_dir / "-leading-dash")
        write_private_text(paths.runs_dir / "stray.json", "{}")
        assert [run.run_id for run in discover_local_runs(paths)] == [RUN]

    def test_symlinked_entry_is_not_a_run(self, tmp_path: Path) -> None:
        """解決先が`runs/`の外になるentryはrunと解釈しない（containment）。"""
        paths = state_paths(tmp_path)
        _seed_run(paths, RUN)
        outside = tmp_path.resolve() / "outside"
        create_private_dir(outside)
        write_private_text(outside / "checkpoint.json", json.dumps(checkpoint_payload()))
        try:
            (paths.runs_dir / "run-linked").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - symlink不可環境
            pytest.skip("symlinkを作成できない環境")
        assert [run.run_id for run in discover_local_runs(paths)] == [RUN]

    @pytest.mark.parametrize(
        "entry, inside",
        [("runs/run-1", True), ("elsewhere/run-1", False)],
        ids=["inside", "outside"],
    )
    def test_containment_predicate(self, tmp_path: Path, entry: str, inside: bool) -> None:
        """`runs/`直下の実体だけをrunと認める判定（symlinkが作れない環境でも固定する）。"""
        runs_dir = tmp_path.resolve() / "runs"
        assert discovery._is_inside_runs(tmp_path.resolve() / entry, runs_dir) is inside

    def test_broken_checkpoint_is_not_reduced_to_missing(self, tmp_path: Path) -> None:
        """壊れたcheckpointを「無い」に丸めない（silent repair禁止）。"""
        paths = state_paths(tmp_path)
        run_directory(paths, RUN)
        write_private_text(checkpoint_path(paths, RUN), "{ not json")
        runs = discover_local_runs(paths)
        assert len(runs) == 1 and type(runs[0].result).__name__ == "CheckpointSchemaInvalid"


class TestGithubDiscovery:
    def test_groups_markers_by_run(self) -> None:
        comments = chain_comments(2, run_id="run-a") + chain_comments(1, run_id="run-b", start_id=1101)
        runs, unattributable = enumerate_github_runs(comments)
        assert [(run.run_id, run.record_count, run.max_seq) for run in runs] == [
            ("run-a", 2, 2),
            ("run-b", 1, 1),
        ]
        assert unattributable == 0

    def test_unparsable_markers_are_counted_not_dropped(self) -> None:
        """解析できないmarkerはrunへ帰属できないが、件数を残す（曖昧さの根拠）。"""
        broken = make_comment(9001, "本文\n<!-- CC_REVIEW_META:1 {not json} -->")
        runs, unattributable = enumerate_github_runs(chain_comments(1) + (broken,))
        assert [run.run_id for run in runs] == [RUN]
        assert unattributable == 1

    def test_comments_without_marker_are_ignored(self) -> None:
        runs, unattributable = enumerate_github_runs((make_comment(9002, "ただのcomment"),))
        assert runs == () and unattributable == 0


class TestCandidateSet:
    def test_union_of_local_and_github(self, tmp_path: Path) -> None:
        paths = state_paths(tmp_path)
        _seed_run(paths, "run-local")
        candidates = enumerate_run_candidates(
            paths, chain_comments(1, run_id="run-remote"), repository=REPOSITORY, number=NUMBER
        )
        assert candidates.run_ids == ("run-local", "run-remote")
        assert candidates.unrelated_local == ()

    def test_other_target_local_run_is_not_a_candidate(self, tmp_path: Path) -> None:
        """別repository / 別番号のrunは候補にしない（無関係なresumeを止めない）。"""
        paths = state_paths(tmp_path)
        _seed_run(paths, "run-other", repository="other/repo")
        candidates = enumerate_run_candidates(paths, (), repository=REPOSITORY, number=NUMBER)
        assert candidates.run_ids == () and candidates.unrelated_local == ("run-other",)

    def test_unreadable_local_run_becomes_candidate_only_via_github(self, tmp_path: Path) -> None:
        """読めないcheckpointは対象を名乗れないため、GitHub側に現れた場合だけ候補になる。"""
        paths = state_paths(tmp_path)
        run_directory(paths, RUN)
        write_private_text(checkpoint_path(paths, RUN), "{ not json")
        assert enumerate_run_candidates(paths, (), repository=REPOSITORY, number=NUMBER).run_ids == ()
        with_github = enumerate_run_candidates(
            paths, chain_comments(1, run_id=RUN), repository=REPOSITORY, number=NUMBER
        )
        assert with_github.run_ids == (RUN,)


class TestSelectRun:
    def test_single_resumable_run_is_selected(self, tmp_path: Path) -> None:
        paths = state_paths(tmp_path)
        _seed_run(paths, RUN)
        summary = RunSummary(
            run_id=RUN, verification=verified_chain([_K.REVIEW_RESULT]), checkpoint=_loaded(paths, RUN)
        )
        result = select_run((summary,))
        assert isinstance(result, RunSelected)
        assert (result.status.run_id, result.status.terminal, result.status.max_seq) == (RUN, False, 1)
        assert result.status.intact is True

    def test_multiple_resumable_runs_stop(self) -> None:
        """非terminal候補が複数なら推測せず停止し、候補を提示する。"""
        summaries = (
            RunSummary(run_id="run-a", verification=verified_chain([_K.REVIEW_RESULT])),
            RunSummary(run_id="run-b", verification=verified_chain([_K.REVIEW_RESULT], run_id="run-b")),
        )
        result = select_run(summaries)
        assert isinstance(result, RunAmbiguous)
        assert [status.run_id for status in result.candidates] == ["run-a", "run-b"]

    def test_no_candidate_is_reported(self) -> None:
        result = select_run(())
        assert isinstance(result, RunNotFound) and "観測されていない" in result.detail

    @pytest.mark.parametrize(
        "kinds",
        [[_K.REVIEW_RESULT, _K.FINAL_REPORT], [_K.USER_CANCEL, _K.REVIEW_RESULT]],
        ids=["final_report_last", "user_cancel_present"],
    )
    def test_github_terminal_signals_exclude_the_run(self, kinds: list[RecordKind]) -> None:
        result = select_run((RunSummary(run_id=RUN, verification=verified_chain(kinds)),))
        assert isinstance(result, RunNotFound)
        assert result.considered[0].terminal is True

    def test_checkpoint_terminal_state_excludes_the_run(self, tmp_path: Path) -> None:
        paths = state_paths(tmp_path)
        _seed_run(paths, RUN, state={"state": State.MERGED.value})
        result = select_run((RunSummary(run_id=RUN, checkpoint=_loaded(paths, RUN)),))
        assert isinstance(result, RunNotFound)
        assert result.considered[0].terminal_reason is not None
        assert "MERGED" in result.considered[0].terminal_reason

    def test_violated_chain_is_not_declared_terminal(self) -> None:
        """violationのあるchainのsignalを終端判定に使わない（壊れた系列で候補を消さない）。"""
        verification = verified_chain([_K.REVIEW_RESULT, _K.FINAL_REPORT], author="attacker")
        assert verification.is_intact is False
        result = select_run((RunSummary(run_id=RUN, verification=verification),))
        assert isinstance(result, RunSelected)
        assert result.status.terminal is False and result.status.intact is False

    def test_unreadable_checkpoint_stops_selection(self, tmp_path: Path) -> None:
        """checkpointが読めない候補はfresh resumeへ迂回せず停止する。"""
        paths = state_paths(tmp_path)
        run_directory(paths, RUN)
        write_private_text(checkpoint_path(paths, RUN), "{ not json")
        summary = RunSummary(run_id=RUN, checkpoint=load_checkpoint(checkpoint_path(paths, RUN)))
        result = select_run((summary,))
        assert isinstance(result, RunUnavailable) and result.run_id == RUN

    def test_missing_checkpoint_is_not_an_obstacle(self, tmp_path: Path) -> None:
        """checkpoint消失（fresh resume）はviolationではない。"""
        paths = state_paths(tmp_path)
        summary = RunSummary(
            run_id=RUN,
            verification=verified_chain([_K.REVIEW_RESULT]),
            checkpoint=load_checkpoint(checkpoint_path(paths, RUN)),
        )
        assert isinstance(select_run((summary,)), RunSelected)

    def test_summary_requires_a_source(self) -> None:
        with pytest.raises(RunDiscoveryError):
            RunSummary(run_id=RUN)

    def test_checkpoint_without_state_section_has_no_state(self, tmp_path: Path) -> None:
        paths = state_paths(tmp_path)
        path = checkpoint_path(paths, RUN)
        run_directory(paths, RUN)
        write_private_text(path, json.dumps(checkpoint_payload(state={"round": 1})))
        result = select_run((RunSummary(run_id=RUN, checkpoint=load_checkpoint(path)),))
        assert isinstance(result, RunSelected) and result.status.checkpoint_state is None


def test_producer_is_the_chain_author() -> None:
    """helperが製品規約どおりのproducerでchainを作っていることの明示。"""
    assert verified_chain([_K.REVIEW_RESULT]).records[0].author_login == PRODUCER
