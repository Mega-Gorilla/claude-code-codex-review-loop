# SPDX-License-Identifier: Apache-2.0
"""C-09 sandbox preflight（ADR-0027 決定13 / 14）。

reviewerをspawnする直前に、生成したpermission profileが**実起動と同じconfig stackで
選ばれ**、かつ**OSが実際に強制している**ことをfacade自身が実測する。2つは互いの代替に
ならず、どちらか1つでも成立しなければ`PreflightError`で停止する（fail closed）。

- effective configの照合: 同じcommand・同じ`CODEX_HOME`・同じcwdで`codex doctor --json`
  を取得し、observableなfield（approval `Never`、sandbox `restricted`、denied-read有効、
  sandbox helperの状態）を確認する
- OS強制の実測: `codex sandbox -P <profile> --include-managed-config -C <checkout> -- <probe>`
  で境界を実測する。`codex sandbox`は認証を要求しない

evidenceはcommand・version・config digest・profile・roots・`CODEX_HOME`・env digestへ
bindし、spawn直前に`verify_preflight_evidence`で同じ条件を再検証する。呼出側の申告値や
前turnのevidenceで代替しない。native出力は例外へ含めず、固定stageだけを公開する。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..identity.fs_permissions import FsPermissionError, verify_private_dir
from ..policy.permission_profile import ensure_argv_allowed
from ..process import Completed, SpawnError, SpawnSpec, run_tree
from ..schema.projection import canonical_json
from .codex_canary import (
    PROFILE_NAME,
    CanaryError,
    CodexCanaryHome,
    CodexCanaryInvocation,
    build_codex_canary_invocation,
)
from .sandbox_probe import PROBE_LABELS

EXPECTED_BOUNDARIES: Final[Mapping[str, str]] = {
    "workspace_write": "allowed",
    "protected_write": "denied",
    "credential_read": "denied",
    "network": "denied",
}
_EXPECTED_EFFECTIVE: Final[Mapping[str, str]] = {
    "approval policy": "Never",
    "filesystem sandbox": "restricted",
    "network sandbox": "restricted",
    "denied-read restrictions": "true",
}


class PreflightError(Exception):
    """固定stageだけを公開するpreflightの失敗。native出力・pathは含めない。"""

    def __init__(self, stage: str) -> None:
        super().__init__(f"sandbox_preflight_error: {stage}")
        self.stage = stage


@dataclass(frozen=True)
class EffectiveSandbox:
    """`codex doctor --json`から読み取ったobservableなeffective config。"""

    approval_policy: str
    filesystem_sandbox: str
    network_sandbox: str
    denied_read_restrictions: str


@dataclass(frozen=True)
class PreflightEvidence:
    """facadeが取得した実測evidence。spawn直前に同じ条件で再検証する。"""

    codex_command: tuple[str, ...]
    codex_version: str
    configuration_digest: str
    profile_name: str
    workspace_root: Path
    protected_roots: tuple[Path, ...]
    codex_home: Path
    environment_digest: str
    effective: EffectiveSandbox
    boundaries: Mapping[str, str]


def run_sandbox_preflight(
    *,
    home: CodexCanaryHome,
    codex_command: tuple[str, ...],
    reviewer_env: Mapping[str, str],
    probe_command: tuple[str, ...],
    network_target: tuple[str, int],
    evidence_root: Path,
    timeout_seconds: float,
    grace_seconds: float,
) -> PreflightEvidence:
    """2つの確認を両方行い、成立した場合だけevidenceを返す。"""
    invocation = _invocation(home, codex_command, reviewer_env)
    command = invocation.argv[: len(codex_command)]
    _validate_evidence_root(evidence_root, home)
    runner = _Runner(command, invocation.env, home.workspace_root, evidence_root, timeout_seconds, grace_seconds)
    version = _read_version(runner)
    effective = _read_effective_config(runner, home)
    boundaries = _probe_boundaries(runner, home, probe_command, network_target)
    return PreflightEvidence(
        codex_command=command,
        codex_version=version,
        configuration_digest=home.configuration_digest,
        profile_name=PROFILE_NAME,
        workspace_root=home.workspace_root,
        protected_roots=home.protected_roots,
        codex_home=home.root,
        environment_digest=_digest(invocation.env),
        effective=effective,
        boundaries=boundaries,
    )


def verify_preflight_evidence(
    evidence: PreflightEvidence,
    *,
    home: CodexCanaryHome,
    codex_command: tuple[str, ...],
    reviewer_env: Mapping[str, str],
    evidence_root: Path,
    timeout_seconds: float,
    grace_seconds: float,
) -> None:
    """spawn直前の再検証。binding先が1つでも違えば`evidence_mismatch`で停止する。"""
    invocation = _invocation(home, codex_command, reviewer_env)
    command = invocation.argv[: len(codex_command)]
    _validate_evidence_root(evidence_root, home)
    expected = (
        command,
        home.configuration_digest,
        PROFILE_NAME,
        home.workspace_root,
        home.protected_roots,
        home.root,
        _digest(invocation.env),
    )
    observed = (
        evidence.codex_command,
        evidence.configuration_digest,
        evidence.profile_name,
        evidence.workspace_root,
        evidence.protected_roots,
        evidence.codex_home,
        evidence.environment_digest,
    )
    if expected != observed or dict(evidence.boundaries) != dict(EXPECTED_BOUNDARIES):
        raise PreflightError("evidence_mismatch")
    if _effective_fields(evidence.effective) != dict(_EXPECTED_EFFECTIVE):
        raise PreflightError("evidence_mismatch")
    runner = _Runner(command, invocation.env, home.workspace_root, evidence_root, timeout_seconds, grace_seconds)
    if _read_version(runner) != evidence.codex_version:
        raise PreflightError("evidence_mismatch")


@dataclass(frozen=True)
class _Runner:
    command: tuple[str, ...]
    env: Mapping[str, str]
    workspace: Path
    evidence_root: Path
    timeout_seconds: float
    grace_seconds: float

    def output(self, stage: str, *arguments: str) -> tuple[int, str]:
        """codexを実行し、終了codeとUTF-8のstdoutを返す。起動失敗とtimeoutはstageで停止する。"""
        argv = (*self.command, *arguments)
        ensure_argv_allowed(argv)
        stdout_path = self.evidence_root / f"{stage}.stdout"
        stderr_path = self.evidence_root / f"{stage}.stderr"
        spec = SpawnSpec(argv=argv, cwd=self.workspace, env=self.env, stdout_path=stdout_path, stderr_path=stderr_path)
        try:
            outcome = run_tree(spec, timeout_seconds=self.timeout_seconds, grace_seconds=self.grace_seconds)
        except SpawnError as error:
            raise PreflightError(stage) from error
        if not isinstance(outcome, Completed):
            raise PreflightError(stage)
        try:
            text = stdout_path.read_bytes().decode("utf-8", errors="replace")
        except OSError as error:
            raise PreflightError(stage) from error
        return outcome.exit_code, text


def _invocation(
    home: CodexCanaryHome, codex_command: tuple[str, ...], reviewer_env: Mapping[str, str]
) -> CodexCanaryInvocation:
    try:
        return build_codex_canary_invocation(home=home, codex_command=codex_command, reviewer_env=reviewer_env)
    except CanaryError as error:
        raise PreflightError("configuration") from error


def _validate_evidence_root(evidence_root: Path, home: CodexCanaryHome) -> None:
    """evidenceの出力先はprivateで、workspaceとも`CODEX_HOME`とも重ならない。"""
    root = Path(evidence_root)
    try:
        if not root.is_absolute() or root != root.resolve() or not root.is_dir():
            raise PreflightError("evidence_root")
        verify_private_dir(root)
    except FsPermissionError as error:
        raise PreflightError("evidence_root") from error
    for other in (home.workspace_root, home.root):
        if root.is_relative_to(other) or other.is_relative_to(root):
            raise PreflightError("evidence_root")


def _read_version(runner: _Runner) -> str:
    exit_code, text = runner.output("version", "--version")
    version = text.strip()
    if exit_code != 0 or not version or "\n" in version:
        raise PreflightError("version")
    return version


def _read_effective_config(runner: _Runner, home: CodexCanaryHome) -> EffectiveSandbox:
    """doctorの終了codeは認証欠如でもfailになるため見ず、JSONの該当checkだけを照合する。"""
    _, text = runner.output("doctor", "doctor", "--json")
    try:
        report = json.loads(text)
        checks = report["checks"]
        config_load = checks["config.load"]
        helpers = checks["sandbox.helpers"]
        load_status = str(config_load["status"])
        load_details = config_load["details"]
        helper_status = str(helpers["status"])
        details = helpers["details"]
        effective = EffectiveSandbox(
            approval_policy=str(details["approval policy"]),
            filesystem_sandbox=str(details["filesystem sandbox"]),
            network_sandbox=str(details["network sandbox"]),
            denied_read_restrictions=str(details["denied-read restrictions"]),
        )
        home_seen = str(load_details["CODEX_HOME"])
        cwd_seen = str(load_details["cwd"])
    except (ValueError, KeyError, TypeError) as error:
        raise PreflightError("doctor_output") from error
    if load_status != "ok" or home_seen != str(home.root) or cwd_seen != str(home.workspace_root):
        raise PreflightError("effective_config")
    if _effective_fields(effective) != dict(_EXPECTED_EFFECTIVE):
        raise PreflightError("effective_config")
    # helperがfailなら、profileはこのOS / backendで適用できない（例: 非昇格Windowsのdeny）。
    if helper_status != "ok":
        raise PreflightError("sandbox_unavailable")
    return effective


def _probe_boundaries(
    runner: _Runner, home: CodexCanaryHome, probe_command: tuple[str, ...], network_target: tuple[str, int]
) -> Mapping[str, str]:
    host, port = network_target
    if not probe_command or any(not argument for argument in probe_command) or not host or port <= 0:
        raise PreflightError("probe_command")
    exit_code, text = runner.output(
        "probe",
        "sandbox",
        "-P",
        PROFILE_NAME,
        "--include-managed-config",
        "-C",
        str(home.workspace_root),
        "--",
        *probe_command,
        str(home.workspace_root),
        str(home.config_path),
        host,
        str(port),
        *(str(path) for path in home.protected_roots[:-1]),
    )
    if exit_code != 0:
        raise PreflightError("sandbox_unavailable")
    observed: dict[str, str] = {}
    for line in text.splitlines():
        label, separator, outcome = line.strip().partition("=")
        if separator and label in PROBE_LABELS and outcome in {"allowed", "denied"}:
            observed[label] = outcome
    if set(observed) != set(PROBE_LABELS):
        raise PreflightError("probe_unavailable")
    if observed != dict(EXPECTED_BOUNDARIES):
        raise PreflightError("boundary")
    return observed


def _effective_fields(effective: EffectiveSandbox) -> dict[str, str]:
    return {
        "approval policy": effective.approval_policy,
        "filesystem sandbox": effective.filesystem_sandbox,
        "network sandbox": effective.network_sandbox,
        "denied-read restrictions": effective.denied_read_restrictions,
    }


def _digest(env: Mapping[str, str]) -> str:
    return hashlib.sha256(canonical_json(dict(env)).encode("utf-8")).hexdigest()
