# SPDX-License-Identifier: Apache-2.0
"""C-09 sandbox preflightのhermetic test（ADR-0027 決定13 / 14）。

fakeのcodex（interpreter + script）をscenario fileで駆動し、実Codex・認証・network・
実GitHubは使わない。probe自体は実filesystemとlocal socketで両outcomeを実測する。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from claude_code_codex_review_loop.identity import create_private_dir
from claude_code_codex_review_loop.process import Completed
from claude_code_codex_review_loop.runtime import codex_preflight as module
from claude_code_codex_review_loop.runtime import sandbox_probe
from claude_code_codex_review_loop.runtime.codex_canary import prepare_codex_canary_home
from claude_code_codex_review_loop.runtime.codex_preflight import (
    EXPECTED_BOUNDARIES,
    EffectiveSandbox,
    PreflightError,
    PreflightEvidence,
    run_sandbox_preflight,
    verify_preflight_evidence,
)

_GOOD_DETAILS = {
    "approval policy": "Never",
    "filesystem sandbox": "restricted",
    "network sandbox": "restricted",
    "denied-read restrictions": "true",
}
_GOOD_LINES = [f"{label}={outcome}" for label, outcome in EXPECTED_BOUNDARIES.items()]

_FAKE_CODEX = r'''
import json, os, pathlib, sys, time
scenario = json.loads(pathlib.Path(os.environ["FAKE_CODEX_SCENARIO"]).read_text(encoding="utf-8"))
argv = sys.argv[1:]
with open(scenario["argv_log"], "a", encoding="utf-8") as log:
    log.write(json.dumps(argv) + "\n")
if scenario.get("sleep_seconds"):
    time.sleep(scenario["sleep_seconds"])
if argv == ["--version"]:
    sys.stdout.write(scenario["version"])
    sys.exit(scenario["version_exit"])
if argv == ["doctor", "--json"]:
    if scenario.get("doctor_raw") is not None:
        sys.stdout.write(scenario["doctor_raw"])
        sys.exit(1)
    doctor = scenario["doctor"]
    report = {"checks": {
        "config.load": {"status": doctor["load_status"], "details": {
            "CODEX_HOME": doctor.get("codex_home") or os.environ["CODEX_HOME"],
            "cwd": doctor.get("cwd") or os.getcwd(),
        }},
        "sandbox.helpers": {"status": doctor["helper_status"], "details": doctor["details"]},
    }}
    sys.stdout.write(json.dumps(report))
    sys.exit(1)
if argv[:1] == ["sandbox"]:
    sys.stdout.write("\n".join(scenario["probe_lines"]) + "\n")
    sys.exit(scenario["sandbox_exit"])
sys.exit(3)
'''


class Fixture:
    """1 testぶんのhome・fake codex・scenarioをまとめる。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        private = tmp_path / "private"
        create_private_dir(private)
        self.workspace = (tmp_path / "checkout").resolve()
        self.real_repository = (tmp_path / "real-repository").resolve()
        self.state_root = (tmp_path / "state").resolve()
        for path in (self.workspace, self.real_repository, self.state_root):
            path.mkdir()
        self.home = prepare_codex_canary_home(
            private_root=private.resolve(),
            name="codex-home",
            workspace_root=self.workspace,
            protected_roots=(self.real_repository, self.state_root),
        )
        self.evidence_root = tmp_path / "evidence"
        create_private_dir(self.evidence_root)
        self.evidence_root = self.evidence_root.resolve()
        self.script = tmp_path / "fake_codex.py"
        self.script.write_text(_FAKE_CODEX, encoding="utf-8")
        self.scenario_path = tmp_path / "scenario.json"
        self.argv_log = tmp_path / "argv.log"
        self.codex_command = (os.fspath(Path(sys.executable).resolve()), os.fspath(self.script))
        self.probe_command = (os.fspath(Path(sys.executable).resolve()), os.fspath(tmp_path / "probe.py"))
        self.write_scenario()

    def write_scenario(self, **overrides: object) -> None:
        scenario: dict[str, object] = {
            "version": "codex-cli 0.0.0-fake",
            "version_exit": 0,
            "doctor": {"load_status": "ok", "helper_status": "ok", "details": dict(_GOOD_DETAILS)},
            "doctor_raw": None,
            "sandbox_exit": 0,
            "probe_lines": list(_GOOD_LINES),
            "sleep_seconds": 0,
            "argv_log": os.fspath(self.argv_log),
        }
        scenario.update(overrides)
        self.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

    def doctor(self, **overrides: object) -> None:
        doctor: dict[str, object] = {"load_status": "ok", "helper_status": "ok", "details": dict(_GOOD_DETAILS)}
        doctor.update(overrides)
        self.write_scenario(doctor=doctor)

    @property
    def reviewer_env(self) -> dict[str, str]:
        env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if name in os.environ}
        env["PYTHONUTF8"] = "1"
        env["FAKE_CODEX_SCENARIO"] = os.fspath(self.scenario_path)
        return env

    def run(self, **overrides: object) -> PreflightEvidence:
        values: dict[str, object] = {
            "home": self.home,
            "codex_command": self.codex_command,
            "reviewer_env": self.reviewer_env,
            "probe_command": self.probe_command,
            "network_target": ("api.github.invalid", 443),
            "evidence_root": self.evidence_root,
            "timeout_seconds": 60.0,
            "grace_seconds": 1.0,
        }
        values.update(overrides)
        return run_sandbox_preflight(**values)  # type: ignore[arg-type]

    def verify(self, evidence: PreflightEvidence, **overrides: object) -> None:
        values: dict[str, object] = {
            "home": self.home,
            "codex_command": self.codex_command,
            "reviewer_env": self.reviewer_env,
            "evidence_root": self.evidence_root,
            "timeout_seconds": 60.0,
            "grace_seconds": 1.0,
        }
        values.update(overrides)
        verify_preflight_evidence(evidence, **values)  # type: ignore[arg-type]

    def logged_argv(self) -> list[list[str]]:
        return [json.loads(line) for line in self.argv_log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


class TestRunSandboxPreflight:
    def test_both_checks_pass_and_evidence_is_bound(self, fx: Fixture) -> None:
        evidence = fx.run()
        assert evidence.codex_command == fx.codex_command
        assert evidence.codex_version == "codex-cli 0.0.0-fake"
        assert evidence.configuration_digest == fx.home.configuration_digest
        assert evidence.profile_name == "c09-canary"
        assert evidence.workspace_root == fx.workspace
        assert evidence.protected_roots == fx.home.protected_roots
        assert evidence.codex_home == fx.home.root
        assert len(evidence.environment_digest) == 64
        assert evidence.effective == EffectiveSandbox("Never", "restricted", "restricted", "true")
        assert dict(evidence.boundaries) == dict(EXPECTED_BOUNDARIES)
        version, doctor, probe = fx.logged_argv()
        assert version == ["--version"] and doctor == ["doctor", "--json"]
        assert probe == [
            "sandbox", "-P", "c09-canary", "--include-managed-config", "-C", os.fspath(fx.workspace), "--",
            *fx.probe_command, os.fspath(fx.workspace), os.fspath(fx.home.config_path), "api.github.invalid", "443",
            os.fspath(fx.real_repository), os.fspath(fx.state_root),
        ]
        assert "-a" not in probe and "-c" not in probe

    def test_token_environment_is_rejected_before_any_process_starts(self, fx: Fixture) -> None:
        env = {**fx.reviewer_env, "OPENAI_API_KEY": "sk-" + "x" * 40}
        with pytest.raises(PreflightError) as stopped:
            fx.run(reviewer_env=env)
        assert stopped.value.stage == "configuration"
        assert not fx.argv_log.exists()

    @pytest.mark.parametrize("kind", ("relative", "missing", "inside_workspace", "inside_home", "not_private"))
    def test_evidence_root_must_be_private_and_disjoint(
        self, fx: Fixture, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        roots = {
            "relative": Path("relative"),
            "missing": (fx.tmp_path / "missing").resolve(),
            "inside_workspace": fx.workspace,
            "inside_home": fx.home.root,
            "not_private": fx.evidence_root,
        }
        if kind == "not_private":
            monkeypatch.setattr(
                module,
                "verify_private_dir",
                lambda *args: (_ for _ in ()).throw(module.FsPermissionError("verify", "test")),
            )
        with pytest.raises(PreflightError) as stopped:
            fx.run(evidence_root=roots[kind])
        assert stopped.value.stage == "evidence_root"

    @pytest.mark.parametrize(
        ("overrides", "stage"),
        (
            ({"version_exit": 1}, "version"),
            ({"version": ""}, "version"),
            ({"version": "codex-cli 1\nextra"}, "version"),
            ({"doctor_raw": "not json"}, "doctor_output"),
            ({"doctor_raw": "{\"checks\": {}}"}, "doctor_output"),
            ({"doctor_raw": "[]"}, "doctor_output"),
            ({"sandbox_exit": 1}, "sandbox_unavailable"),
            ({"probe_lines": ["workspace_write=allowed", "garbage"]}, "probe_unavailable"),
            ({"probe_lines": [*_GOOD_LINES[:-1], "network=allowed"]}, "boundary"),
        ),
    )
    def test_each_stage_fails_closed(self, fx: Fixture, overrides: dict[str, object], stage: str) -> None:
        fx.write_scenario(**overrides)
        with pytest.raises(PreflightError) as stopped:
            fx.run()
        assert stopped.value.stage == stage

    @pytest.mark.parametrize(
        ("overrides", "stage"),
        (
            ({"load_status": "fail"}, "effective_config"),
            ({"codex_home": "elsewhere"}, "effective_config"),
            ({"cwd": "elsewhere"}, "effective_config"),
            ({"details": {**_GOOD_DETAILS, "approval policy": "UnlessTrusted"}}, "effective_config"),
            ({"details": {**_GOOD_DETAILS, "filesystem sandbox": "unrestricted"}}, "effective_config"),
            ({"details": {**_GOOD_DETAILS, "network sandbox": "enabled"}}, "effective_config"),
            ({"details": {**_GOOD_DETAILS, "denied-read restrictions": "false"}}, "effective_config"),
            ({"details": {"approval policy": "Never"}}, "doctor_output"),
            ({"helper_status": "fail"}, "sandbox_unavailable"),
        ),
    )
    def test_effective_config_mismatch_fails_closed(
        self, fx: Fixture, overrides: dict[str, object], stage: str
    ) -> None:
        fx.doctor(**overrides)
        with pytest.raises(PreflightError) as stopped:
            fx.run()
        assert stopped.value.stage == stage

    @pytest.mark.parametrize(
        ("probe_command", "network_target"),
        (
            ((), ("host", 443)),
            (("python", ""), ("host", 443)),
            (("python",), ("", 443)),
            (("python",), ("host", 0)),
        ),
    )
    def test_invalid_probe_command_or_target_is_rejected(
        self, fx: Fixture, probe_command: tuple[str, ...], network_target: tuple[str, int]
    ) -> None:
        with pytest.raises(PreflightError) as stopped:
            fx.run(probe_command=probe_command, network_target=network_target)
        assert stopped.value.stage == "probe_command"

    def test_spawn_failure_is_classified_by_stage(self, fx: Fixture) -> None:
        not_executable = fx.tmp_path / "not-executable.txt"
        not_executable.write_text("plain", encoding="utf-8")
        with pytest.raises(PreflightError) as stopped:
            fx.run(codex_command=(os.fspath(not_executable.resolve()),))
        assert stopped.value.stage == "version"

    def test_timeout_is_classified_by_stage(self, fx: Fixture) -> None:
        fx.write_scenario(sleep_seconds=30)
        with pytest.raises(PreflightError) as stopped:
            fx.run(timeout_seconds=0.5, grace_seconds=0.2)
        assert stopped.value.stage == "version"

    def test_missing_output_file_is_classified_by_stage(self, fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(module, "run_tree", lambda *args, **kwargs: Completed(exit_code=0))
        with pytest.raises(PreflightError) as stopped:
            fx.run()
        assert stopped.value.stage == "version"


class TestVerifyPreflightEvidence:
    def test_same_conditions_pass(self, fx: Fixture) -> None:
        evidence = fx.run()
        fx.verify(evidence)

    @pytest.mark.parametrize(
        "field",
        (
            "codex_command",
            "configuration_digest",
            "profile_name",
            "workspace_root",
            "protected_roots",
            "codex_home",
            "environment_digest",
            "boundaries",
            "effective",
            "codex_version",
        ),
    )
    def test_any_changed_binding_is_rejected(self, fx: Fixture, field: str) -> None:
        evidence = fx.run()
        changed = {
            "codex_command": (fx.codex_command[0],),
            "configuration_digest": "0" * 64,
            "profile_name": "other",
            "workspace_root": fx.real_repository,
            "protected_roots": fx.home.protected_roots[:-1],
            "codex_home": fx.workspace,
            "environment_digest": "0" * 64,
            "boundaries": {**EXPECTED_BOUNDARIES, "network": "allowed"},
            "effective": EffectiveSandbox("UnlessTrusted", "restricted", "restricted", "true"),
            "codex_version": "codex-cli 9.9.9",
        }
        with pytest.raises(PreflightError) as stopped:
            fx.verify(replace(evidence, **{field: changed[field]}))
        assert stopped.value.stage == "evidence_mismatch"

    def test_changed_environment_or_command_is_rejected(self, fx: Fixture) -> None:
        evidence = fx.run()
        with pytest.raises(PreflightError) as stopped:
            fx.verify(evidence, reviewer_env={**fx.reviewer_env, "EXTRA": "1"})
        assert stopped.value.stage == "evidence_mismatch"

    def test_upgraded_cli_is_rejected_at_spawn_time(self, fx: Fixture) -> None:
        evidence = fx.run()
        fx.write_scenario(version="codex-cli 0.0.1-fake")
        with pytest.raises(PreflightError) as stopped:
            fx.verify(evidence)
        assert stopped.value.stage == "evidence_mismatch"

    def test_configuration_and_evidence_root_are_revalidated(self, fx: Fixture) -> None:
        evidence = fx.run()
        with pytest.raises(PreflightError) as configuration:
            fx.verify(evidence, reviewer_env={**fx.reviewer_env, "OPENAI_API_KEY": "sk-" + "x" * 40})
        assert configuration.value.stage == "configuration"
        with pytest.raises(PreflightError) as root:
            fx.verify(evidence, evidence_root=fx.workspace)
        assert root.value.stage == "evidence_root"


class TestSandboxProbe:
    def test_reports_each_boundary_from_real_observations(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        blocked = tmp_path / "blocked-file"
        blocked.write_text("not a directory", encoding="utf-8")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            outcomes = sandbox_probe.run_probe(workspace, blocked, "127.0.0.1", port, (blocked,))
        finally:
            listener.close()
        assert outcomes == {
            "workspace_write": "allowed",
            "protected_write": "denied",
            "credential_read": "allowed",
            "network": "allowed",
        }
        assert list(workspace.iterdir()) == []
        outcomes = sandbox_probe.run_probe(blocked, tmp_path / "missing", "127.0.0.1", port, (workspace,))
        assert outcomes == {
            "workspace_write": "denied",
            "protected_write": "allowed",
            "credential_read": "denied",
            "network": "denied",
        }
        assert sandbox_probe.run_probe(workspace, blocked, "127.0.0.1", port, ())["protected_write"] == "allowed"

    def test_main_prints_labels_or_usage(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert sandbox_probe.main([]) == 0
        assert capsys.readouterr().out == "usage=error\n"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        assert sandbox_probe.main([os.fspath(workspace), os.fspath(tmp_path / "missing"), "127.0.0.1", "1"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert [line.split("=")[0] for line in lines] == list(sandbox_probe.PROBE_LABELS)

    def test_default_probe_command_runs_standalone(self, tmp_path: Path) -> None:
        command = sandbox_probe.default_probe_command()
        assert command[0] == sys.executable and Path(command[1]).is_file()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        completed = subprocess.run(
            [*command, os.fspath(workspace), os.fspath(tmp_path / "missing"), "127.0.0.1", "1"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "workspace_write=allowed" in completed.stdout
        assert "credential_read=denied" in completed.stdout
