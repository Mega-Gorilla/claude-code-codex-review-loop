# SPDX-License-Identifier: Apache-2.0
"""C-09 reviewer用の独立checkoutを作成・破棄する。

reviewerは実repositoryをworktreeとして共有しない。reviewごとにprivateな親directory
の下へ独立cloneを作り、指定SHAへdetached checkoutした後、originを削除する。
このmoduleはCodexを起動せず、promptや認証材料も受け取らない。native adapterは後続の
責務であり、ここで保証するのはreviewerの作業treeと実repositoryの分離だけである。
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..identity.fs_permissions import create_private_dir, verify_private_dir
from ..policy.permission_profile import ensure_argv_allowed
from ..process import Completed, SpawnError, SpawnSpec, run_tree

_SHA256 = re.compile(r"[0-9a-f]{40}")


class CheckoutError(Exception):
    """隔離checkoutの作成・検証に失敗した。詳細出力は公開しない。"""

    def __init__(self, stage: str) -> None:
        super().__init__(f"checkout_error: {stage}")
        self.stage = stage


@dataclass(frozen=True)
class CheckoutRelease:
    """破棄直前の観測結果。dirtyはreviewerの一時書込が残ったことを表す。"""

    dirty: bool
    removed: bool


@dataclass(frozen=True)
class ReviewerCheckout:
    """1 review turnだけで使う、独立したdetached checkout。"""

    root: Path
    repository: Path
    target_head_sha: str
    git_command: tuple[str, ...]
    env: Mapping[str, str]
    timeout_seconds: float
    grace_seconds: float

    def release(self) -> CheckoutRelease:
        """dirty stateを観測してから、自分が作成したrootだけを破棄する。"""
        _verify_checkout_root(self.root, self.repository)
        dirty = _git_output(self, "status", "-C", str(self.repository), "status", "--porcelain") != ""
        try:
            _remove_tree(self.root)
        except OSError as error:
            raise CheckoutError("remove") from error
        return CheckoutRelease(dirty=dirty, removed=not self.root.exists())


def create_reviewer_checkout(
    *,
    parent: Path,
    source_repository: Path,
    target_head_sha: str,
    git_command: tuple[str, ...],
    env: Mapping[str, str],
    timeout_seconds: float,
    grace_seconds: float,
) -> ReviewerCheckout:
    """sourceからremoteを持たないdetached cloneを作成し、不変条件を検証する。"""
    _validate_inputs(parent, source_repository, target_head_sha, git_command)
    root = parent / f"reviewer-checkout-{uuid.uuid4().hex}"
    repository = root / "repository"
    create_private_dir(root)
    checkout = ReviewerCheckout(
        root=root,
        repository=repository,
        target_head_sha=target_head_sha,
        git_command=git_command,
        env=env,
        timeout_seconds=timeout_seconds,
        grace_seconds=grace_seconds,
    )
    try:
        _git_output(
            checkout, "clone", "clone", "--no-local", "--no-checkout", "--", str(source_repository), str(repository)
        )
        _git_output(checkout, "checkout", "-C", str(repository), "checkout", "--detach", target_head_sha)
        _git_output(checkout, "remove_remote", "-C", str(repository), "remote", "remove", "origin")
        _verify_checkout(checkout)
    except (CheckoutError, OSError):
        _remove_tree(root)
        raise
    return checkout


def _validate_inputs(parent: Path, source_repository: Path, target_head_sha: str, git_command: tuple[str, ...]) -> None:
    if not parent.is_absolute() or parent != parent.resolve():
        raise CheckoutError("parent")
    if not parent.is_dir():
        raise CheckoutError("parent")
    if not source_repository.is_absolute() or source_repository != source_repository.resolve():
        raise CheckoutError("source_repository")
    if not (source_repository / ".git").is_dir():
        raise CheckoutError("source_repository")
    if _SHA256.fullmatch(target_head_sha) is None:
        raise CheckoutError("target_head_sha")
    if not git_command or not os.path.isabs(git_command[0]):
        raise CheckoutError("git_command")


def _git_output(checkout: ReviewerCheckout, stage: str, *arguments: str) -> str:
    """gitをexplicit envで実行し、成功時だけUTF-8出力を返す。"""
    ensure_argv_allowed((*checkout.git_command, *arguments))
    stdout_path = checkout.root / f"{stage}.stdout"
    stderr_path = checkout.root / f"{stage}.stderr"
    spec = SpawnSpec(
        argv=(*checkout.git_command, *arguments),
        cwd=checkout.root,
        env=checkout.env,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    try:
        outcome = run_tree(spec, timeout_seconds=checkout.timeout_seconds, grace_seconds=checkout.grace_seconds)
    except SpawnError as error:
        raise CheckoutError(stage) from error
    if not isinstance(outcome, Completed) or outcome.exit_code != 0:
        raise CheckoutError(stage)
    try:
        return stdout_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise CheckoutError(stage) from error


def _verify_checkout(checkout: ReviewerCheckout) -> None:
    _verify_checkout_root(checkout.root, checkout.repository)
    git_dir = checkout.repository / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise CheckoutError("git_directory")
    alternates = git_dir / "objects" / "info" / "alternates"
    if os.path.lexists(alternates):
        raise CheckoutError("alternates")
    if _git_output(checkout, "head", "-C", str(checkout.repository), "rev-parse", "HEAD") != checkout.target_head_sha:
        raise CheckoutError("head")
    if _git_output(checkout, "detached", "-C", str(checkout.repository), "rev-parse", "--abbrev-ref", "HEAD") != "HEAD":
        raise CheckoutError("detached")
    if _git_output(checkout, "remotes", "-C", str(checkout.repository), "remote") != "":
        raise CheckoutError("remote")


def _verify_checkout_root(root: Path, repository: Path) -> None:
    if root.name.startswith("reviewer-checkout-") and repository == root / "repository":
        verify_private_dir(root)
        return
    raise CheckoutError("checkout_path")


def _remove_tree(root: Path) -> None:
    """git cloneが付けたread-only file属性を外してから、自分のrootを破棄する。"""
    def clear_readonly(function: object, path: str, error: BaseException) -> None:
        del error
        try:
            os.chmod(path, stat.S_IWRITE)
            function(path)  # type: ignore[operator]
        except OSError:
            raise CheckoutError("remove") from None

    shutil.rmtree(root, onexc=clear_readonly)
