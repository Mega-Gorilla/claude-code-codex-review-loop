# SPDX-License-Identifier: Apache-2.0
"""C-09 reviewer turn adapter（checkout → prompt → launch → head binding）のtest。

checkoutは実gitで作り、起動facadeはfakeへ差し替える（facadeの実挙動は`test_c09_codex_launch.py`が
固定する）。実Codex・実GitHub・実`~/.codex`は使わない。
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import MISSING, fields
from pathlib import Path

import pytest

from claude_code_codex_review_loop.identity import create_private_dir
from claude_code_codex_review_loop.policy.redaction import TOKEN_ENV_NAMES, redact
from claude_code_codex_review_loop.runtime import reviewer_turn as module
from claude_code_codex_review_loop.runtime.checkout import CheckoutError
from claude_code_codex_review_loop.runtime.codex_launch import LaunchError, ReviewerCompleted, ReviewerTimedOut
from claude_code_codex_review_loop.runtime.codex_preflight import (
    EXPECTED_BOUNDARIES,
    EXPECTED_CONTROL,
    NETWORK_CONTROL_TARGET,
    PROBE_DIGEST,
    EffectiveSandbox,
    PreflightError,
    PreflightEvidence,
)
from claude_code_codex_review_loop.runtime.codex_provisioning import MARKER_RELATIVE_PATH, USERS_RELATIVE_PATH
from claude_code_codex_review_loop.runtime.head_binding import HeadMismatch, HeadsBound
from claude_code_codex_review_loop.runtime.ports import PortUnavailableError
from claude_code_codex_review_loop.runtime.review_prompt import GitHubText, ReviewContext
from claude_code_codex_review_loop.runtime.reviewer_turn import (
    AdvertisedHeadPort,
    ReportHeadPort,
    ReviewerTurn,
    ReviewerTurnRequest,
    TurnError,
    UnavailableAdvertisedHead,
    UnavailableReportHead,
    run_reviewer_turn,
)

_SHA = re.compile(r"[0-9a-f]{40}")


def _git() -> tuple[str, ...]:
    command = shutil.which("git")
    assert command is not None
    return (command,)


def _run_git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        [*_git(), "-C", str(repository), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *arguments],
        check=True, capture_output=True, encoding="utf-8",
    )
    return completed.stdout.strip()


def _source_repository(tmp_path: Path) -> tuple[Path, str, str]:
    source = (tmp_path / "source").resolve()
    source.mkdir()
    _run_git(source, "init", "-q", "-b", "main")
    (source / "README.md").write_text("one\n", encoding="utf-8")
    _run_git(source, "add", "README.md")
    _run_git(source, "commit", "-q", "-m", "one")
    first = _run_git(source, "rev-parse", "HEAD")
    (source / "README.md").write_text("two\n", encoding="utf-8")
    _run_git(source, "commit", "-q", "-am", "two")
    second = _run_git(source, "rev-parse", "HEAD")
    _run_git(source, "checkout", "-q", "--detach", first)
    return source, first, second


def _context(head: str, **overrides: object) -> ReviewContext:
    values: dict[str, object] = {
        "repository": "octo/repo",
        "number": 12,
        "target_head_sha": head,
        "base_ref": "main",
        "title": "feat: something",
        "round": 1,
        "instructions": "差分をreviewし、blocking findingを列挙する。",
        "materials": (GitHubText("pr_body", "someone", "本文"),),
    }
    values.update(overrides)
    return ReviewContext(**values)  # type: ignore[arg-type]


class ReportHeadFromMessage:
    """最終messageの最初の40桁hexを対象headとして返すfake port。"""

    def reported_head(self, last_message: bytes | None) -> str | None:
        if last_message is None:
            return None
        found = _SHA.search(last_message.decode("utf-8", "replace"))
        return found.group(0) if found else None


class AdvertisedHeadSequence:
    """呼出ごとに与えた値を順に返すfake port（最後の値を繰り返す）。呼出の引数も記録する。"""

    def __init__(self, *heads: str) -> None:
        self.heads = list(heads)
        self.calls: list[tuple[str, int]] = []

    def advertised_head(self, *, repository: str, number: int) -> str:
        self.calls.append((repository, number))
        return self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]


class FakeLaunch:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.outcome: str = "completed"
        self.error: Exception | None = None
        self.on_launch = None

    def __call__(self, **kwargs: object) -> ReviewerCompleted | ReviewerTimedOut:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.on_launch is not None:
            self.on_launch(kwargs)
        home = kwargs["home"]
        evidence = PreflightEvidence(
            codex_executable=os.fspath(kwargs["codex_executable"]),  # type: ignore[arg-type]
            codex_version="codex-cli 0.0.0-fake",
            configuration_digest=home.configuration_digest,  # type: ignore[attr-defined]
            profile_name="c09-canary",
            workspace_root=home.workspace_root,  # type: ignore[attr-defined]
            protected_roots=home.protected_roots,  # type: ignore[attr-defined]
            codex_home=home.root,  # type: ignore[attr-defined]
            environment_digest="0" * 64,
            probe_interpreter=sys.executable,
            probe_digest=PROBE_DIGEST,
            network_target=NETWORK_CONTROL_TARGET,
            effective=EffectiveSandbox("Never", "restricted", "restricted", "true", "elevated", "complete"),
            control=dict(EXPECTED_CONTROL),
            boundaries=dict(EXPECTED_BOUNDARIES),
        )
        if self.outcome == "timed_out":
            return ReviewerTimedOut(diagnostic=redact(""), evidence=evidence)
        head = _run_git(home.workspace_root, "rev-parse", "HEAD")  # type: ignore[attr-defined]
        return ReviewerCompleted(
            exit_code=0, last_message=f"reviewed head {head}\n".encode(), diagnostic=redact(""),
            evidence=evidence,
        )


class Fixture:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.source, self.first, self.second = _source_repository(tmp_path)
        self.run_root = (tmp_path / "run").resolve()
        create_private_dir(self.run_root)
        self.state_root = (tmp_path / "state").resolve()
        self.state_root.mkdir()
        self.launch = FakeLaunch()
        monkeypatch.setattr(module, "launch_codex_reviewer", self.launch)
        monkeypatch.setattr(module, "_platform", lambda: "linux")

    @property
    def base_env(self) -> dict[str, str]:
        return {name: value for name, value in os.environ.items() if name.upper() not in TOKEN_ENV_NAMES}

    def request(self, **overrides: object) -> ReviewerTurnRequest:
        values: dict[str, object] = {
            "source_repository": self.source,
            "advertised_head": self.first,
            "context": _context(self.first),
            "run_root": self.run_root,
            "protected_roots": (self.state_root,),
            "git_command": _git(),
            "codex_executable": Path(sys.executable).resolve(),
            "base_env": self.base_env,
            "provisioning_source": None,
            "git_timeout_seconds": 60.0,
            "git_grace_seconds": 2.0,
            "reviewer_timeout_seconds": 60.0,
            "reviewer_grace_seconds": 2.0,
        }
        values.update(overrides)
        return ReviewerTurnRequest(**values)  # type: ignore[arg-type]

    def run(
        self,
        report_head: ReportHeadPort | None = None,
        advertised: AdvertisedHeadPort | None = None,
        **overrides: object,
    ) -> ReviewerTurn:
        self.advertised = advertised or AdvertisedHeadSequence(self.first)
        return run_reviewer_turn(
            self.request(**overrides),
            report_head=report_head or ReportHeadFromMessage(),
            advertised_head=self.advertised,
        )

    def leftover_checkouts(self) -> list[Path]:
        return [path for path in self.run_root.iterdir() if path.name.startswith("reviewer-checkout-")]

    def provisioning_source(self) -> Path:
        source = (self.tmp_path / "authoritative").resolve()
        (source / MARKER_RELATIVE_PATH).parent.mkdir(parents=True)
        (source / USERS_RELATIVE_PATH).parent.mkdir()
        (source / MARKER_RELATIVE_PATH).write_text(json.dumps({
            "version": 5, "offline_username": "CodexSandboxOffline", "online_username": "CodexSandboxOnline",
            "proxy_ports": [], "allow_local_binding": False,
        }), encoding="utf-8")
        users = {"version": 5, "offline": {}, "online": {}}
        (source / USERS_RELATIVE_PATH).write_text(json.dumps(users), encoding="utf-8")
        return source


@pytest.fixture
def fx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    return Fixture(tmp_path, monkeypatch)


class TestRunReviewerTurn:
    def test_composes_the_parts_in_fixed_order_and_binds_the_head(self, fx: Fixture) -> None:
        turn = fx.run()
        assert turn.binding == HeadsBound(fx.first) and turn.observed_head == fx.first
        assert turn.advertised_head_after == fx.first
        # advertised headはreview終了後（投稿判断の直前）にGitHubから取り直す
        assert fx.advertised.calls == [("octo/repo", 12)]  # type: ignore[attr-defined]
        assert turn.release.dirty is False and turn.provisioning is None
        assert len(turn.prompt_boundary) == 32 and turn.redaction_hits == 0
        assert isinstance(turn.launch, ReviewerCompleted)
        assert turn.evidence_root == fx.run_root / "evidence" and turn.evidence_root.is_dir()
        # 起動facadeへ渡したもの: workspaceは隔離checkout、protectedは実repositoryと呼出側のroot、promptはfence付き
        (call,) = fx.launch.calls
        home = call["home"]
        assert home.workspace_root.parent.parent == fx.run_root  # type: ignore[attr-defined]
        assert home.workspace_root.parent.name.startswith("reviewer-checkout-")  # type: ignore[attr-defined]
        assert home.protected_roots[:-1] == tuple(sorted((fx.source, fx.state_root), key=str))  # type: ignore[attr-defined]
        assert home.root == fx.run_root / "codex-home"  # type: ignore[attr-defined]
        prompt = call["prompt"]
        assert isinstance(prompt, str) and f"<<<GITHUB_DATA:{turn.prompt_boundary}" in prompt and fx.first in prompt
        env = call["reviewer_env"]
        assert isinstance(env, dict) and "CODEX_HOME" not in env and not (set(env) & set(TOKEN_ENV_NAMES))
        assert env["HOME"].startswith(os.fspath(fx.run_root / "reviewer-home"))
        assert call["evidence_root"] == fx.run_root / "evidence"
        assert (call["timeout_seconds"], call["grace_seconds"]) == (60.0, 2.0)
        # checkoutは破棄済み。専用home・evidenceは呼出側が所有するrun_rootに残る
        assert fx.leftover_checkouts() == []
        assert sorted(path.name for path in fx.run_root.iterdir()) == ["codex-home", "evidence", "reviewer-home"]

    def test_real_repository_is_a_denied_root_of_the_sandbox_profile(self, fx: Fixture) -> None:
        """AC-C06-03 / AC-C09-02の配線: 実repositoryは専用profileのdeny rootで、preflightの境界probeが
        その書込拒否を実測できなければreviewerは起動しない（実sandboxでの実測はADR-0027 追補(3)）。
        """
        fx.run()
        home = fx.launch.calls[0]["home"]
        with home.config_path.open("rb") as handle:  # type: ignore[attr-defined]
            config = tomllib.load(handle)
        filesystem = config["permissions"]["c09-canary"]["filesystem"]
        assert filesystem[os.fspath(fx.source)] == "deny" and filesystem[os.fspath(fx.state_root)] == "deny"
        assert fx.source in home.protected_roots and fx.state_root in home.protected_roots  # type: ignore[attr-defined]

    def test_second_turn_on_the_same_head_is_independent(self, fx: Fixture, tmp_path: Path) -> None:
        """AC-C09-03: 同一headへの2回目のturnは前回のcheckout・home・promptに依存しない。"""
        first = fx.run()
        other_root = (tmp_path / "run2").resolve()
        create_private_dir(other_root)
        second = fx.run(run_root=other_root)
        assert first.binding == second.binding == HeadsBound(fx.first)
        assert first.prompt_boundary != second.prompt_boundary
        assert fx.launch.calls[0]["home"].root != fx.launch.calls[1]["home"].root  # type: ignore[attr-defined]

    def test_mirrors_provisioning_artifacts_only_on_windows(self, fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
        source = fx.provisioning_source()
        assert fx.run(provisioning_source=source).provisioning is None
        monkeypatch.setattr(module, "_platform", lambda: "win32")
        fx.launch.calls.clear()
        shutil.rmtree(fx.run_root / "codex-home")
        shutil.rmtree(fx.run_root / "reviewer-home")
        shutil.rmtree(fx.run_root / "evidence")
        turn = fx.run(provisioning_source=source)
        assert turn.provisioning is not None and turn.provisioning.source == source
        home = fx.launch.calls[0]["home"]
        assert (home.root / MARKER_RELATIVE_PATH).is_file() and (home.root / USERS_RELATIVE_PATH).is_file()  # type: ignore[attr-defined]

    def test_windows_without_a_provisioning_source_does_not_mirror(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(module, "_platform", lambda: "win32")
        turn = fx.run(provisioning_source=None)
        assert turn.provisioning is None
        home = fx.launch.calls[0]["home"]
        assert not (home.root / ".sandbox").exists()  # type: ignore[attr-defined]

    def test_timed_out_reviewer_has_no_reported_head_and_is_a_mismatch(self, fx: Fixture) -> None:
        fx.launch.outcome = "timed_out"
        turn = fx.run()
        assert isinstance(turn.launch, ReviewerTimedOut)
        assert isinstance(turn.binding, HeadMismatch) and turn.binding.reasons == ("reported_invalid",)
        assert fx.leftover_checkouts() == []

    def test_head_moved_inside_the_checkout_during_review_is_detected(self, fx: Fixture) -> None:
        """AC-C09-04: 観測は作成時の値ではなく実際のHEADで、review中に動けば一致しない。"""

        def move(kwargs: dict[str, object]) -> None:
            _run_git(kwargs["home"].workspace_root, "checkout", "-q", "--detach", fx.second)  # type: ignore[attr-defined]

        fx.launch.on_launch = move
        turn = fx.run()
        assert turn.observed_head == fx.second
        assert isinstance(turn.binding, HeadMismatch) and turn.binding.checkout_head == fx.second
        assert turn.binding.reasons == ("advertised_moved",)

    def test_push_to_the_pull_request_during_review_is_detected(self, fx: Fixture) -> None:
        """AC-C09-04: review中にPRへpushされると、checkoutとreportが一致してもadvertised headが動いている。"""
        turn = fx.run(advertised=AdvertisedHeadSequence(fx.second))
        assert turn.observed_head == fx.first and turn.advertised_head_after == fx.second
        assert isinstance(turn.binding, HeadMismatch) and turn.binding.reasons == ("advertised_moved",)
        assert turn.binding.advertised_head == fx.second and fx.leftover_checkouts() == []

    def test_advertised_head_is_read_after_the_review_not_before(self, fx: Fixture) -> None:
        """portの読取は起動の後。起動中にportの値がold→newへ変われば、newで照合する。"""
        port = AdvertisedHeadSequence(fx.first)

        def push(kwargs: dict[str, object]) -> None:
            assert port.calls == []
            port.heads = [fx.second]

        fx.launch.on_launch = push
        turn = fx.run(advertised=port)
        assert isinstance(turn.binding, HeadMismatch) and turn.binding.reasons == ("advertised_moved",)

    def test_report_that_names_another_head_is_a_mismatch(self, fx: Fixture) -> None:
        class Other:
            def reported_head(self, last_message: bytes | None) -> str | None:
                return "b" * 40

        turn = fx.run(report_head=Other())
        assert isinstance(turn.binding, HeadMismatch) and turn.binding.reasons == ("reported_differs",)

    def test_dirty_checkout_is_reported_not_rejected(self, fx: Fixture) -> None:
        """AC-C09-01: 隔離checkout内の一時書込は許可され、dirty stateはevidenceになる。"""

        def write(kwargs: dict[str, object]) -> None:
            (kwargs["home"].workspace_root / "scratch.txt").write_text("tmp", encoding="utf-8")  # type: ignore[attr-defined]

        fx.launch.on_launch = write
        turn = fx.run()
        assert turn.release.dirty is True and turn.binding == HeadsBound(fx.first)
        assert fx.leftover_checkouts() == []


class TestFailClosed:
    def test_advertised_head_that_moved_before_start_stops_before_any_checkout(self, fx: Fixture) -> None:
        with pytest.raises(TurnError) as stopped:
            fx.run(advertised_head=fx.second)
        assert stopped.value.stage == "head:advertised_moved"
        assert fx.launch.calls == [] and fx.leftover_checkouts() == []
        assert not (fx.run_root / "reviewer-home").exists()

    def test_prompt_error_stops_before_any_checkout(self, fx: Fixture) -> None:
        with pytest.raises(TurnError) as stopped:
            fx.run(context=_context(fx.first, base_ref="main branch"))
        assert stopped.value.stage == "prompt:base_ref"
        assert fx.launch.calls == [] and fx.leftover_checkouts() == []

    def test_unknown_head_fails_in_checkout_and_leaves_nothing(self, fx: Fixture) -> None:
        head = "c" * 40
        with pytest.raises(TurnError) as stopped:
            fx.run(advertised_head=head, context=_context(head))
        assert stopped.value.stage.startswith("checkout:")
        assert fx.launch.calls == [] and fx.leftover_checkouts() == []

    @pytest.mark.parametrize(
        ("error", "stage"),
        ((PreflightError("boundary"), "preflight:boundary"), (LaunchError("spawn"), "launch:spawn")),
    )
    def test_launch_failures_are_mapped_and_the_checkout_is_released(
        self, fx: Fixture, error: Exception, stage: str
    ) -> None:
        fx.launch.error = error
        with pytest.raises(TurnError) as stopped:
            fx.run()
        assert stopped.value.stage == stage
        assert fx.leftover_checkouts() == []

    def test_observe_failure_after_launch_is_mapped(self, fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            module, "observe_checkout_head", lambda checkout: (_ for _ in ()).throw(CheckoutError("head"))
        )
        with pytest.raises(TurnError) as stopped:
            fx.run()
        assert stopped.value.stage == "checkout:head" and fx.leftover_checkouts() == []

    def test_release_failure_after_a_successful_review_is_reported(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            module.ReviewerCheckout, "release", lambda self: (_ for _ in ()).throw(CheckoutError("remove"))
        )
        with pytest.raises(TurnError) as stopped:
            fx.run()
        assert stopped.value.stage == "checkout:release"

    def test_release_failure_does_not_replace_the_original_failure(
        self, fx: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fx.launch.error = PreflightError("boundary")
        monkeypatch.setattr(
            module.ReviewerCheckout, "release", lambda self: (_ for _ in ()).throw(CheckoutError("remove"))
        )
        with pytest.raises(TurnError) as stopped:
            fx.run()
        assert stopped.value.stage == "preflight:boundary"

    def test_report_head_port_is_fail_closed_until_c10(self, fx: Fixture) -> None:
        port: ReportHeadPort = UnavailableReportHead()
        with pytest.raises(PortUnavailableError):
            fx.run(report_head=port)
        assert fx.leftover_checkouts() == []

    def test_advertised_head_port_is_fail_closed_until_c10(self, fx: Fixture) -> None:
        port: AdvertisedHeadPort = UnavailableAdvertisedHead()
        with pytest.raises(PortUnavailableError):
            fx.run(advertised=port)
        assert fx.leftover_checkouts() == []

    @pytest.mark.parametrize("kind", ("equals_state_root", "inside_source", "contains_state_root"))
    def test_run_root_overlapping_a_protected_root_is_rejected_before_anything_is_created(
        self, fx: Fixture, kind: str
    ) -> None:
        """保護対象と重なるrun_rootは、reviewer homeやcheckoutを作る前に拒否し、保護対象を変えない。"""
        if kind == "equals_state_root":
            run_root = fx.state_root
        elif kind == "inside_source":
            run_root = fx.source / "run"
        else:
            run_root = fx.state_root.parent
        create_private_dir(run_root) if not run_root.exists() else None
        before = {
            root: sorted(os.fspath(p) for p in root.rglob("*")) for root in (fx.source, fx.state_root)
        }
        with pytest.raises(TurnError) as stopped:
            fx.run(run_root=run_root)
        assert stopped.value.stage == "run_root" and fx.launch.calls == []
        after = {root: sorted(os.fspath(p) for p in root.rglob("*")) for root in (fx.source, fx.state_root)}
        assert after == before
        assert not (run_root / "reviewer-home").exists()

    def test_canary_home_rejection_is_mapped(self, fx: Fixture) -> None:
        with pytest.raises(TurnError) as stopped:
            fx.run(protected_roots=(fx.state_root, fx.state_root))  # 重複はcanary homeが拒否する
        assert stopped.value.stage == "canary:protected_root" and fx.leftover_checkouts() == []

    def test_provisioning_rejection_is_mapped(self, fx: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(module, "_platform", lambda: "win32")
        with pytest.raises(TurnError) as stopped:
            fx.run(provisioning_source=(fx.tmp_path / "missing").resolve())
        assert stopped.value.stage == "provisioning:source" and fx.leftover_checkouts() == []

    def test_evidence_root_that_cannot_be_created_is_mapped(self, fx: Fixture) -> None:
        (fx.run_root / "evidence").write_text("not a dir", encoding="utf-8")
        with pytest.raises(TurnError) as stopped:
            fx.run()
        assert stopped.value.stage == "evidence_root" and fx.leftover_checkouts() == []

    @pytest.mark.parametrize("kind", ("relative", "missing", "not_private"))
    def test_invalid_run_root_is_rejected(self, fx: Fixture, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
        roots = {
            "relative": Path("relative"), "missing": (fx.tmp_path / "missing").resolve(), "not_private": fx.run_root,
        }
        if kind == "not_private":
            monkeypatch.setattr(
                module,
                "verify_private_dir",
                lambda path: (_ for _ in ()).throw(module.FsPermissionError("verify", "t")),
            )
        with pytest.raises(TurnError) as stopped:
            fx.run(run_root=roots[kind])
        assert stopped.value.stage == "run_root" and fx.launch.calls == []

    def test_token_in_base_env_never_reaches_git_or_the_reviewer(self, fx: Fixture) -> None:
        """C-06のenv契約: 実行基盤の変数だけを複写し、token変数はreviewer envへ到達しない。"""
        turn = fx.run(base_env={**fx.base_env, "OPENAI_API_KEY": "sk-" + "x" * 40})
        assert turn.binding == HeadsBound(fx.first)
        env = fx.launch.calls[0]["reviewer_env"]
        assert isinstance(env, dict) and "OPENAI_API_KEY" not in env

    def test_preexisting_reviewer_home_is_rejected(self, fx: Fixture) -> None:
        (fx.run_root / "reviewer-home").mkdir()
        with pytest.raises(TurnError) as stopped:
            fx.run()
        assert stopped.value.stage == "reviewer_home" and fx.leftover_checkouts() == []


def test_platform_reports_the_running_interpreter_platform() -> None:
    assert module._platform() == sys.platform


def test_request_has_no_defaults_and_api_takes_no_argv_or_probe_injection() -> None:
    assert all(
        field.default is MISSING and field.default_factory is MISSING for field in fields(ReviewerTurnRequest)
    )
    names = set(inspect.signature(run_reviewer_turn).parameters)
    assert names == {"request", "report_head", "advertised_head"}
    assert not {name for name in dir(module) if "argv" in name.lower() or "probe" in name.lower()}
