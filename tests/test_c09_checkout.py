# SPDX-License-Identifier: Apache-2.0
"""C-09の独立checkoutを実gitで検証する。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from claude_code_codex_review_loop.identity import build_reviewer_env, prepare_reviewer_home
from claude_code_codex_review_loop.process import Completed, SpawnError
from claude_code_codex_review_loop.runtime import checkout as module
from claude_code_codex_review_loop.runtime.checkout import CheckoutError, create_reviewer_checkout

_TIMEOUT_SECONDS = 30.0
_GRACE_SECONDS = 2.0


def _git() -> tuple[str, ...]:
    command = shutil.which("git")
    assert command is not None
    return (command,)


def _run_git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        [*_git(), "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _source_repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "source"
    repository.mkdir(parents=True)
    _run_git(repository, "init", "--quiet")
    _run_git(repository, "config", "user.name", "test")
    _run_git(repository, "config", "user.email", "test@example.invalid")
    (repository / "tracked.txt").write_text("first", encoding="utf-8")
    _run_git(repository, "add", "tracked.txt")
    _run_git(repository, "commit", "--quiet", "-m", "first")
    first = _run_git(repository, "rev-parse", "HEAD")
    (repository / "tracked.txt").write_text("second", encoding="utf-8")
    _run_git(repository, "commit", "--quiet", "-am", "second")
    return repository.resolve(), first, _run_git(repository, "rev-parse", "HEAD")


def _checkout(tmp_path: Path, source: Path, target_head_sha: str):
    parent = tmp_path / "private"
    parent.mkdir(parents=True)
    home = prepare_reviewer_home(parent, "home")
    return create_reviewer_checkout(
        parent=parent.resolve(),
        source_repository=source,
        target_head_sha=target_head_sha,
        git_command=_git(),
        env=build_reviewer_env(dict(os.environ), home),
        timeout_seconds=_TIMEOUT_SECONDS,
        grace_seconds=_GRACE_SECONDS,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="gitが無い環境")
class TestReviewerCheckout:
    def test_exact_detached_clone_has_no_remote_or_shared_git_directory(self, tmp_path: Path) -> None:
        source, first, _ = _source_repository(tmp_path)
        parent = tmp_path / "private"
        parent.mkdir()
        home = prepare_reviewer_home(parent, "home")
        checkout = create_reviewer_checkout(
            parent=parent.resolve(), source_repository=source, target_head_sha=first, git_command=_git(),
            env=build_reviewer_env(dict(os.environ), home),
            timeout_seconds=_TIMEOUT_SECONDS,
            grace_seconds=_GRACE_SECONDS,
        )
        assert _run_git(checkout.repository, "rev-parse", "HEAD") == first
        assert _run_git(checkout.repository, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
        assert _run_git(checkout.repository, "remote") == ""
        assert checkout.repository / ".git" != source / ".git"
        assert not (checkout.repository / ".git" / "objects" / "info" / "alternates").exists()
        assert (checkout.repository / "tracked.txt").read_text(encoding="utf-8") == "first"
        assert checkout.release().dirty is False

    def test_temporary_write_is_observed_and_checkout_is_removed(self, tmp_path: Path) -> None:
        source, _, head = _source_repository(tmp_path / "source")
        checkout = _checkout(tmp_path / "checkout", source, head)
        (checkout.repository / "reviewer-temp.txt").write_text("allowed here", encoding="utf-8")
        released = checkout.release()
        assert released.dirty is True
        assert released.removed is True
        assert not checkout.root.exists()

    def test_two_checkouts_do_not_share_review_state(self, tmp_path: Path) -> None:
        source, _, head = _source_repository(tmp_path / "source")
        first = _checkout(tmp_path / "one", source, head)
        (first.repository / "prior-review-state").write_text("not shared", encoding="utf-8")
        first.release()
        second = _checkout(tmp_path / "two", source, head)
        assert not (second.repository / "prior-review-state").exists()
        second.release()

    def test_unknown_head_is_rejected_and_private_root_is_cleaned(self, tmp_path: Path) -> None:
        source, _, _ = _source_repository(tmp_path)
        parent = tmp_path / "private"
        parent.mkdir()
        home = prepare_reviewer_home(parent, "home")
        with pytest.raises(CheckoutError) as stopped:
            create_reviewer_checkout(
                parent=parent.resolve(), source_repository=source, target_head_sha="f" * 40, git_command=_git(),
                env=build_reviewer_env(dict(os.environ), home),
                timeout_seconds=_TIMEOUT_SECONDS,
                grace_seconds=_GRACE_SECONDS,
            )
        assert stopped.value.stage == "checkout"
        assert list(parent.glob("reviewer-checkout-*")) == []


class TestCheckoutRejections:
    @pytest.mark.parametrize(
        ("parent", "source", "head", "command", "stage"),
        [
            (Path("relative"), None, "a" * 40, _git(), "parent"),
            (Path("missing-parent"), None, "a" * 40, _git(), "parent"),
            (None, Path("relative"), "a" * 40, _git(), "source_repository"),
            (None, None, "a" * 40, (), "git_command"),
            (None, None, "not-a-sha", _git(), "target_head_sha"),
        ],
    )
    def test_invalid_inputs_are_rejected(
        self, tmp_path: Path, parent: Path | None, source: Path | None, head: str, command: tuple[str, ...], stage: str
    ) -> None:
        valid_source, _, _ = _source_repository(tmp_path)
        actual_parent = tmp_path.resolve() if parent is None else parent
        if parent == Path("missing-parent"):
            actual_parent = (tmp_path / parent).resolve()
        actual_source = valid_source if source is None else source
        with pytest.raises(CheckoutError) as stopped:
            module._validate_inputs(actual_parent, actual_source, head, command)
        assert stopped.value.stage == stage

    def test_missing_source_and_relative_command_are_rejected(self, tmp_path: Path) -> None:
        missing = (tmp_path / "missing").resolve()
        with pytest.raises(CheckoutError) as stopped:
            module._validate_inputs(tmp_path.resolve(), missing, "a" * 40, _git())
        assert stopped.value.stage == "source_repository"
        source, _, _ = _source_repository(tmp_path / "source")
        with pytest.raises(CheckoutError) as stopped:
            module._validate_inputs(tmp_path.resolve(), source, "a" * 40, ("git",))
        assert stopped.value.stage == "git_command"

    def test_git_output_converts_spawn_exit_and_read_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checkout = module.ReviewerCheckout(tmp_path, tmp_path, "a" * 40, _git(), {}, 1.0, 1.0)
        monkeypatch.setattr(module, "run_tree", lambda *args, **kwargs: Completed(exit_code=1))
        with pytest.raises(CheckoutError) as stopped:
            module._git_output(checkout, "exit", "status")
        assert stopped.value.stage == "exit"
        monkeypatch.setattr(module, "run_tree", lambda *args, **kwargs: (_ for _ in ()).throw(SpawnError("x", "x")))
        with pytest.raises(CheckoutError) as stopped:
            module._git_output(checkout, "spawn", "status")
        assert stopped.value.stage == "spawn"
        monkeypatch.setattr(module, "run_tree", lambda *args, **kwargs: Completed(exit_code=0))
        monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
        with pytest.raises(CheckoutError) as stopped:
            module._git_output(checkout, "read", "status")
        assert stopped.value.stage == "read"

    @pytest.mark.parametrize(
        ("stage", "outputs"),
        [
            ("head", ("b" * 40,)),
            ("detached", ("a" * 40, "main")),
            ("remote", ("a" * 40, "HEAD", "origin")),
        ],
    )
    def test_checkout_verification_rejects_mismatched_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, outputs: tuple[str, ...]
    ) -> None:
        git_dir = tmp_path / "repository" / ".git" / "objects" / "info"
        git_dir.mkdir(parents=True)
        checkout = module.ReviewerCheckout(tmp_path, tmp_path / "repository", "a" * 40, _git(), {}, 1.0, 1.0)
        monkeypatch.setattr(module, "_verify_checkout_root", lambda *args: None)
        iterator = iter(outputs)
        monkeypatch.setattr(module, "_git_output", lambda *args: next(iterator))
        with pytest.raises(CheckoutError) as stopped:
            module._verify_checkout(checkout)
        assert stopped.value.stage == stage

    def test_checkout_verification_rejects_missing_git_alternates_and_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checkout = module.ReviewerCheckout(tmp_path, tmp_path / "repository", "a" * 40, _git(), {}, 1.0, 1.0)
        verify_root = module._verify_checkout_root
        monkeypatch.setattr(module, "_verify_checkout_root", lambda *args: None)
        with pytest.raises(CheckoutError) as stopped:
            module._verify_checkout(checkout)
        assert stopped.value.stage == "git_directory"
        alternates = checkout.repository / ".git" / "objects" / "info" / "alternates"
        alternates.parent.mkdir(parents=True)
        alternates.write_text("outside", encoding="utf-8")
        with pytest.raises(CheckoutError) as stopped:
            module._verify_checkout(checkout)
        assert stopped.value.stage == "alternates"
        with pytest.raises(CheckoutError) as stopped:
            verify_root(tmp_path, tmp_path)
        assert stopped.value.stage == "checkout_path"

    def test_release_and_remove_errors_are_structured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        checkout = module.ReviewerCheckout(tmp_path, tmp_path / "repository", "a" * 40, _git(), {}, 1.0, 1.0)
        remove_tree = module._remove_tree
        monkeypatch.setattr(module, "_verify_checkout_root", lambda *args: None)
        monkeypatch.setattr(module, "_git_output", lambda *args: "")
        monkeypatch.setattr(module, "_remove_tree", lambda *args: (_ for _ in ()).throw(OSError()))
        with pytest.raises(CheckoutError) as stopped:
            checkout.release()
        assert stopped.value.stage == "remove"

        def fail(path: str) -> None:
            raise OSError(path)

        def rmtree(root: Path, *, onerror):
            onerror(fail, str(root), (OSError, OSError(), None))

        monkeypatch.setattr(module, "_remove_tree", remove_tree)
        monkeypatch.setattr(module.shutil, "rmtree", rmtree)
        with pytest.raises(CheckoutError) as stopped:
            module._remove_tree(tmp_path)
        assert stopped.value.stage == "remove"
