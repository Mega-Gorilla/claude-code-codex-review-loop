# SPDX-License-Identifier: Apache-2.0
"""C-09 auth-setup（ADR-0031 決定3）のhermetic test。実Codex・実login・実credential storeは使わない。"""

from __future__ import annotations

import inspect
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from claude_code_codex_review_loop.identity import create_private_dir
from claude_code_codex_review_loop.process import Completed, SpawnError, SpawnSpec, StopError
from claude_code_codex_review_loop.runtime import reviewer_auth_setup as module
from claude_code_codex_review_loop.runtime.reviewer_auth_setup import (
    AuthSetupError,
    AuthSetupResult,
    LoginPort,
    SpawnLogin,
    run_reviewer_auth_setup,
)
from claude_code_codex_review_loop.runtime.reviewer_home import MARKER_NAME


class FakeCodex:
    """`run_tree`の差替え。`--version` / `doctor --json`へscenarioどおりに応答する。"""

    def __init__(self) -> None:
        self.version = "codex-cli 0.154.0"
        self.storage_before = "Keyring"
        self.status_after = "ok"
        self.storage_after = "Keyring"
        self.doctor_raw: str | None = None
        self.home_seen: str | None = None
        self.raise_at: str | None = None
        self.timeout_at: str | None = None
        self.specs: list[SpawnSpec] = []

    def run_tree(self, spec: SpawnSpec, timeout_seconds: float, grace_seconds: float) -> object:
        self.specs.append(spec)
        stage = spec.stdout_path.name.removesuffix(".stdout") if spec.stdout_path else ""
        if self.raise_at == stage:
            raise SpawnError("spawn", "test")
        if self.timeout_at == stage:
            return object()
        assert spec.stdout_path is not None
        if spec.argv[1:] == ("--version",):
            spec.stdout_path.write_text(self.version, encoding="utf-8")
            return Completed(exit_code=0)
        if spec.argv[1:] == ("doctor", "--json"):
            if self.doctor_raw is not None:
                spec.stdout_path.write_text(self.doctor_raw, encoding="utf-8")
                return Completed(exit_code=1)
            first = stage == "doctor_before"
            payload = {
                "checks": {
                    "config.load": {
                        "status": "ok",
                        "details": {"CODEX_HOME": self.home_seen or spec.env["CODEX_HOME"]},
                    },
                    "auth.credentials": {
                        "status": "fail" if first else self.status_after,
                        "details": {"auth storage mode": self.storage_before if first else self.storage_after},
                    },
                }
            }
            spec.stdout_path.write_text(json.dumps(payload), encoding="utf-8")
            return Completed(exit_code=1)
        raise AssertionError(spec.argv)


class FakeLogin:
    def __init__(self) -> None:
        self.exit_code = 0
        self.write_auth_file = False
        self.calls: list[dict[str, object]] = []

    def login(self, *, env: Mapping[str, str], home: Path, evidence_root: Path) -> int:
        self.calls.append({"env": dict(env), "home": home, "evidence_root": evidence_root})
        assert (home / "config.toml").is_file()  # 管理下configが先に置かれている
        if self.write_auth_file:
            (home / "auth.json").write_text('{"tokens": "fake"}', encoding="utf-8")
        return self.exit_code


class Fixture:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.parent = (tmp_path / "homes").resolve()
        create_private_dir(self.parent)
        self.evidence = (tmp_path / "evidence").resolve()
        create_private_dir(self.evidence)
        self.state = (tmp_path / "state").resolve()
        self.state.mkdir()
        self.fake = FakeCodex()
        self.login = FakeLogin()
        monkeypatch.setattr(module, "run_tree", self.fake.run_tree)
        monkeypatch.setattr(module, "_platform", lambda: "win32")

    @property
    def env(self) -> dict[str, str]:
        env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if name in os.environ}
        env["PYTHONUTF8"] = "1"
        return env

    def run(self, **overrides: object) -> AuthSetupResult:
        values: dict[str, object] = {
            "parent": self.parent,
            "name": "reviewer-codex-home",
            "disjoint_from": (self.state,),
            "codex_executable": Path(sys.executable).resolve(),
            "reviewer_env": self.env,
            "evidence_root": self.evidence,
            "login": self.login,
            "timeout_seconds": 60.0,
            "grace_seconds": 1.0,
        }
        values.update(overrides)
        return run_reviewer_auth_setup(**values)  # type: ignore[arg-type]

    @property
    def home(self) -> Path:
        return self.parent / "reviewer-codex-home"


@pytest.fixture
def fx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    return Fixture(tmp_path, monkeypatch)


class TestRunReviewerAuthSetup:
    def test_fixed_order_and_result(self, fx: Fixture) -> None:
        result = fx.run()
        assert result.home == fx.home and result.marker == fx.home / MARKER_NAME
        assert (result.codex_version, result.storage_mode) == ("codex-cli 0.154.0", "Keyring")
        assert (fx.home / MARKER_NAME).is_file() and not (fx.home / "auth.json").exists()
        config = (fx.home / "config.toml").read_text(encoding="utf-8")
        assert 'cli_auth_credentials_store = "keyring"' in config and 'approval_policy = "never"' in config
        # 順序: version -> doctor（Keyring確認） -> login -> doctor（登録確認）
        stages = [spec.stdout_path.name for spec in fx.fake.specs if spec.stdout_path]
        assert stages == ["version.stdout", "doctor_before.stdout", "doctor_after.stdout"]
        (call,) = fx.login.calls
        env = call["env"]
        assert isinstance(env, dict) and env["CODEX_HOME"] == os.fspath(fx.home)
        assert not any(name.upper().endswith("KEY") for name in env)
        assert call["home"] == fx.home and call["evidence_root"] == fx.evidence
        for spec in fx.fake.specs:
            assert spec.env["CODEX_HOME"] == os.fspath(fx.home) and spec.cwd == fx.home
            assert spec.stdout_path is not None and spec.stdout_path.parent == fx.evidence
        assert not (fx.parent / "reviewer-codex-home.lock").exists()

    def test_storage_mode_other_than_keyring_stops_before_login(self, fx: Fixture) -> None:
        """決定3-3: doctorが`File` / `Auto`ならloginを起動しない（auth.jsonが作られ得る）。"""
        fx.fake.storage_before = "File"
        with pytest.raises(AuthSetupError) as stopped:
            fx.run()
        assert stopped.value.stage == "storage_mode" and fx.login.calls == []
        # markerはconfigより先に置かれるため、途中失敗後も次回の固定home取得が再生成で回復できる
        assert (fx.home / MARKER_NAME).is_file() and not (fx.parent / "reviewer-codex-home.lock").exists()

    def test_login_that_writes_a_file_credential_is_rejected_and_the_file_removed(self, fx: Fixture) -> None:
        fx.login.write_auth_file = True
        with pytest.raises(AuthSetupError) as stopped:
            fx.run()
        assert stopped.value.stage == "file_credentials"
        assert not (fx.home / "auth.json").exists() and (fx.home / MARKER_NAME).is_file()

    def test_preexisting_file_credential_is_rejected_before_login(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = module.render_auth_setup_configuration

        def plant() -> str:
            (fx.home / "auth.json").write_text("{}", encoding="utf-8")
            return original()

        monkeypatch.setattr(module, "render_auth_setup_configuration", plant)
        with pytest.raises(AuthSetupError) as stopped:
            fx.run()
        assert stopped.value.stage == "file_credentials" and fx.login.calls == []

    def test_failed_login_is_reported(self, fx: Fixture) -> None:
        fx.login.exit_code = 1
        with pytest.raises(AuthSetupError) as stopped:
            fx.run()
        assert stopped.value.stage == "login" and (fx.home / MARKER_NAME).is_file()

    @pytest.mark.parametrize(("status", "storage"), (("fail", "Keyring"), ("ok", "File")))
    def test_unregistered_credential_after_login_is_reported(self, fx: Fixture, status: str, storage: str) -> None:
        fx.fake.status_after = status
        fx.fake.storage_after = storage
        with pytest.raises(AuthSetupError) as stopped:
            fx.run()
        assert stopped.value.stage == "not_registered" and (fx.home / MARKER_NAME).is_file()

    def test_unsupported_cli_version_stops_before_doctor(self, fx: Fixture) -> None:
        fx.fake.version = "codex-cli 0.155.0"
        with pytest.raises(AuthSetupError) as stopped:
            fx.run()
        assert stopped.value.stage == "auth_version" and fx.login.calls == []

    def test_non_windows_platform_is_fail_closed(self, fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(module, "_platform", lambda: "linux")
        with pytest.raises(AuthSetupError) as stopped:
            fx.run()
        assert stopped.value.stage == "auth_platform" and fx.fake.specs == []

    @pytest.mark.parametrize(
        ("overrides", "stage"),
        (
            ({"doctor_raw": "{not json"}, "doctor_before_output"),
            ({"doctor_raw": json.dumps({"checks": {}})}, "doctor_before_output"),
            ({"home_seen": "elsewhere"}, "doctor_before_home"),
            ({"raise_at": "doctor_before"}, "doctor_before"),
            ({"timeout_at": "version"}, "version"),
            ({"version": ""}, "version"),
        ),
        ids=("not_json", "missing_checks", "other_home", "spawn_error", "timeout", "empty_version"),
    )
    def test_doctor_and_version_failures_are_classified(
        self, fx: Fixture, overrides: dict[str, object], stage: str
    ) -> None:
        for key, value in overrides.items():
            setattr(fx.fake, key, value)
        with pytest.raises(AuthSetupError) as stopped:
            fx.run()
        assert stopped.value.stage == stage and fx.login.calls == []

    @pytest.mark.parametrize(
        "name",
        ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "codex_access_token", "Codex_Access_Token"),
    )
    def test_token_environment_is_rejected_before_anything(self, fx: Fixture, name: str) -> None:
        """Codex CLIが認証材料として読むalias（`codex login --with-access-token`の`CODEX_ACCESS_TOKEN`を含む）は、
        大文字小文字を問わず、home作成・version / doctor / loginのどれよりも前に`environment`で止まる。
        """
        secret = "sk-" + "x" * 40
        with pytest.raises(AuthSetupError) as stopped:
            fx.run(reviewer_env={**fx.env, name: secret})
        assert stopped.value.stage == "environment"
        assert fx.fake.specs == [] and fx.login.calls == [] and not fx.home.exists()
        assert secret not in str(stopped.value)

    @pytest.mark.parametrize("kind", ("relative", "missing"))
    def test_invalid_executable_is_rejected(self, fx: Fixture, kind: str) -> None:
        candidate = Path("relative") if kind == "relative" else (fx.tmp_path / "missing").resolve()
        with pytest.raises(AuthSetupError) as stopped:
            fx.run(codex_executable=candidate)
        assert stopped.value.stage == "executable"

    @pytest.mark.parametrize("kind", ("relative", "missing", "not_private"))
    def test_invalid_evidence_root_is_rejected(self, fx: Fixture, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        if kind == "not_private":
            monkeypatch.setattr(
                module,
                "verify_private_dir",
                lambda path: (_ for _ in ()).throw(module.FsPermissionError("verify", "t")),
            )
        roots = {
            "relative": Path("relative"), "missing": (fx.tmp_path / "missing").resolve(), "not_private": fx.evidence,
        }
        with pytest.raises(AuthSetupError) as stopped:
            fx.run(evidence_root=roots[kind])
        assert stopped.value.stage == "evidence_root"

    def test_home_errors_are_mapped(self, fx: Fixture) -> None:
        fx.home.mkdir()
        (fx.home / "precious.txt").write_text("keep", encoding="utf-8")
        with pytest.raises(AuthSetupError) as stopped:
            fx.run()
        assert stopped.value.stage == "home:home_marker"
        assert (fx.home / "precious.txt").read_text(encoding="utf-8") == "keep"

    def test_configuration_write_failure_is_classified(self, fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            module, "write_private_text", lambda *args: (_ for _ in ()).throw(module.FsPermissionError("write", "t"))
        )
        with pytest.raises(AuthSetupError) as stopped:
            fx.run()
        assert stopped.value.stage == "configuration"

    def test_home_creation_failure_is_mapped(self, fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(module, "create_home_dir", lambda home: (_ for _ in ()).throw(module.HomeError("create")))
        with pytest.raises(AuthSetupError) as stopped:
            fx.run()
        assert stopped.value.stage == "home:create" and fx.fake.specs == []

    def test_unremovable_file_credential_still_stops_with_the_same_stage(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fx.login.write_auth_file = True
        original = Path.unlink

        def refusing(self: Path, missing_ok: bool = False) -> None:
            if self.name == "auth.json":
                raise PermissionError("test")
            original(self, missing_ok)

        monkeypatch.setattr(Path, "unlink", refusing)
        with pytest.raises(AuthSetupError) as stopped:
            fx.run()
        assert stopped.value.stage == "file_credentials"
        monkeypatch.undo()

    def test_marker_write_failure_removes_the_fresh_home(self, fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            module, "write_home_marker", lambda home: (_ for _ in ()).throw(module.HomeError("marker"))
        )
        with pytest.raises(AuthSetupError) as stopped:
            fx.run()
        assert stopped.value.stage == "home:marker" and not fx.home.exists() and fx.fake.specs == []

    def test_marker_write_failure_with_unremovable_home_still_stops(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            module, "write_home_marker", lambda home: (_ for _ in ()).throw(module.HomeError("marker"))
        )
        monkeypatch.setattr(
            module, "remove_tree", lambda root: (_ for _ in ()).throw(module.FsPermissionError("remove", "t"))
        )
        with pytest.raises(AuthSetupError) as stopped:
            fx.run()
        assert stopped.value.stage == "home:marker"

    def test_failed_setup_is_recoverable_on_the_next_attempt(self, fx: Fixture) -> None:
        """config失敗やlogin失敗の後でも、marker付きのhomeを次回の取得が再生成する。"""
        fx.login.exit_code = 1
        with pytest.raises(AuthSetupError):
            fx.run()
        fx.login.exit_code = 0
        result = fx.run()
        assert result.marker.is_file() and (fx.home / "config.toml").is_file()

    def test_stdout_read_failure_is_classified(self, fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
        original = Path.open

        def failing(self: Path, *args: object, **kwargs: object) -> object:
            if self.name == "version.stdout" and args and args[0] == "rb":
                raise PermissionError("test")
            return original(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "open", failing)
        with pytest.raises(AuthSetupError) as stopped:
            fx.run()
        assert stopped.value.stage == "version"


class TestSpawnLogin:
    def test_spawns_device_auth_with_fixed_argv_and_evidence_outputs(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[SpawnSpec] = []

        def fake_run_tree(spec: SpawnSpec, timeout_seconds: float, grace_seconds: float) -> object:
            seen.append(spec)
            return Completed(exit_code=0)

        monkeypatch.setattr(module, "run_tree", fake_run_tree)
        port: LoginPort = SpawnLogin(
            codex_executable=Path(sys.executable).resolve(), timeout_seconds=30.0, grace_seconds=1.0
        )
        code = port.login(env={"CODEX_HOME": os.fspath(fx.home)}, home=fx.home, evidence_root=fx.evidence)
        (spec,) = seen
        assert code == 0
        assert spec.argv == (os.fspath(Path(sys.executable).resolve()), "login", "--device-auth")
        assert spec.cwd == fx.home and spec.env == {"CODEX_HOME": os.fspath(fx.home)}
        assert spec.stdout_path == fx.evidence / "login.stdout" and spec.stderr_path == fx.evidence / "login.stderr"
        assert spec.stdin_path is None

    @pytest.mark.parametrize(
        ("outcome", "stage"),
        (
            (SpawnError("spawn", "test"), "login_spawn"),
            (StopError("close", "test"), "login_spawn"),
            (object(), "login_timeout"),
        ),
        ids=("spawn_error", "stop_error", "timeout"),
    )
    def test_failures_are_classified(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch, outcome: object, stage: str
    ) -> None:
        def fake_run_tree(spec: SpawnSpec, timeout_seconds: float, grace_seconds: float) -> object:
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(module, "run_tree", fake_run_tree)
        port = SpawnLogin(codex_executable=Path(sys.executable).resolve(), timeout_seconds=30.0, grace_seconds=1.0)
        with pytest.raises(AuthSetupError) as stopped:
            port.login(env={}, home=fx.home, evidence_root=fx.evidence)
        assert stopped.value.stage == stage


def test_api_has_no_token_or_argv_injection() -> None:
    names = set(inspect.signature(run_reviewer_auth_setup).parameters)
    assert names.isdisjoint({"api_key", "token", "argv", "codex_command"})
    assert module._platform() == sys.platform
