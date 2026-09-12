# SPDX-License-Identifier: Apache-2.0
"""C-09 sandbox preflight（ADR-0027 決定13 / 14）。

reviewerをspawnする直前に、生成したpermission profileが**実起動と同じconfig stackで
選ばれ**、かつ**OSが実際に強制している**ことをfacade自身が実測する。確認は互いの代替に
ならず、1つでも成立しなければ`PreflightError`で停止する（fail closed）。

1. effective configの照合: 同じexecutable・同じ`CODEX_HOME`・同じcwdで`codex doctor --json`
   を取得し、observableなfield（approval `Never`、sandbox `restricted`、denied-read有効、
   sandbox helperの状態）を確認する
2. probeの同一性: facadeが選ぶcanonicalなprobe（本packageの`sandbox_probe.py`）の内容を
   固定digestと照合する。呼出側からprobe・接続先を受け取らない
3. positive control: 同じprobe・同じ接続先をsandboxの**外**で実行し、書込・読取・接続が
   通ることを確かめる。これが無いと、到達不能な接続先や壊れたpathでも`denied`に見える
4. OS強制の実測: `codex sandbox -P <profile> --include-managed-config -C <checkout> -- <probe>`
   で同じprobeを**中**で実行し、境界が期待どおり閉じていることを確かめる

evidenceはexecutable・version・config digest・profile・roots・`CODEX_HOME`・env digest・
probe interpreter・probe digest・接続先・観測結果へbindする。`verify_preflight_evidence`は
spawn直前に**同じ測定を再実行**し、与えられたevidenceと完全一致する場合だけ通す。過去の
evidenceや呼出側が組み立てたdataclassは、現実と一致しなければ通らない。native出力は
例外へ含めず、固定stageだけを公開する。
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..identity.fs_permissions import FsPermissionError, verify_private_dir
from ..policy.permission_profile import ensure_argv_allowed
from ..process import Completed, SpawnError, SpawnSpec, StopError, run_tree
from ..schema.projection import canonical_json
from . import sandbox_probe
from .codex_canary import (
    PROFILE_NAME,
    CanaryError,
    CodexCanaryHome,
    CodexCanaryInvocation,
    build_codex_canary_invocation,
)
from .sandbox_probe import CLEANUP_LABEL, PROBE_LABELS

# sandboxの外で同じprobeが到達できることを先に確かめる接続先。reviewerが到達して
# はならない先（AC-C09-05）をそのまま使う。TCP handshakeだけで、requestは送らない。
NETWORK_CONTROL_TARGET: Final[tuple[str, int]] = ("api.github.com", 443)
# `sandbox_probe.py`の内容（改行をLFへ正規化）のSHA-256。probeを変更したら更新し、
# testが実fileと一致することを固定する。一致しなければprobeを信頼せず起動しない。
PROBE_DIGEST: Final = "ba4b0262cca6b4698ee4cc13b8c8da8732cab09458569bef5c3d61c1979b04e4"

EXPECTED_BOUNDARIES: Final[Mapping[str, str]] = {
    "workspace_write": "allowed",
    "protected_write": "denied",
    "credential_read": "denied",
    "network": "denied",
}
EXPECTED_CONTROL: Final[Mapping[str, str]] = {
    "workspace_write": "allowed",
    "protected_write": "allowed",
    "credential_read": "allowed",
    "network": "allowed",
}
_EXPECTED_EFFECTIVE: Final[Mapping[str, str]] = {
    "approval policy": "Never",
    "filesystem sandbox": "restricted",
    "network sandbox": "restricted",
    "denied-read restrictions": "true",
}
# D-033: Windows nativeではelevated backendが必須で、provisioningが完了していなければ起動しない。
# backendの選択は`CODEX_HOME`のconfigで決まり、provisioningは`CODEX_HOME`ごとに紐付く（ADR-0027 追補）。
_WINDOWS_EXPECTED_BACKEND: Final[Mapping[str, str]] = {
    "sandbox backend": "elevated",
    "sandbox provisioning": "complete",
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
    # 0.154.0以降のdoctorが報告する。無ければ空文字で記録し、Windowsでは要求値との一致を要求する
    sandbox_backend: str
    sandbox_provisioning: str


@dataclass(frozen=True)
class PreflightEvidence:
    """facadeが取得した実測evidence。spawn直前に同じ測定を再実行して照合する。"""

    codex_executable: str
    codex_version: str
    configuration_digest: str
    profile_name: str
    workspace_root: Path
    protected_roots: tuple[Path, ...]
    codex_home: Path
    environment_digest: str
    probe_interpreter: str
    probe_digest: str
    network_target: tuple[str, int]
    effective: EffectiveSandbox
    control: Mapping[str, str]
    boundaries: Mapping[str, str]


def run_sandbox_preflight(
    *,
    home: CodexCanaryHome,
    codex_executable: Path,
    reviewer_env: Mapping[str, str],
    evidence_root: Path,
    timeout_seconds: float,
    grace_seconds: float,
) -> PreflightEvidence:
    """4つの確認を順に行い、すべて成立した場合だけevidenceを返す。"""
    invocation = _invocation(home, codex_executable, reviewer_env)
    executable = invocation.argv[0]
    _validate_evidence_root(evidence_root, home)
    runner = _Runner(invocation.env, home.workspace_root, evidence_root, timeout_seconds, grace_seconds)
    version = _read_version(runner, executable)
    effective = _read_effective_config(runner, executable, home)
    probe = _canonical_probe()
    # sentinelは1測定ごとに払い出す一意なfile名。probeの書込先をhost側で残留確認するために使い、
    # 再測定で値が変わるためevidenceには含めない。
    sentinel = f".cc-review-probe-{uuid.uuid4().hex}"
    control = _run_control(runner, probe, home, sentinel)
    boundaries = _probe_boundaries(runner, executable, probe, home, sentinel)
    return PreflightEvidence(
        codex_executable=executable,
        codex_version=version,
        configuration_digest=home.configuration_digest,
        profile_name=PROFILE_NAME,
        workspace_root=home.workspace_root,
        protected_roots=home.protected_roots,
        codex_home=home.root,
        environment_digest=_digest(canonical_json(dict(invocation.env)).encode("utf-8")),
        probe_interpreter=probe.interpreter,
        probe_digest=PROBE_DIGEST,
        network_target=NETWORK_CONTROL_TARGET,
        effective=effective,
        control=control,
        boundaries=boundaries,
    )


def verify_preflight_evidence(
    evidence: PreflightEvidence,
    *,
    home: CodexCanaryHome,
    codex_executable: Path,
    reviewer_env: Mapping[str, str],
    evidence_root: Path,
    timeout_seconds: float,
    grace_seconds: float,
) -> None:
    """spawn直前の再検証。同じ測定を再実行し、与えられたevidenceと完全一致しなければ停止する。

    再実行が正であり、evidenceはその一致を要求されるだけである。過去turnのevidence、
    期待値で組み立てたdataclass、測定後に変わったsystem / managed configやbackendは、
    いずれも現在の測定と一致しない限り通らない。
    """
    fresh = run_sandbox_preflight(
        home=home,
        codex_executable=codex_executable,
        reviewer_env=reviewer_env,
        evidence_root=evidence_root,
        timeout_seconds=timeout_seconds,
        grace_seconds=grace_seconds,
    )
    if fresh != evidence:
        raise PreflightError("evidence_mismatch")


@dataclass(frozen=True)
class _Runner:
    env: Mapping[str, str]
    workspace: Path
    evidence_root: Path
    timeout_seconds: float
    grace_seconds: float

    def output(self, stage: str, argv: tuple[str, ...]) -> tuple[int, str]:
        """argvを実行し、終了codeとUTF-8のstdoutを返す。起動失敗とtimeoutはstageで停止する。"""
        ensure_argv_allowed(argv)
        stdout_path = self.evidence_root / f"{stage}.stdout"
        stderr_path = self.evidence_root / f"{stage}.stderr"
        spec = SpawnSpec(argv=argv, cwd=self.workspace, env=self.env, stdout_path=stdout_path, stderr_path=stderr_path)
        try:
            outcome = run_tree(spec, timeout_seconds=self.timeout_seconds, grace_seconds=self.grace_seconds)
        except (SpawnError, StopError) as error:
            # 起動失敗も、timeout後の停止失敗も、C-03のnative detailを持つ型を外へ出さない
            raise PreflightError(stage) from error
        if not isinstance(outcome, Completed):
            raise PreflightError(stage)
        try:
            text = stdout_path.read_bytes().decode("utf-8", errors="replace")
        except OSError as error:
            raise PreflightError(stage) from error
        return outcome.exit_code, text


def _invocation(
    home: CodexCanaryHome, codex_executable: Path, reviewer_env: Mapping[str, str]
) -> CodexCanaryInvocation:
    try:
        return build_codex_canary_invocation(home=home, codex_executable=codex_executable, reviewer_env=reviewer_env)
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


def _read_version(runner: _Runner, executable: str) -> str:
    exit_code, text = runner.output("version", (executable, "--version"))
    version = text.strip()
    if exit_code != 0 or not version or "\n" in version:
        raise PreflightError("version")
    return version


def _read_effective_config(runner: _Runner, executable: str, home: CodexCanaryHome) -> EffectiveSandbox:
    """doctorの終了codeは認証欠如でもfailになるため見ず、JSONの該当checkだけを照合する。"""
    _, text = runner.output("doctor", (executable, "doctor", "--json"))
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
            sandbox_backend=str(details.get("sandbox backend", "")),
            sandbox_provisioning=str(details.get("sandbox provisioning", "")),
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
    # D-033: helperがokでも、Windowsでelevated backendがprovisioning済みでなければ起動しない。
    if _platform() == "win32" and _backend_fields(effective) != dict(_WINDOWS_EXPECTED_BACKEND):
        raise PreflightError("sandbox_backend")
    return effective


@dataclass(frozen=True)
class _Probe:
    """digest照合済みのprobe。`content`は改行をLFへ正規化した本文で、複製の元になる。"""

    interpreter: str
    source: str
    content: bytes


def _canonical_probe() -> _Probe:
    """facadeが選ぶprobe: 現在のinterpreterと、固定digestに一致する本packageのprobe file。"""
    try:
        interpreter = Path(sys.executable).resolve()
        source = Path(sandbox_probe.__file__).resolve()
        if not interpreter.is_file() or not source.is_file():
            raise PreflightError("probe_integrity")
        content = source.read_bytes().replace(b"\r\n", b"\n")
    except OSError as error:
        raise PreflightError("probe_integrity") from error
    if _digest(content) != PROBE_DIGEST:
        raise PreflightError("probe_integrity")
    return _Probe(interpreter=str(interpreter), source=str(source), content=content)


def _probe_arguments(home: CodexCanaryHome, protected: tuple[Path, ...], sentinel: str) -> tuple[str, ...]:
    host, port = NETWORK_CONTROL_TARGET
    return (
        str(home.workspace_root),
        str(home.config_path),
        host,
        str(port),
        sentinel,
        *(str(path) for path in protected),
    )


def _run_control(runner: _Runner, probe: _Probe, home: CodexCanaryHome, sentinel: str) -> Mapping[str, str]:
    """sandboxの外で同じprobeを実行し、書込・読取・接続が通ることを確かめる。

    protected rootは渡さない。実repositoryへは一時fileであっても書かず、この確認で
    証明するのは「probeが動き、接続先へ到達できる」ことである。
    """
    argv = (probe.interpreter, probe.source, *_probe_arguments(home, (), sentinel))
    observed = _probe_stage(runner, "control", argv, home, sentinel, exit_stage="control", missing_stage="control")
    if observed != dict(EXPECTED_CONTROL):
        raise PreflightError("control_boundary")
    return observed


def _probe_boundaries(
    runner: _Runner, executable: str, probe: _Probe, home: CodexCanaryHome, sentinel: str
) -> Mapping[str, str]:
    """sandboxの中でprobeを実行する。

    probeの本体は隔離checkout（workspace root）へ複製してから実行する。専用profileは実repository
    （本packageの所在を含む）をdenyし、elevated backendではその読取拒否が実際に強制されるため、
    package内のprobe fileを直接指定すると起動できない（2026-09-12実測、ADR-0027 決定17）。複製は
    本呼出だけの名前で置き、実行後に必ず取り除く。取り除けなければ`probe_residue`で停止する。
    """
    copy = _stage_probe_copy(home, sentinel, probe.content)
    argv = (
        executable,
        "sandbox",
        "-P",
        PROFILE_NAME,
        "--include-managed-config",
        "-C",
        str(home.workspace_root),
        "--",
        probe.interpreter,
        str(copy),
        *_probe_arguments(home, home.protected_roots[:-1], sentinel),
    )
    try:
        observed = _probe_stage(
            runner, "probe", argv, home, sentinel, exit_stage="sandbox_unavailable", missing_stage="probe_unavailable"
        )
    finally:
        leftover = _remove_probe_copy(copy)
    if leftover:
        raise PreflightError("probe_residue")
    if observed != dict(EXPECTED_BOUNDARIES):
        raise PreflightError("boundary")
    return observed


def _stage_probe_copy(home: CodexCanaryHome, sentinel: str, content: bytes) -> Path:
    """digest照合済みの本文を、隔離checkout直下の本呼出だけの名前へ置く。既存のentryがあれば置かない。

    通常のfileとして書く（private ACLにしない）。sandbox userが読めるのはworkspace rootから継承する
    権限であり、owner限定のACLを付けると読取が拒否される。
    """
    copy = home.workspace_root / f"{sentinel}.py"
    if copy.is_symlink() or copy.exists():
        raise PreflightError("probe_copy")
    try:
        copy.write_bytes(content)
    except OSError as error:
        # 書込途中の失敗（disk full等）でfileが作られていることがある。作成済みentryは回収し、
        # 回収できなければ残留として停止する（決定17「実行後は複製を必ず取り除く」）
        if _remove_probe_copy(copy):
            raise PreflightError("probe_residue") from error
        raise PreflightError("probe_copy") from error
    return copy


def _remove_probe_copy(copy: Path) -> bool:
    """複製を取り除き、残留していればTrueを返す（best effort。停止理由は呼出側が決める）。"""
    try:
        copy.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return copy.is_symlink() or copy.exists()


def _probe_stage(
    runner: _Runner,
    stage: str,
    argv: tuple[str, ...],
    home: CodexCanaryHome,
    sentinel: str,
    *,
    exit_stage: str,
    missing_stage: str,
) -> dict[str, str]:
    """probeを1回実行し、host側で残留を掃除・検出してから結果を読む。

    probeの書込が成功して削除だけ失敗した場合、probeは境界を`allowed`と報告しつつ
    `cleanup=failed`を返す。facadeはhost側の残留とcleanup失敗のどちらでも`probe_residue`で
    停止する。残留の掃除はbest effortで、失敗しても停止理由を置き換えない。
    """
    try:
        exit_code, text = runner.output(stage, argv)
    finally:
        residue = _sweep_residue(home, sentinel)
    if residue:
        raise PreflightError("probe_residue")
    if exit_code != 0:
        raise PreflightError(exit_stage)
    observed, cleanup = _parse_probe(text, missing_stage)
    if cleanup != "ok":
        raise PreflightError("probe_residue")
    return observed


def _sweep_residue(home: CodexCanaryHome, sentinel: str) -> bool:
    """probeのsentinelがworkspace / protected rootに残っていれば掃除し、残留の有無を返す。"""
    found = False
    for root in (home.workspace_root, *home.protected_roots[:-1]):
        target = root / sentinel
        if target.exists():
            found = True
            try:
                target.unlink()
            except OSError:
                pass
    return found


def _parse_probe(text: str, missing_stage: str) -> tuple[dict[str, str], str]:
    observed: dict[str, str] = {}
    cleanup: str | None = None
    for line in text.splitlines():
        label, separator, outcome = line.strip().partition("=")
        if separator and label in PROBE_LABELS and outcome in {"allowed", "denied"}:
            observed[label] = outcome
        elif separator and label == CLEANUP_LABEL and outcome in {"ok", "failed"}:
            cleanup = outcome
    if set(observed) != set(PROBE_LABELS) or cleanup is None:
        raise PreflightError(missing_stage)
    return observed, cleanup


def _effective_fields(effective: EffectiveSandbox) -> dict[str, str]:
    return {
        "approval policy": effective.approval_policy,
        "filesystem sandbox": effective.filesystem_sandbox,
        "network sandbox": effective.network_sandbox,
        "denied-read restrictions": effective.denied_read_restrictions,
    }


def _backend_fields(effective: EffectiveSandbox) -> dict[str, str]:
    return {
        "sandbox backend": effective.sandbox_backend,
        "sandbox provisioning": effective.sandbox_provisioning,
    }


def _platform() -> str:
    return sys.platform


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
