# SPDX-License-Identifier: Apache-2.0
"""C-09 reviewer turn adapter: 隔離checkout → prompt → 起動 → head bindingを1つの呼出で束ねる。

Phase 9の各部品（checkout / canary home / preflight / launch / prompt / head binding）は単体で
検証済みだが、本moduleがそれらを**固定した順序**で呼ぶ本番経路である。順序は安全性の一部で、
呼出側が部品を並べ替える入口を持たない:

1. promptの構築（純粋。失敗はcheckoutより前に分かる）
2. reviewer専用home / env（C-06。provider認証・token envは渡らない）
3. 隔離checkout（exact head、detached、remoteなし）
4. 専用`CODEX_HOME`（workspace = 隔離checkout、protected = 実repository + 呼出側のroot）
5. Windowsではprovisioning成果物の複製（ADR-0029。provisioningは走らせない）
6. 起動facade（preflightを同じ呼出の中で行い、成立しなければspawnしない）
7. 隔離checkoutのHEADを再観測し、advertised head・reportの対象headと三者照合（AC-C09-04）
8. checkoutの破棄（dirty stateをevidenceとして返す。失敗しても必ず試みる）

reportからの対象head抽出はC-10のport（`ReportHeadPort`）で、本実装までは`PortUnavailableError`で
停止する。`HeadMismatch`時の非投稿はC-10の責務で、本moduleは判定結果を返すだけである。
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..identity import (
    CredentialIsolationError,
    build_reviewer_env,
    create_private_dir,
    prepare_reviewer_home,
    verify_private_dir,
)
from ..identity.fs_permissions import FsPermissionError
from .checkout import CheckoutError, CheckoutRelease, ReviewerCheckout, create_reviewer_checkout, observe_checkout_head
from .codex_canary import CanaryError, prepare_codex_canary_home
from .codex_launch import LaunchError, ReviewerCompleted, ReviewerTimedOut, launch_codex_reviewer
from .codex_preflight import PreflightError
from .codex_provisioning import ProvisioningError, ProvisioningMirror, mirror_provisioning_artifacts
from .head_binding import HeadMismatch, HeadsBound, verify_review_target
from .ports import PortUnavailableError
from .review_prompt import PromptError, ReviewContext, build_review_prompt


class ReportHeadPort(Protocol):
    """reviewerの最終messageから対象head SHAを取り出すport。本実装はC-10のreport parser。"""

    def reported_head(self, last_message: bytes | None) -> str | None: ...


class UnavailableReportHead:
    """C-10が実装するまでのfail closed実装。"""

    def reported_head(self, last_message: bytes | None) -> str | None:
        raise PortUnavailableError("reportからの対象head抽出はC-10（report parser）が実装する")


class TurnError(Exception):
    """固定stage（`<部品>:<部品のstage>`）だけを公開するturnの失敗。本文・path・native出力を含めない。"""

    def __init__(self, stage: str) -> None:
        super().__init__(f"reviewer_turn_error: {stage}")
        self.stage = stage


@dataclass(frozen=True)
class ReviewerTurnRequest:
    """1 turnの入力。すべて明示値で、既定値を持たない。`run_root`は呼出側が所有し、turn後に破棄する。"""

    source_repository: Path
    advertised_head: str
    context: ReviewContext
    run_root: Path
    protected_roots: tuple[Path, ...]
    git_command: tuple[str, ...]
    codex_executable: Path
    base_env: Mapping[str, str]
    provisioning_source: Path | None
    git_timeout_seconds: float
    git_grace_seconds: float
    reviewer_timeout_seconds: float
    reviewer_grace_seconds: float


@dataclass(frozen=True)
class ReviewerTurn:
    """1 turnの結果。prompt本文は含めない（起動facadeがevidence root配下へ私有fileとして書く）。"""

    prompt_boundary: str
    redaction_hits: int
    launch: ReviewerCompleted | ReviewerTimedOut
    observed_head: str
    binding: HeadsBound | HeadMismatch
    release: CheckoutRelease
    provisioning: ProvisioningMirror | None
    evidence_root: Path


def run_reviewer_turn(request: ReviewerTurnRequest, *, report_head: ReportHeadPort) -> ReviewerTurn:
    """固定順序でturnを実行する。どの段階で失敗しても、作成済みのcheckoutは破棄を試みる。"""
    _validate_run_root(request.run_root)
    if request.advertised_head != request.context.target_head_sha:
        # 起動前にheadが動いている。reviewを走らせても投稿できないため、checkoutを作る前に止める
        raise TurnError("head:advertised_moved")
    try:
        prompt = build_review_prompt(request.context)
    except PromptError as error:
        raise TurnError(f"prompt:{error.stage}") from error
    try:
        reviewer_home = prepare_reviewer_home(request.run_root, "reviewer-home")
        reviewer_env = build_reviewer_env(request.base_env, reviewer_home)
    except (CredentialIsolationError, FsPermissionError, OSError) as error:
        raise TurnError("reviewer_home") from error
    try:
        checkout = create_reviewer_checkout(
            parent=request.run_root,
            source_repository=request.source_repository,
            target_head_sha=request.context.target_head_sha,
            git_command=request.git_command,
            env=reviewer_env,
            timeout_seconds=request.git_timeout_seconds,
            grace_seconds=request.git_grace_seconds,
        )
    except CheckoutError as error:
        raise TurnError(f"checkout:{error.stage}") from error
    try:
        launch, observed, provisioning, evidence_root = _review_in_checkout(
            request, checkout, reviewer_env, prompt.text
        )
    except BaseException:
        _release_quietly(checkout)
        raise
    try:
        release = checkout.release()
    except CheckoutError as error:
        raise TurnError("checkout:release") from error
    last_message = launch.last_message if isinstance(launch, ReviewerCompleted) else None
    reported = report_head.reported_head(last_message)
    binding = verify_review_target(
        checkout_head=observed, advertised_head=request.advertised_head, reported_head=reported or ""
    )
    return ReviewerTurn(
        prompt_boundary=prompt.boundary,
        redaction_hits=prompt.redaction_hits,
        launch=launch,
        observed_head=observed,
        binding=binding,
        release=release,
        provisioning=provisioning,
        evidence_root=evidence_root,
    )


def _review_in_checkout(
    request: ReviewerTurnRequest, checkout: ReviewerCheckout, reviewer_env: Mapping[str, str], prompt_text: str
) -> tuple[ReviewerCompleted | ReviewerTimedOut, str, ProvisioningMirror | None, Path]:
    try:
        home = prepare_codex_canary_home(
            private_root=request.run_root,
            name="codex-home",
            workspace_root=checkout.repository,
            protected_roots=(request.source_repository, *request.protected_roots),
        )
    except CanaryError as error:
        raise TurnError(f"canary:{error.stage}") from error
    provisioning: ProvisioningMirror | None = None
    if request.provisioning_source is not None and _platform() == "win32":
        try:
            provisioning = mirror_provisioning_artifacts(home, request.provisioning_source)
        except ProvisioningError as error:
            raise TurnError(f"provisioning:{error.stage}") from error
    evidence_root = request.run_root / "evidence"
    try:
        create_private_dir(evidence_root)
    except (FsPermissionError, OSError) as error:
        raise TurnError("evidence_root") from error
    try:
        launch = launch_codex_reviewer(
            home=home,
            codex_executable=request.codex_executable,
            reviewer_env=reviewer_env,
            evidence_root=evidence_root,
            prompt=prompt_text,
            timeout_seconds=request.reviewer_timeout_seconds,
            grace_seconds=request.reviewer_grace_seconds,
        )
    except PreflightError as error:
        raise TurnError(f"preflight:{error.stage}") from error
    except LaunchError as error:
        raise TurnError(f"launch:{error.stage}") from error
    try:
        observed = observe_checkout_head(checkout)
    except CheckoutError as error:
        raise TurnError(f"checkout:{error.stage}") from error
    return launch, observed, provisioning, evidence_root


def _release_quietly(checkout: ReviewerCheckout) -> None:
    """失敗経路の破棄。元の失敗理由を置き換えないため、破棄の失敗は握る（root内に限られる）。"""
    try:
        checkout.release()
    except CheckoutError:
        pass


def _validate_run_root(run_root: Path) -> None:
    try:
        if not run_root.is_absolute() or run_root != run_root.resolve() or not run_root.is_dir():
            raise TurnError("run_root")
        verify_private_dir(run_root)
    except (FsPermissionError, OSError) as error:
        raise TurnError("run_root") from error


def _platform() -> str:
    return sys.platform
