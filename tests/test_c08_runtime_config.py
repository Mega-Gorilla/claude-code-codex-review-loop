# SPDX-License-Identifier: Apache-2.0
"""session configの受入test（Phase 8 PR-3b1。ADR-0020）。

engineは既定値を持たないため、entry pointは全設定を明示で受け取る。ここでは
**足りない設定を補わない**こと（補完はC-12の領域）と、run directoryへ置いたconfigが
**別processから同じportを再構成できる**形であることを固定する。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from c05_support.helpers import make_context
from c07_support.helpers import NUMBER, RUN, state_paths
from c08_support.runtime import session_payload

from claude_code_codex_review_loop.identity.fs_permissions import replace_private_text
from claude_code_codex_review_loop.runtime import (
    ConfigUnavailable,
    SessionConfig,
    config_path,
    read_session_config,
    write_session_config,
)


def _written(tmp_path: Path, **overrides: object):
    paths = state_paths(tmp_path)
    directory = tmp_path / "gh"
    directory.mkdir(parents=True, exist_ok=True)
    write_session_config(paths, RUN, session_payload(directory, **overrides))
    return paths


class TestRead:
    def test_a_written_config_round_trips(self, tmp_path: Path) -> None:
        paths = _written(tmp_path)
        config = read_session_config(paths, RUN)
        assert isinstance(config, SessionConfig)
        assert config.run_id == RUN
        assert config.number == NUMBER
        assert config.repo.owner == "owner"
        assert config.producers.logins == config.producer_logins

    def test_durations_are_stored_as_milliseconds(self, tmp_path: Path) -> None:
        """秒を浮動小数で持つと丸めの差がtimeoutの意味を変える（ADR-0020 決定18）。"""
        paths = _written(tmp_path, gh_timeout_ms=1_500, halt_grace_ms=250)
        config = read_session_config(paths, RUN)
        assert isinstance(config, SessionConfig)
        assert config.gh_timeout_seconds == 1.5
        assert config.halt_grace_seconds == 0.25

    def test_the_gh_context_matches_the_written_values(self, tmp_path: Path) -> None:
        """別processが同じ`GhContext`を組み直せる（cross-process resumeの前提）。"""
        directory = tmp_path / "gh"
        directory.mkdir(parents=True, exist_ok=True)
        expected = make_context(directory)
        paths = state_paths(tmp_path)
        write_session_config(paths, RUN, session_payload(directory))
        config = read_session_config(paths, RUN)
        assert isinstance(config, SessionConfig)
        context = config.context()
        assert context.gh_command == expected.gh_command
        assert context.env == expected.env

    def test_a_missing_config_is_reported_not_defaulted(self, tmp_path: Path) -> None:
        paths = state_paths(tmp_path)
        outcome = read_session_config(paths, RUN)
        assert isinstance(outcome, ConfigUnavailable)
        assert "session.json" in outcome.detail

    def test_a_config_sharing_its_inode_is_refused(self, tmp_path: Path) -> None:
        """設定はrun directoryのprivate fileでなければならない（C-06の権限検証）。

        hard linkでfile実体を共有されると、run directory外のpathから同じ内容を
        書き換えられる。権限検証はこれをlink数で検出する。
        """
        paths = _written(tmp_path)
        path = config_path(paths, RUN)
        os.link(path, tmp_path / "linked.json")
        outcome = read_session_config(paths, RUN)
        assert isinstance(outcome, ConfigUnavailable)
        assert "作成者限定" in outcome.detail

    def test_an_invalid_config_is_refused(self, tmp_path: Path) -> None:
        paths = _written(tmp_path)
        path = config_path(paths, RUN)
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["speaker"]
        replace_private_text(path, json.dumps(payload))
        outcome = read_session_config(paths, RUN)
        assert isinstance(outcome, ConfigUnavailable)
        assert "required_missing" in outcome.detail

    def test_a_config_for_another_run_is_refused(self, tmp_path: Path) -> None:
        """別runの設定で走らせるとcheckpointとportの指す先がずれる。"""
        paths = _written(tmp_path)
        path = config_path(paths, RUN)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["run_id"] = "run-2"
        replace_private_text(path, json.dumps(payload))
        outcome = read_session_config(paths, RUN)
        assert isinstance(outcome, ConfigUnavailable)
        assert "run ID" in outcome.detail


class TestWrite:
    def test_an_invalid_payload_is_not_written(self, tmp_path: Path) -> None:
        """検証を通らない設定はfileにしない（読み手が壊れた設定を見ない）。"""
        paths = state_paths(tmp_path)
        directory = tmp_path / "gh"
        directory.mkdir(parents=True, exist_ok=True)
        payload = session_payload(directory)
        del payload["gh_command"]
        with pytest.raises(ValueError, match="required_missing"):
            write_session_config(paths, RUN, payload)
        assert not config_path(paths, RUN).exists()
