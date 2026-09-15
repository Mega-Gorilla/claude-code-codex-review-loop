# SPDX-License-Identifier: Apache-2.0
"""C-09 起動facadeのhermetic test（ADR-0027 決定15）。

preflightは固定evidenceを返すfakeへ、C-03の`run_tree`はscenario駆動のfakeへ差し替える。
実Codex・認証・network・実GitHubは使わない。実C-03経由の経路は、interpreterをcodexとして
起動する1 caseで確認する（`exec`をscript名として解釈し、非0で終了する）。
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from claude_code_codex_review_loop.identity import create_private_dir
from claude_code_codex_review_loop.process import (
    Completed,
    SpawnError,
    SpawnSpec,
    StopError,
    StopMethod,
    StopResult,
    TimedOut,
)
from claude_code_codex_review_loop.runtime import codex_launch as module
from claude_code_codex_review_loop.runtime import codex_preflight as preflight_module
from claude_code_codex_review_loop.runtime.codex_canary import prepare_codex_canary_home
from claude_code_codex_review_loop.runtime.codex_launch import (
    MAX_PROMPT_BYTES,
    LaunchError,
    ReviewerCompleted,
    ReviewerTimedOut,
    keyring_target_for_home,
    launch_codex_reviewer,
)
from claude_code_codex_review_loop.runtime.codex_preflight import (
    EXPECTED_BOUNDARIES,
    EXPECTED_CONTROL,
    NETWORK_CONTROL_TARGET,
    AuthState,
    EffectiveSandbox,
    PreflightError,
    PreflightEvidence,
)

_TOKEN = "sk-" + "x" * 40


class FakeReviewer:
    """`run_tree`の差替え。stdinのpromptを検証し、scenarioに従って出力fileを書く。"""

    def __init__(self) -> None:
        self.scenario: dict[str, object] = {
            "exit_code": 0,
            "last_message": "LGTM",
            "stderr": "progress\n",
            "timeout": False,
            "raise": False,
            "skip_stderr": False,
        }
        self.specs: list[SpawnSpec] = []
        self.seen_prompt: str | None = None

    def run_tree(self, spec: SpawnSpec, timeout_seconds: float, grace_seconds: float) -> object:
        self.specs.append(spec)
        if self.scenario["raise"]:
            raise SpawnError("popen", "test")
        if self.scenario.get("stop_error"):
            raise StopError("close", "test")
        assert spec.stdin_path is not None and spec.stdout_path is not None and spec.stderr_path is not None
        self.seen_prompt = spec.stdin_path.read_text(encoding="utf-8")
        spec.stdout_path.write_text("", encoding="utf-8")
        if not self.scenario["skip_stderr"]:
            spec.stderr_path.write_bytes(str(self.scenario["stderr"]).encode("utf-8"))
        if self.scenario["timeout"]:
            return TimedOut(stop_result=StopResult(method=StopMethod.FORCED, graceful_requested=True))
        last_message = self.scenario["last_message"]
        output = Path(spec.argv[spec.argv.index("-o") + 1])
        if self.scenario.get("last_message_dir"):
            output.mkdir()
        elif last_message is not None:
            output.write_bytes(last_message if isinstance(last_message, bytes) else str(last_message).encode("utf-8"))
        return Completed(exit_code=int(str(self.scenario["exit_code"])))


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
        self.fake = FakeReviewer()
        self.preflight_calls = 0
        self.preflight_error: str | None = None
        monkeypatch.setattr(module, "run_tree", self.fake.run_tree)
        monkeypatch.setattr(module, "run_sandbox_preflight", self._fake_preflight)
        self.credential_calls: list[dict[str, object]] = []
        self.credential_error: str | None = None
        monkeypatch.setattr(module, "run_credential_store_probe", self._fake_credential_probe)
        # spawn直前の再測定（`verify_preflight_evidence`）は本物を使い、その内側の測定だけをfakeにする
        monkeypatch.setattr(preflight_module, "run_sandbox_preflight", self._fake_preflight)
        self.second_auth: AuthState | None = None
        # auth gateはWindows native限定（ADR-0031）。testはplatformを固定し、gateの各経路を別testで固定する
        monkeypatch.setattr(module, "_platform", lambda: "win32")
        self.auth = AuthState("ok", "Keyring")
        self.codex_version = "codex-cli 0.154.0"

    def _fake_preflight(self, **kwargs: object) -> PreflightEvidence:
        self.preflight_calls += 1
        assert kwargs["home"] is self.home and kwargs["evidence_root"] == self.evidence_root
        if self.preflight_error:
            raise PreflightError(self.preflight_error)
        if self.preflight_calls >= 2 and self.second_auth is not None:
            # 再測定でauthの状態が変わっていた（例: 1回目は登録済み、2回目は失効）
            self.auth = self.second_auth
        return self.evidence()

    def _fake_credential_probe(self, **kwargs: object) -> dict[str, str]:
        self.credential_calls.append(kwargs)
        if self.credential_error:
            raise PreflightError(self.credential_error)
        return {"control": "present", "sandbox": "absent"}

    def evidence(self) -> PreflightEvidence:
        return PreflightEvidence(
            codex_executable=os.fspath(self.codex_executable),
            codex_version=self.codex_version,
            configuration_digest=self.home.configuration_digest,
            profile_name="c09-canary",
            workspace_root=self.workspace,
            protected_roots=self.home.protected_roots,
            codex_home=self.home.root,
            environment_digest="0" * 64,
            probe_interpreter=os.fspath(self.codex_executable),
            probe_digest="0" * 64,
            network_target=NETWORK_CONTROL_TARGET,
            effective=EffectiveSandbox("Never", "restricted", "restricted", "true", "elevated", "complete"),
            auth=self.auth,
            control=dict(EXPECTED_CONTROL),
            boundaries=dict(EXPECTED_BOUNDARIES),
        )

    @property
    def reviewer_env(self) -> dict[str, str]:
        env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if name in os.environ}
        env["PYTHONUTF8"] = "1"
        return env

    def launch(self, **overrides: object) -> ReviewerCompleted | ReviewerTimedOut:
        values: dict[str, object] = {
            "home": self.home,
            "codex_executable": self.codex_executable,
            "reviewer_env": self.reviewer_env,
            "evidence_root": self.evidence_root,
            "prompt": "review this head\n",
            "timeout_seconds": 60.0,
            "grace_seconds": 1.0,
        }
        values.update(overrides)
        return launch_codex_reviewer(**values)  # type: ignore[arg-type]


@pytest.fixture
def fx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    return Fixture(tmp_path, monkeypatch)


class TestKeyringTarget:
    def test_target_is_account_dot_service_from_the_extended_canonical_path(self, tmp_path: Path) -> None:
        home = (tmp_path / "reviewer-home").resolve()
        target = keyring_target_for_home(home)
        account, _, service = target.rpartition(".")
        assert service == "Codex Auth" and account.startswith("cli|") and len(account) == 4 + 16
        assert keyring_target_for_home(tmp_path / "reviewer-home") == target  # 正規化してから導出
        if sys.platform == "win32":
            # 既に拡張長形式（Rustのcanonicalize()の出力形）でも同じ導出になる。POSIXでは接頭辞が意味を持たない
            assert keyring_target_for_home(Path("\\\\?\\" + os.fspath(home))) == target

    def test_different_homes_have_different_targets(self, tmp_path: Path) -> None:
        assert keyring_target_for_home(tmp_path / "a") != keyring_target_for_home(tmp_path / "b")


def test_platform_reports_the_running_interpreter_platform() -> None:
    assert module._platform() == sys.platform


class TestLaunchCodexReviewer:
    def test_preflight_then_spawn_with_prompt_on_stdin_and_last_message_file(self, fx: Fixture) -> None:
        result = fx.launch(prompt="please review\n")
        assert isinstance(result, ReviewerCompleted)
        assert result.exit_code == 0 and result.last_message == b"LGTM"
        assert result.evidence == fx.evidence()
        assert result.diagnostic.text == "progress\n" and result.diagnostic.hits == ()
        # 測定は2回: 最初のevidence取得と、spawn直前の再測定（ADR-0027 決定14）
        assert fx.preflight_calls == 2 and fx.fake.seen_prompt == "please review\n"
        (spec,) = fx.fake.specs
        assert spec.argv == (
            os.fspath(fx.codex_executable), "exec", "--ephemeral", "--ignore-rules", "-C", os.fspath(fx.workspace),
            "-o", os.fspath(fx.evidence_root / "last_message.txt"), "-",
        )
        assert "please review" not in " ".join(spec.argv)
        assert spec.cwd == fx.workspace
        assert spec.env == {**fx.reviewer_env, "CODEX_HOME": os.fspath(fx.home.root)}
        assert spec.stdin_path == fx.evidence_root / "prompt.txt"
        assert {spec.stdout_path, spec.stderr_path} == {
            fx.evidence_root / "reviewer.stdout", fx.evidence_root / "reviewer.stderr",
        }
        if os.name == "posix":
            assert stat.S_IMODE((fx.evidence_root / "prompt.txt").stat().st_mode) == 0o600

    @pytest.mark.parametrize(
        ("platform", "version", "auth", "stage"),
        (
            ("linux", "codex-cli 0.154.0", AuthState("ok", "Keyring"), "auth_platform"),
            ("darwin", "codex-cli 0.154.0", AuthState("ok", "Keyring"), "auth_platform"),
            ("win32", "codex-cli 0.155.0", AuthState("ok", "Keyring"), "auth_version"),
            ("win32", "codex-cli 0.154.0", AuthState("fail", "Keyring"), "auth"),
            ("win32", "codex-cli 0.154.0", AuthState("ok", "File"), "auth"),
            ("win32", "codex-cli 0.154.0", AuthState("", ""), "auth"),
        ),
        ids=("linux", "darwin", "version", "not_registered", "file_storage", "check_absent"),
    )
    def test_auth_gate_stops_before_the_prompt_file_and_spawn(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch, platform: str, version: str, auth: AuthState, stage: str
    ) -> None:
        """ADR-0031 決定5: Windows native以外・未対応version・未登録 / file保存の認証では起動しない。"""
        monkeypatch.setattr(module, "_platform", lambda: platform)
        fx.codex_version = version
        fx.auth = auth
        with pytest.raises(LaunchError) as stopped:
            fx.launch()
        assert stopped.value.stage == stage
        assert fx.preflight_calls == 2 and fx.fake.specs == [] and not (fx.evidence_root / "prompt.txt").exists()

    @pytest.mark.parametrize(
        "second",
        (AuthState("fail", "Keyring"), AuthState("ok", "File"), AuthState("", "")),
        ids=("revoked", "file_storage", "check_absent"),
    )
    def test_auth_that_changes_between_measurement_and_spawn_is_rejected(self, fx: Fixture, second: AuthState) -> None:
        """ADR-0027 決定14 + ADR-0031 決定5: 再測定で一致しなければ、auth gate以前にevidence_mismatchで止まる。"""
        fx.second_auth = second
        with pytest.raises(PreflightError) as stopped:
            fx.launch()
        assert stopped.value.stage == "evidence_mismatch"
        assert fx.preflight_calls == 2 and fx.fake.specs == [] and not (fx.evidence_root / "prompt.txt").exists()

    def test_re_measurement_failure_prevents_spawn(self, fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def flaky(**kwargs: object) -> PreflightEvidence:
            calls["n"] += 1
            if calls["n"] == 2:
                raise PreflightError("boundary")
            return fx.evidence()

        monkeypatch.setattr(module, "run_sandbox_preflight", flaky)
        monkeypatch.setattr(preflight_module, "run_sandbox_preflight", flaky)
        with pytest.raises(PreflightError) as stopped:
            fx.launch()
        assert stopped.value.stage == "boundary" and fx.fake.specs == []

    def test_credential_store_probe_runs_after_the_auth_gate_and_before_spawn(self, fx: Fixture) -> None:
        """AC-C09-06: 登録済みcredentialへsandbox内から到達できないことを、spawn前にpositive control付きで実測する。"""
        fx.launch()
        (call,) = fx.credential_calls
        assert call["home"] is fx.home and call["evidence_root"] == fx.evidence_root
        assert call["target"] == keyring_target_for_home(fx.home.root)
        assert (call["timeout_seconds"], call["grace_seconds"]) == (60.0, 1.0)

    def test_credential_store_probe_failure_prevents_spawn(self, fx: Fixture) -> None:
        fx.credential_error = "credential_boundary"
        with pytest.raises(PreflightError) as stopped:
            fx.launch()
        assert stopped.value.stage == "credential_boundary"
        assert fx.fake.specs == [] and not (fx.evidence_root / "prompt.txt").exists()

    def test_auth_gate_runs_before_the_credential_store_probe(self, fx: Fixture) -> None:
        fx.auth = AuthState("fail", "Keyring")
        with pytest.raises(LaunchError):
            fx.launch()
        assert fx.credential_calls == []

    def test_preflight_failure_prevents_spawn(self, fx: Fixture) -> None:
        fx.preflight_error = "sandbox_unavailable"
        with pytest.raises(PreflightError) as stopped:
            fx.launch()
        assert stopped.value.stage == "sandbox_unavailable"
        assert fx.fake.specs == [] and not (fx.evidence_root / "prompt.txt").exists()

    @pytest.mark.parametrize(
        "prompt",
        ("", "   \n", "a\x00b", "x" * (MAX_PROMPT_BYTES + 1), "lone " + chr(0xD800), "escaped " + chr(0xDC80)),
        ids=("empty", "blank", "nul", "too_large", "unpaired_high_surrogate", "unpaired_low_surrogate"),
    )
    def test_invalid_prompt_is_rejected_before_preflight(self, fx: Fixture, prompt: str) -> None:
        with pytest.raises(LaunchError) as stopped:
            fx.launch(prompt=prompt)
        assert stopped.value.stage == "prompt"
        assert fx.preflight_calls == 0 and fx.fake.specs == []

    def test_prompt_file_write_failure_is_classified(self, fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            module,
            "write_private_text",
            lambda *args: (_ for _ in ()).throw(module.FsPermissionError("create_file", "test")),
        )
        with pytest.raises(LaunchError) as stopped:
            fx.launch()
        assert stopped.value.stage == "prompt" and fx.fake.specs == []

    def test_token_environment_is_rejected_as_configuration(self, fx: Fixture) -> None:
        with pytest.raises(LaunchError) as stopped:
            fx.launch(reviewer_env={**fx.reviewer_env, "OPENAI_API_KEY": _TOKEN})
        assert stopped.value.stage == "configuration" and fx.fake.specs == []

    def test_spawn_failure_is_classified(self, fx: Fixture) -> None:
        fx.fake.scenario["raise"] = True
        with pytest.raises(LaunchError) as stopped:
            fx.launch()
        assert stopped.value.stage == "spawn"

    def test_timeout_returns_timed_out_with_redacted_diagnostic(self, fx: Fixture) -> None:
        fx.fake.scenario.update(timeout=True, stderr="OPENAI_API_KEY=" + _TOKEN + "\n")
        result = fx.launch()
        assert isinstance(result, ReviewerTimedOut)
        assert _TOKEN not in result.diagnostic.text and result.diagnostic.hits
        assert result.evidence == fx.evidence()

    def test_missing_last_message_is_none_not_an_error(self, fx: Fixture) -> None:
        fx.fake.scenario.update(last_message=None, exit_code=1)
        result = fx.launch()
        assert isinstance(result, ReviewerCompleted)
        assert (result.exit_code, result.last_message) == (1, None)

    def test_diagnostic_is_redacted_and_capped(self, fx: Fixture) -> None:
        fx.fake.scenario["stderr"] = "x" * (module.MAX_DIAGNOSTIC_BYTES + 10) + _TOKEN
        result = fx.launch()
        assert isinstance(result, ReviewerCompleted)
        assert len(result.diagnostic.text) == module.MAX_DIAGNOSTIC_BYTES and _TOKEN not in result.diagnostic.text

    def test_missing_stderr_file_is_classified(self, fx: Fixture) -> None:
        fx.fake.scenario["skip_stderr"] = True
        with pytest.raises(LaunchError) as stopped:
            fx.launch()
        assert stopped.value.stage == "diagnostic"

    def test_preseeded_last_message_is_rejected_before_spawn(self, fx: Fixture) -> None:
        """pre-seedされた古い出力を今回の結果として返さない。spawnもprompt fileも発生しない。"""
        (fx.evidence_root / "last_message.txt").write_bytes(b"stale")
        with pytest.raises(LaunchError) as stopped:
            fx.launch()
        assert stopped.value.stage == "output"
        assert fx.fake.specs == [] and not (fx.evidence_root / "prompt.txt").exists()

    def test_dangling_symlink_at_last_message_path_is_rejected_before_spawn(self, fx: Fixture) -> None:
        """`exists()`がFalseを返すdangling symlinkも「既存のentry」として拒否する（root外への書込を防ぐ）。"""
        link = fx.evidence_root / "last_message.txt"
        try:
            link.symlink_to(fx.tmp_path / "outside" / "stale.txt")
        except OSError:
            pytest.skip("symlinkを作成できない環境")
        with pytest.raises(LaunchError) as stopped:
            fx.launch()
        assert stopped.value.stage == "output"
        assert fx.fake.specs == [] and not (fx.tmp_path / "outside").exists()

    def test_undecidable_output_entry_before_spawn_fails_closed(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spawn前のentry判定が権限等で不能なら、許可せず`output`で止める。生の例外を出さない。"""
        target = fx.evidence_root / "last_message.txt"
        original = Path.lstat

        def undecidable(self: Path, *args: object, **kwargs: object) -> object:
            if self == target:
                raise PermissionError("test")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", undecidable)
        with pytest.raises(LaunchError) as stopped:
            fx.launch()
        assert stopped.value.stage == "output"
        assert fx.fake.specs == [] and not (fx.evidence_root / "prompt.txt").exists()

    def test_unreadable_last_message_is_classified(self, fx: Fixture) -> None:
        """終了後に通常file以外（directory）が現れていれば、今回の生成物とみなさない。"""
        fx.fake.scenario["last_message_dir"] = True
        with pytest.raises(LaunchError) as stopped:
            fx.launch()
        assert stopped.value.stage == "output"

    @pytest.mark.parametrize("failing", ("lstat", "open"))
    def test_last_message_io_failures_are_classified(
        self, fx: Fixture, failing: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """終了後のstat / readのOS errorは`output`へ写し、生の例外を外へ出さない。"""
        target = fx.evidence_root / "last_message.txt"
        original = getattr(Path, failing)

        def failing_call(self: Path, *args: object, **kwargs: object) -> object:
            if self == target and target.exists():
                raise PermissionError("test")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, failing, failing_call)
        with pytest.raises(LaunchError) as stopped:
            fx.launch()
        assert stopped.value.stage == "output"

    def test_last_message_is_raw_bytes_without_decoding(self, fx: Fixture) -> None:
        """不正UTF-8をそのまま搬送する。C-10のutf8 stageが原文で判定できる。"""
        raw = b'{"verdict": "\xff\xfe broken"}'
        fx.fake.scenario["last_message"] = raw
        result = fx.launch()
        assert isinstance(result, ReviewerCompleted) and result.last_message == raw

    @pytest.mark.parametrize("extra", (0, 1, 50))
    def test_last_message_read_is_bounded_at_limit_plus_one(self, fx: Fixture, extra: int) -> None:
        limit = module.MAX_LAST_MESSAGE_BYTES
        fx.fake.scenario["last_message"] = b"m" * (limit + extra)
        result = fx.launch()
        assert isinstance(result, ReviewerCompleted) and result.last_message is not None
        assert len(result.last_message) == min(limit + extra, limit + 1)

    def test_stop_failure_after_timeout_is_classified(self, fx: Fixture) -> None:
        """C-03の`StopError`（native detailを持つ）をfacade外へ出さない。"""
        fx.fake.scenario["stop_error"] = True
        with pytest.raises(LaunchError) as stopped:
            fx.launch()
        assert stopped.value.stage == "stop"

    def test_real_spawn_path_feeds_stdin_through_c03(self, fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
        """fakeを外し、interpreterをcodexとして起動する。`exec`をscriptとして開けず非0で終わる。"""
        from claude_code_codex_review_loop.process import run_tree as real_run_tree

        monkeypatch.setattr(module, "run_tree", real_run_tree)
        result = fx.launch(prompt="ignored by the interpreter\n")
        assert isinstance(result, ReviewerCompleted)
        assert result.exit_code != 0 and result.last_message is None
        assert "exec" in result.diagnostic.text
        assert (fx.evidence_root / "prompt.txt").read_text(encoding="utf-8") == "ignored by the interpreter\n"
