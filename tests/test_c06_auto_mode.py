# SPDX-License-Identifier: Apache-2.0
"""Auto mode検出の受入test（AC-C06-10）。実CLIへ依存せずfake CLIで検証する（P-011）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from c03_support.helpers import child_env
from c06_support.helpers import write_fake_claude

from claude_code_codex_review_loop.identity import (
    AutoModeProbe,
    IdentityError,
    detect_auto_mode,
    probe_auto_mode,
)
from claude_code_codex_review_loop.identity import auto_mode as auto_mode_module
from claude_code_codex_review_loop.identity.auto_mode import MAX_PROBE_OUTPUT_BYTES
from claude_code_codex_review_loop.policy import PermissionProfile, ProfilePurpose, select_profile


def _probe(tmp_path: Path, mode: str, *, timeout_seconds: float = 30.0) -> AutoModeProbe:
    script = write_fake_claude(tmp_path)
    env = child_env()
    env["CC_REVIEW_FAKE_CLAUDE_MODE"] = mode
    return probe_auto_mode(
        (sys.executable, str(script)),
        workdir=tmp_path,
        env=env,
        timeout_seconds=timeout_seconds,
        grace_seconds=1.0,
    )


class TestDetectRule:
    """純粋規則: exit 0かつ設定がJSON objectとして解釈できる場合だけ利用可。"""

    @pytest.mark.parametrize(
        ("exit_code", "valid", "expected"),
        [
            (0, True, True),
            (0, False, False),
            (1, True, False),
            (None, False, False),
            (None, True, False),
        ],
    )
    def test_truth_table(self, exit_code: int | None, valid: bool, expected: bool) -> None:
        assert detect_auto_mode(AutoModeProbe(exit_code=exit_code, config_json_valid=valid)) is expected


class TestProbe:
    def test_available_cli_reports_success(self, tmp_path: Path) -> None:
        probe = _probe(tmp_path, "ok")
        assert probe == AutoModeProbe(exit_code=0, config_json_valid=True)
        assert detect_auto_mode(probe) is True

    def test_probe_leaves_no_directory_behind(self, tmp_path: Path) -> None:
        write_fake_claude(tmp_path)
        before = set(tmp_path.iterdir())
        _probe(tmp_path, "ok")
        assert set(tmp_path.iterdir()) == before

    def test_existing_workdir_files_are_untouched(self, tmp_path: Path) -> None:
        """出力先は呼び出しごとの専用directory。workdirの既存fileを壊さない。"""
        victim = tmp_path / "auto-mode-probe.out"
        victim.write_text("既存の成果物", encoding="utf-8")
        other = tmp_path / "notes.txt"
        other.write_text("大事なfile", encoding="utf-8")
        probe = _probe(tmp_path, "ok")
        assert probe.exit_code == 0
        assert victim.read_text(encoding="utf-8") == "既存の成果物"
        assert other.read_text(encoding="utf-8") == "大事なfile"

    def test_concurrent_probes_do_not_share_output(self, tmp_path: Path) -> None:
        """同一workdirでの連続probeが互いの出力先を共有しない。"""
        script = write_fake_claude(tmp_path)
        env = child_env()
        env["CC_REVIEW_FAKE_CLAUDE_MODE"] = "ok"
        seen: set[str] = set()
        real_create = auto_mode_module.create_private_dir

        def _record(path: Path) -> None:
            seen.add(path.name)
            real_create(path)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(auto_mode_module, "create_private_dir", _record)
            for _ in range(3):
                probe_auto_mode(
                    (sys.executable, str(script)),
                    workdir=tmp_path,
                    env=env,
                    timeout_seconds=30.0,
                    grace_seconds=1.0,
                )
        assert len(seen) == 3

    def test_failure_exit_code_is_preserved(self, tmp_path: Path) -> None:
        probe = _probe(tmp_path, "fail")
        assert probe.exit_code == 1 and probe.config_json_valid is False
        assert detect_auto_mode(probe) is False

    @pytest.mark.parametrize("mode", ["nonjson", "jsonarray"])
    def test_unparseable_config_is_unavailable(self, tmp_path: Path, mode: str) -> None:
        """mode名の文字列一致に依存せず、構造化された事実だけで判定する（P-003）。"""
        probe = _probe(tmp_path, mode)
        assert probe.exit_code == 0 and probe.config_json_valid is False
        assert detect_auto_mode(probe) is False

    def test_timeout_is_unavailable(self, tmp_path: Path) -> None:
        probe = _probe(tmp_path, "hang", timeout_seconds=1.0)
        assert probe == AutoModeProbe(exit_code=None, config_json_valid=False)

    def test_spawn_failure_is_unavailable(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.exe"
        probe = probe_auto_mode(
            (str(missing),), workdir=tmp_path, env=child_env(), timeout_seconds=5.0, grace_seconds=1.0
        )
        assert probe == AutoModeProbe(exit_code=None, config_json_valid=False)

    def test_relative_command_is_rejected(self, tmp_path: Path) -> None:
        """envが非継承のためPATH解決に依存しない（先頭は絶対path必須）。"""
        with pytest.raises(IdentityError) as excinfo:
            probe_auto_mode(
                ("claude",), workdir=tmp_path, env={}, timeout_seconds=5.0, grace_seconds=1.0
            )
        assert excinfo.value.stage == "probe"

    def test_empty_command_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(IdentityError):
            probe_auto_mode((), workdir=tmp_path, env={}, timeout_seconds=5.0, grace_seconds=1.0)

    def test_oversized_output_is_unavailable(self, tmp_path: Path) -> None:
        from claude_code_codex_review_loop.identity.auto_mode import _read_probe_output

        target = tmp_path / "big.json"
        target.write_bytes(b"[" + b"0," * MAX_PROBE_OUTPUT_BYTES + b"0]")
        assert _read_probe_output(target) is False

    def test_non_utf8_output_is_unavailable(self, tmp_path: Path) -> None:
        from claude_code_codex_review_loop.identity.auto_mode import _read_probe_output

        target = tmp_path / "broken.json"
        target.write_bytes(b"\xff\xfe{}")
        assert _read_probe_output(target) is False


class TestProfileSelection:
    """AC-C06-10: 検出（C-06）と選択規則（C-04）の接続。"""

    def test_available_selects_auto(self, tmp_path: Path) -> None:
        available = detect_auto_mode(_probe(tmp_path, "ok"))
        assert select_profile(available, ProfilePurpose.AUTOMATION) is PermissionProfile.AUTO

    @pytest.mark.parametrize(
        ("purpose", "expected"),
        [
            (ProfilePurpose.AUTOMATION, PermissionProfile.ACCEPT_EDITS),
            (ProfilePurpose.INTERACTIVE, PermissionProfile.DEFAULT),
            (ProfilePurpose.NON_INTERACTIVE, PermissionProfile.DONT_ASK),
        ],
    )
    def test_unavailable_selects_purpose_fallback(
        self, tmp_path: Path, purpose: ProfilePurpose, expected: PermissionProfile
    ) -> None:
        available = detect_auto_mode(_probe(tmp_path, "fail"))
        assert select_profile(available, purpose) is expected
