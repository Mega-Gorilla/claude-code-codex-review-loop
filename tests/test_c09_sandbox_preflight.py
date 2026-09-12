# SPDX-License-Identifier: Apache-2.0
"""C-09 sandbox preflightのhermetic test（ADR-0027 決定13 / 14）。

C-03の`run_tree`をscenario駆動のfakeへ差し替え、実Codex・認証・network・実GitHubは
使わない。production APIはcanonicalな実行file 1つしか受け取らないため、fake CLIは
argvではなくrunnerの差替えで注入する。probe自体は実filesystemとlocal socketで両outcomeを
実測し、probe fileのdigestが固定値と一致することも固定する。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import socket
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from claude_code_codex_review_loop.identity import create_private_dir
from claude_code_codex_review_loop.process import Completed, SpawnError, SpawnSpec, StopError
from claude_code_codex_review_loop.process import run_tree as real_run_tree
from claude_code_codex_review_loop.runtime import codex_preflight as module
from claude_code_codex_review_loop.runtime import sandbox_probe
from claude_code_codex_review_loop.runtime.codex_canary import prepare_codex_canary_home
from claude_code_codex_review_loop.runtime.codex_preflight import (
    EXPECTED_BOUNDARIES,
    EXPECTED_CONTROL,
    NETWORK_CONTROL_TARGET,
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
    "sandbox backend": "elevated",
    "sandbox provisioning": "complete",
}
_GOOD_LINES = [*(f"{label}={outcome}" for label, outcome in EXPECTED_BOUNDARIES.items()), "cleanup=ok"]
_CONTROL_LINES = [*(f"{label}={outcome}" for label, outcome in EXPECTED_CONTROL.items()), "cleanup=ok"]
_INTERPRETER = os.fspath(Path(sys.executable).resolve())
_PROBE_FILE = os.fspath(Path(sandbox_probe.__file__).resolve())


class FakeCodex:
    """`run_tree`の差替え。argvからstageを判定し、scenarioに従ってstdout fileを書く。"""

    def __init__(self) -> None:
        self.scenario: dict[str, object] = {
            "version": "codex-cli 0.0.0-fake",
            "version_exit": 0,
            "doctor": {"load_status": "ok", "helper_status": "ok", "details": dict(_GOOD_DETAILS)},
            "doctor_raw": None,
            "control_lines": list(_CONTROL_LINES),
            "control_exit": 0,
            "probe_lines": list(_GOOD_LINES),
            "sandbox_exit": 0,
            "raise_at": None,
            "timeout_at": None,
            "silent_at": None,
            "residue_at": None,
            "residue_dir_at": None,
        }
        self.specs: list[SpawnSpec] = []
        self.copies: list[Path] = []

    def doctor(self, **overrides: object) -> None:
        doctor: dict[str, object] = {"load_status": "ok", "helper_status": "ok", "details": dict(_GOOD_DETAILS)}
        doctor.update(overrides)
        self.scenario["doctor"] = doctor

    def run_tree(self, spec: SpawnSpec, timeout_seconds: float, grace_seconds: float) -> object:
        self.specs.append(spec)
        stage = spec.stdout_path.name.removesuffix(".stdout") if spec.stdout_path else ""
        if self.scenario["raise_at"] == stage:
            raise SpawnError("spawn", "test")
        if self.scenario.get("stop_at") == stage:
            raise StopError("close", "test")
        if self.scenario["timeout_at"] == stage:
            return object()
        if self.scenario["silent_at"] == stage:
            return Completed(exit_code=0)
        assert spec.stdout_path is not None
        argv = spec.argv
        sentinel = next((a for a in argv if a.startswith(".cc-review-probe-")), None)
        if sentinel and self.scenario["residue_at"] == stage:
            (spec.cwd / sentinel).write_text("left behind", encoding="utf-8")
        if sentinel and self.scenario["residue_dir_at"] == stage:
            (spec.cwd / sentinel).mkdir()
            (spec.cwd / sentinel / "inner").write_text("blocks unlink", encoding="utf-8")
        if argv[1:] == ("--version",):
            spec.stdout_path.write_text(str(self.scenario["version"]), encoding="utf-8")
            return Completed(exit_code=int(str(self.scenario["version_exit"])))
        if argv[1:] == ("doctor", "--json"):
            raw = self.scenario["doctor_raw"]
            spec.stdout_path.write_text(raw if isinstance(raw, str) else self._doctor_json(spec), encoding="utf-8")
            return Completed(exit_code=1)
        if argv[:2] == (_INTERPRETER, _PROBE_FILE):
            spec.stdout_path.write_text("\n".join(map(str, self.scenario["control_lines"])) + "\n", encoding="utf-8")  # type: ignore[call-overload]
            return Completed(exit_code=int(str(self.scenario["control_exit"])))
        if argv[1:2] == ("sandbox",):
            # probe本体はworkspaceへの複製で、sandboxが動く間だけ存在し、内容は固定digestと一致する
            copy = Path(argv[9])
            assert argv[8] == _INTERPRETER and copy.parent == spec.cwd and copy.name.endswith(".py")
            assert hashlib.sha256(copy.read_bytes().replace(b"\r\n", b"\n")).hexdigest() == module.PROBE_DIGEST
            self.copies.append(copy)
            spec.stdout_path.write_text("\n".join(map(str, self.scenario["probe_lines"])) + "\n", encoding="utf-8")  # type: ignore[call-overload]
            return Completed(exit_code=int(str(self.scenario["sandbox_exit"])))
        raise AssertionError(argv)

    def _doctor_json(self, spec: SpawnSpec) -> str:
        doctor = self.scenario["doctor"]
        assert isinstance(doctor, dict)
        return json.dumps(
            {
                "checks": {
                    "config.load": {
                        "status": doctor["load_status"],
                        "details": {
                            "CODEX_HOME": doctor.get("codex_home") or spec.env["CODEX_HOME"],
                            "cwd": doctor.get("cwd") or os.fspath(spec.cwd),
                        },
                    },
                    "sandbox.helpers": {"status": doctor["helper_status"], "details": doctor["details"]},
                }
            }
        )


class Fixture:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        evidence_root = tmp_path / "evidence"
        create_private_dir(evidence_root)
        self.evidence_root = evidence_root.resolve()
        self.codex_executable = Path(sys.executable).resolve()
        self.fake = FakeCodex()
        monkeypatch.setattr(module, "run_tree", self.fake.run_tree)

    @property
    def reviewer_env(self) -> dict[str, str]:
        env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if name in os.environ}
        env["PYTHONUTF8"] = "1"
        return env

    def run(self, **overrides: object) -> PreflightEvidence:
        values: dict[str, object] = {
            "home": self.home,
            "codex_executable": self.codex_executable,
            "reviewer_env": self.reviewer_env,
            "evidence_root": self.evidence_root,
            "timeout_seconds": 60.0,
            "grace_seconds": 1.0,
        }
        values.update(overrides)
        return run_sandbox_preflight(**values)  # type: ignore[arg-type]

    def verify(self, evidence: PreflightEvidence, **overrides: object) -> None:
        values: dict[str, object] = {
            "home": self.home,
            "codex_executable": self.codex_executable,
            "reviewer_env": self.reviewer_env,
            "evidence_root": self.evidence_root,
            "timeout_seconds": 60.0,
            "grace_seconds": 1.0,
        }
        values.update(overrides)
        verify_preflight_evidence(evidence, **values)  # type: ignore[arg-type]


@pytest.fixture
def fx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    return Fixture(tmp_path, monkeypatch)


class TestRunSandboxPreflight:
    def test_all_checks_pass_and_evidence_is_bound(self, fx: Fixture) -> None:
        evidence = fx.run()
        assert evidence.codex_executable == os.fspath(fx.codex_executable)
        assert evidence.codex_version == "codex-cli 0.0.0-fake"
        assert evidence.configuration_digest == fx.home.configuration_digest
        assert evidence.profile_name == "c09-canary"
        assert evidence.workspace_root == fx.workspace
        assert evidence.protected_roots == fx.home.protected_roots
        assert evidence.codex_home == fx.home.root
        assert len(evidence.environment_digest) == 64
        assert evidence.probe_interpreter == _INTERPRETER
        assert evidence.probe_digest == module.PROBE_DIGEST
        assert evidence.network_target == NETWORK_CONTROL_TARGET
        assert evidence.effective == EffectiveSandbox(
            "Never", "restricted", "restricted", "true", "elevated", "complete"
        )
        assert dict(evidence.control) == dict(EXPECTED_CONTROL)
        assert dict(evidence.boundaries) == dict(EXPECTED_BOUNDARIES)
        version, doctor, control, probe = fx.fake.specs
        host, port = NETWORK_CONTROL_TARGET
        codex = os.fspath(fx.codex_executable)
        sentinel = control.argv[-1]
        assert sentinel.startswith(".cc-review-probe-") and len(sentinel) == len(".cc-review-probe-") + 32
        assert version.argv == (codex, "--version") and doctor.argv == (codex, "doctor", "--json")
        assert control.argv == (
            _INTERPRETER, _PROBE_FILE, os.fspath(fx.workspace), os.fspath(fx.home.config_path), host, str(port),
            sentinel,
        )
        assert probe.argv == (
            codex, "sandbox", "-P", "c09-canary", "--include-managed-config", "-C", os.fspath(fx.workspace), "--",
            _INTERPRETER, os.fspath(fx.workspace / f"{sentinel}.py"), os.fspath(fx.workspace),
            os.fspath(fx.home.config_path), host, str(port),
            sentinel, os.fspath(fx.real_repository), os.fspath(fx.state_root),
        )
        assert fx.fake.copies == [fx.workspace / f"{sentinel}.py"] and not fx.fake.copies[0].exists()
        assert not any(path.name.startswith(".cc-review-probe-") for path in fx.workspace.iterdir())
        for spec in fx.fake.specs:
            assert spec.cwd == fx.workspace
            assert spec.env == {**fx.reviewer_env, "CODEX_HOME": os.fspath(fx.home.root)}
            assert spec.stdout_path is not None and spec.stdout_path.parent == fx.evidence_root
        assert "-a" not in probe.argv and "-c" not in probe.argv

    def test_unwritable_probe_copy_fails_closed_before_the_sandbox_runs(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = Path.write_bytes

        def failing(self: Path, data: bytes) -> int:
            if self.parent == fx.workspace and self.name.endswith(".py"):
                raise OSError("test")
            return original(self, data)

        monkeypatch.setattr(Path, "write_bytes", failing)
        with pytest.raises(PreflightError) as stopped:
            fx.run()
        assert stopped.value.stage == "probe_copy"
        assert [spec.stdout_path.name for spec in fx.fake.specs if spec.stdout_path] == [
            "version.stdout", "doctor.stdout", "control.stdout",
        ]

    def test_partially_written_probe_copy_is_removed_before_failing(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """disk full等で本文の一部を書いた後に失敗しても、複製を隔離checkoutへ残さない。"""
        original = Path.write_bytes

        def partial(self: Path, data: bytes) -> int:
            if self.parent == fx.workspace and self.name.endswith(".py"):
                original(self, data[:16])
                raise OSError("disk full")
            return original(self, data)

        monkeypatch.setattr(Path, "write_bytes", partial)
        with pytest.raises(PreflightError) as stopped:
            fx.run()
        assert stopped.value.stage == "probe_copy"
        assert not any(path.name.endswith(".py") for path in fx.workspace.iterdir())

    def test_partially_written_probe_copy_that_cannot_be_removed_is_residue(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = Path.write_bytes

        def partial(self: Path, data: bytes) -> int:
            if self.parent == fx.workspace and self.name.endswith(".py"):
                original(self, data[:16])
                raise OSError("disk full")
            return original(self, data)

        monkeypatch.setattr(Path, "write_bytes", partial)
        monkeypatch.setattr(module, "_remove_probe_copy", lambda copy: True)
        with pytest.raises(PreflightError) as stopped:
            fx.run()
        assert stopped.value.stage == "probe_residue"
        monkeypatch.undo()
        for path in fx.workspace.iterdir():
            path.unlink()

    def test_preexisting_entry_at_the_probe_copy_path_fails_closed(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """複製先に既にentryがあれば上書きせず停止する（sentinelはtestで固定する）。"""
        fixed = uuid.UUID(int=7)
        monkeypatch.setattr(module.uuid, "uuid4", lambda: fixed)
        (fx.workspace / f".cc-review-probe-{fixed.hex}.py").write_text("stale", encoding="utf-8")
        with pytest.raises(PreflightError) as stopped:
            fx.run()
        assert stopped.value.stage == "probe_copy"

    def test_probe_copy_that_cannot_be_unlinked_is_reported_as_residue(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = Path.unlink

        def refusing(self: Path, missing_ok: bool = False) -> None:
            if self.parent == fx.workspace and self.name.endswith(".py"):
                raise PermissionError("test")
            original(self, missing_ok)

        monkeypatch.setattr(Path, "unlink", refusing)
        with pytest.raises(PreflightError) as stopped:
            fx.run()
        assert stopped.value.stage == "probe_residue"
        monkeypatch.undo()
        for path in fx.workspace.iterdir():
            path.unlink()

    def test_leftover_probe_copy_is_reported_as_residue(self, fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(module, "_remove_probe_copy", lambda copy: True)
        with pytest.raises(PreflightError) as stopped:
            fx.run()
        assert stopped.value.stage == "probe_residue"

    def test_probe_copy_that_vanished_during_the_run_is_not_residue(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = fx.fake.run_tree

        def deleting(spec: SpawnSpec, timeout_seconds: float, grace_seconds: float) -> object:
            outcome = original(spec, timeout_seconds, grace_seconds)
            if spec.argv[1:2] == ("sandbox",):
                Path(spec.argv[9]).unlink()
            return outcome

        monkeypatch.setattr(module, "run_tree", deleting)
        fx.run()

    def test_production_api_accepts_no_probe_target_or_argv_prefix(self) -> None:
        """probe・接続先・argv prefixは呼出側から注入できない（ADR-0027 決定9 / 14）。"""
        for function in (run_sandbox_preflight, verify_preflight_evidence):
            names = set(inspect.signature(function).parameters)
            assert names.isdisjoint({"probe_command", "network_target", "codex_command"})
            assert inspect.signature(function).parameters["codex_executable"].annotation == "Path"

    def test_probe_file_matches_the_pinned_digest(self) -> None:
        """probeを変更したら`PROBE_DIGEST`を更新する。一致しなければfacadeはprobeを信頼しない。"""
        content = Path(sandbox_probe.__file__).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(content).hexdigest() == module.PROBE_DIGEST

    def test_token_environment_is_rejected_before_any_process_starts(self, fx: Fixture) -> None:
        env = {**fx.reviewer_env, "OPENAI_API_KEY": "sk-" + "x" * 40}
        with pytest.raises(PreflightError) as stopped:
            fx.run(reviewer_env=env)
        assert stopped.value.stage == "configuration"
        assert fx.fake.specs == []

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
            ({"control_exit": 1}, "control"),
            ({"control_lines": ["workspace_write=allowed", "garbage"]}, "control"),
            ({"control_lines": [*_CONTROL_LINES[:-2], "network=denied", "cleanup=ok"]}, "control_boundary"),
            ({"sandbox_exit": 1}, "sandbox_unavailable"),
            ({"probe_lines": ["workspace_write=allowed", "garbage"]}, "probe_unavailable"),
            ({"probe_lines": [*_GOOD_LINES[:-2], "network=allowed", "cleanup=ok"]}, "boundary"),
            ({"probe_lines": _GOOD_LINES[:-1]}, "probe_unavailable"),
            ({"probe_lines": [*_GOOD_LINES[:-1], "cleanup=failed"]}, "probe_residue"),
            ({"control_lines": [*_CONTROL_LINES[:-1], "cleanup=failed"]}, "probe_residue"),
            ({"residue_at": "control"}, "probe_residue"),
            ({"residue_at": "probe"}, "probe_residue"),
            ({"residue_dir_at": "probe"}, "probe_residue"),
            ({"raise_at": "version"}, "version"),
            ({"stop_at": "probe"}, "probe"),
            ({"timeout_at": "doctor"}, "doctor"),
            ({"silent_at": "probe"}, "probe"),
        ),
    )
    def test_each_stage_fails_closed(self, fx: Fixture, overrides: dict[str, object], stage: str) -> None:
        fx.fake.scenario.update(overrides)
        with pytest.raises(PreflightError) as stopped:
            fx.run()
        assert stopped.value.stage == stage
        leftovers = [path for path in fx.workspace.iterdir() if path.name.startswith(".cc-review-probe-")]
        # 残留fileはhost側で掃除される。directoryの残留はbest effortで残るが、停止理由は変わらない
        assert leftovers == [] or "residue_dir_at" in overrides

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
        fx.fake.doctor(**overrides)
        with pytest.raises(PreflightError) as stopped:
            fx.run()
        assert stopped.value.stage == stage
        assert len(fx.fake.specs) == 2

    @pytest.mark.parametrize(
        "details",
        (
            {**_GOOD_DETAILS, "sandbox backend": "unelevated"},
            {**_GOOD_DETAILS, "sandbox backend": "disabled"},
            {**_GOOD_DETAILS, "sandbox provisioning": "incomplete"},
            {k: v for k, v in _GOOD_DETAILS.items() if not k.startswith("sandbox ")},
        ),
        ids=("unelevated", "disabled", "incomplete", "fields_absent"),
    )
    def test_windows_requires_a_provisioned_elevated_backend(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch, details: dict[str, str]
    ) -> None:
        """D-033: helperがokでも、Windowsではelevated backendのprovisioning完了を要求する。"""
        monkeypatch.setattr(module, "_platform", lambda: "win32")
        fx.fake.doctor(details=details)
        with pytest.raises(PreflightError) as stopped:
            fx.run()
        assert stopped.value.stage == "sandbox_backend"
        assert len(fx.fake.specs) == 2

    def test_backend_fields_are_recorded_but_not_required_outside_windows(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POSIX backendは未実測のため要求値を置かない。fieldが無ければ空文字でevidenceへ記録する。"""
        monkeypatch.setattr(module, "_platform", lambda: "linux")
        fx.fake.doctor(details={k: v for k, v in _GOOD_DETAILS.items() if not k.startswith("sandbox ")})
        evidence = fx.run()
        assert evidence.effective.sandbox_backend == "" and evidence.effective.sandbox_provisioning == ""

    @pytest.mark.parametrize("kind", ("digest", "missing_file", "unreadable", "missing_interpreter"))
    def test_untrusted_probe_is_rejected_before_any_probe_runs(
        self, fx: Fixture, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if kind == "digest":
            monkeypatch.setattr(module, "PROBE_DIGEST", "0" * 64)
        elif kind == "missing_file":
            monkeypatch.setattr(sandbox_probe, "__file__", os.fspath(fx.tmp_path / "missing.py"))
        elif kind == "unreadable":
            original = Path.read_bytes

            def unreadable_probe(self: Path) -> bytes:
                if os.fspath(self) == _PROBE_FILE:
                    raise OSError("test")
                return original(self)

            monkeypatch.setattr(Path, "read_bytes", unreadable_probe)
        else:
            monkeypatch.setattr(sys, "executable", os.fspath(fx.tmp_path / "missing-python"))
        with pytest.raises(PreflightError) as stopped:
            fx.run()
        assert stopped.value.stage == "probe_integrity"
        assert [spec.stdout_path.name for spec in fx.fake.specs if spec.stdout_path] == [
            "version.stdout", "doctor.stdout",
        ]

    def test_real_spawn_path_reaches_codex_through_c03(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fakeを外し、実interpreterをcodexとして起動する。`--version`は通り、doctorで止まる。"""
        monkeypatch.setattr(module, "run_tree", real_run_tree)
        with pytest.raises(PreflightError) as stopped:
            fx.run()
        assert stopped.value.stage == "doctor_output"
        assert (fx.evidence_root / "version.stdout").read_text(encoding="utf-8").startswith("Python ")

    def test_spawn_failure_of_a_non_executable_is_classified(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(module, "run_tree", real_run_tree)
        not_executable = fx.tmp_path / "not-executable.txt"
        not_executable.write_text("plain", encoding="utf-8")
        with pytest.raises(PreflightError) as stopped:
            fx.run(codex_executable=not_executable.resolve())
        assert stopped.value.stage == "version"


class TestVerifyPreflightEvidence:
    def test_same_conditions_pass_by_remeasuring(self, fx: Fixture) -> None:
        evidence = fx.run()
        fx.verify(evidence)
        assert len(fx.fake.specs) == 8

    @pytest.mark.parametrize(
        ("overrides", "stage"),
        (
            ({"version": "codex-cli 0.0.1-fake"}, "evidence_mismatch"),
            ({"probe_lines": [*_GOOD_LINES[:-2], "network=allowed", "cleanup=ok"]}, "boundary"),
            ({"sandbox_exit": 1}, "sandbox_unavailable"),
            ({"probe_lines": [*_GOOD_LINES[:-1], "cleanup=failed"]}, "probe_residue"),
        ),
    )
    def test_reality_changed_after_measurement_is_rejected(
        self, fx: Fixture, overrides: dict[str, object], stage: str
    ) -> None:
        """再測定が正。CLI更新はevidence不一致、境界やbackendの変化は測定自体の失敗として止まる。"""
        evidence = fx.run()
        fx.fake.scenario.update(overrides)
        with pytest.raises(PreflightError) as stopped:
            fx.verify(evidence)
        assert stopped.value.stage == stage

    def test_effective_config_changed_after_measurement_is_rejected(self, fx: Fixture) -> None:
        evidence = fx.run()
        fx.fake.doctor(details={**_GOOD_DETAILS, "network sandbox": "enabled"})
        with pytest.raises(PreflightError) as stopped:
            fx.verify(evidence)
        assert stopped.value.stage == "effective_config"

    @pytest.mark.parametrize(
        "field",
        (
            "codex_executable", "codex_version", "configuration_digest", "profile_name", "workspace_root",
            "protected_roots", "codex_home", "environment_digest", "probe_interpreter", "probe_digest",
            "network_target", "effective", "control", "boundaries",
        ),
    )
    def test_forged_or_stale_evidence_is_rejected_against_fresh_measurement(self, fx: Fixture, field: str) -> None:
        evidence = fx.run()
        forged = {
            "codex_executable": "other",
            "codex_version": "codex-cli 9.9.9",
            "configuration_digest": "0" * 64,
            "profile_name": "other",
            "workspace_root": fx.real_repository,
            "protected_roots": fx.home.protected_roots[:-1],
            "codex_home": fx.workspace,
            "environment_digest": "0" * 64,
            "probe_interpreter": "other",
            "probe_digest": "0" * 64,
            "network_target": ("localhost", 1),
            "effective": EffectiveSandbox("UnlessTrusted", "restricted", "restricted", "true", "elevated", "complete"),
            "control": {**EXPECTED_CONTROL, "network": "denied"},
            "boundaries": {**EXPECTED_BOUNDARIES, "network": "allowed"},
        }
        with pytest.raises(PreflightError) as stopped:
            fx.verify(replace(evidence, **{field: forged[field]}))
        assert stopped.value.stage == "evidence_mismatch"

    def test_changed_environment_is_rejected(self, fx: Fixture) -> None:
        evidence = fx.run()
        with pytest.raises(PreflightError) as stopped:
            fx.verify(evidence, reviewer_env={**fx.reviewer_env, "EXTRA": "1"})
        assert stopped.value.stage == "evidence_mismatch"


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
            outcomes = sandbox_probe.run_probe(workspace, blocked, "127.0.0.1", port, ".probe", (blocked,))
        finally:
            listener.close()
        assert outcomes == {
            "workspace_write": "allowed",
            "protected_write": "denied",
            "credential_read": "allowed",
            "network": "allowed",
            "cleanup": "ok",
        }
        assert list(workspace.iterdir()) == []
        outcomes = sandbox_probe.run_probe(blocked, tmp_path / "missing", "127.0.0.1", port, ".probe", (workspace,))
        assert outcomes == {
            "workspace_write": "denied",
            "protected_write": "allowed",
            "credential_read": "denied",
            "network": "denied",
            "cleanup": "ok",
        }
        no_roots = sandbox_probe.run_probe(workspace, blocked, "127.0.0.1", port, ".probe", ())
        assert no_roots["protected_write"] == "allowed"

    def test_successful_write_with_failed_cleanup_is_still_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """作成できた書込は削除に失敗しても`allowed`。cleanup失敗は別に報告し、`denied`へ反転しない。"""
        protected = tmp_path / "protected"
        protected.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        original = Path.unlink

        def refuse_protected(self: Path, missing_ok: bool = False) -> None:
            if self.parent == protected:
                raise PermissionError("delete denied")
            original(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", refuse_protected)
        outcomes = sandbox_probe.run_probe(workspace, tmp_path / "missing", "127.0.0.1", 1, ".probe", (protected,))
        assert outcomes["protected_write"] == "allowed"
        assert outcomes["cleanup"] == "failed"
        assert (protected / ".probe").exists()
        assert list(workspace.iterdir()) == []

    def test_successful_connection_with_failed_close_is_still_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        original_close = socket.socket.close

        def failing_close(self: socket.socket) -> None:
            if self is not listener:
                raise OSError("close failed")
            original_close(self)

        monkeypatch.setattr(socket.socket, "close", failing_close)
        try:
            assert sandbox_probe._attempt(lambda: sandbox_probe._connect("127.0.0.1", port)) == "allowed"
        finally:
            listener.close()

    def test_main_prints_labels_or_usage(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert sandbox_probe.main([]) == 0
        assert capsys.readouterr().out == "usage=error\n"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        assert sandbox_probe.main([os.fspath(workspace), os.fspath(tmp_path / "missing"), "127.0.0.1", "1"]) == 0
        assert capsys.readouterr().out == "usage=error\n"
        arguments = [os.fspath(workspace), os.fspath(tmp_path / "missing"), "127.0.0.1", "1", ".probe"]
        assert sandbox_probe.main(arguments) == 0
        lines = capsys.readouterr().out.splitlines()
        assert [line.split("=")[0] for line in lines] == [*sandbox_probe.PROBE_LABELS, "cleanup"]

    def test_default_probe_command_runs_standalone(self, tmp_path: Path) -> None:
        command = sandbox_probe.default_probe_command()
        assert command[0] == sys.executable and Path(command[1]).is_file()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        completed = subprocess.run(
            [*command, os.fspath(workspace), os.fspath(tmp_path / "missing"), "127.0.0.1", "1", ".probe"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "workspace_write=allowed" in completed.stdout
        assert "credential_read=denied" in completed.stdout
