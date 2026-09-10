# SPDX-License-Identifier: Apache-2.0
"""C-09 reviewerの起動facade（ADR-0027 決定15）。

sandbox preflight（決定13 / 14）を**同じ呼出の中で**実行してからreviewerをspawnする。
測定とspawnの間に呼出側のcodeを挟まないため、過去のevidenceを渡す入口が無い。

- promptはevidence root配下の私有file（0o600、排他作成）へ書き、C-03の`stdin_path`で子の
  stdinへ流す。argvには載せない
- 最終messageは`-o <evidence root配下のfile>`で受け取る。spawn前にそのfileが存在しない
  ことを要求し、今回のprocessが新規に生成したfileだけを返す
- 最終messageは**raw bytesのまま**、上限+1 byteまでのbounded readで搬送する。decodeも
  置換もしない。不正UTF-8とsize超過の判定はC-10がschema pipeline（size -> utf8 -> json）
  で行えるよう、原文を壊さない
- reviewerのstderrは共通redaction registryを通した`RedactionResult`としてだけ公開し、
  生のnative出力を結果へ含めない。失敗は固定stageだけを公開する
- timeoutはC-03がtreeを停止した後に`ReviewerTimedOut`として返す

本moduleは実Codexへ接続する経路そのものだが、test・CIでは実行しない。認証材料の供給、
prompt本文の構成（P-008 fence、`ReviewContext`）、出力の受理・検証はC-10以降の責務である。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..identity.fs_permissions import FsPermissionError, write_private_text
from ..policy.redaction import RedactionResult, redact
from ..process import Completed, SpawnError, SpawnSpec, StopError, run_tree
from .codex_canary import CanaryError, CodexCanaryHome, build_codex_canary_invocation
from .codex_preflight import PreflightEvidence, run_sandbox_preflight

MAX_PROMPT_BYTES: Final = 1_048_576
MAX_DIAGNOSTIC_BYTES: Final = 262_144
# 最終messageのbounded readの上限。C-10はこの値以下の`max_input_bytes`で検証する前提で、
# 上限+1 byteまで読むことで「上限を超えていた」ことをC-10のsize stageが判定できる。
MAX_LAST_MESSAGE_BYTES: Final = 262_144
_PROMPT_NAME: Final = "prompt.txt"
_LAST_MESSAGE_NAME: Final = "last_message.txt"
_STDOUT_NAME: Final = "reviewer.stdout"
_STDERR_NAME: Final = "reviewer.stderr"


class LaunchError(Exception):
    """固定stageだけを公開する起動の失敗。native出力・path・prompt本文は含めない。"""

    def __init__(self, stage: str) -> None:
        super().__init__(f"codex_launch_error: {stage}")
        self.stage = stage


@dataclass(frozen=True)
class ReviewerCompleted:
    """reviewerが終了した。

    `last_message`は`-o`のfileの**raw bytes**（上限+1 byteまで）で、fileが無ければNone。
    decodeも置換もしていない。成否・schema・sizeの判定はC-10が行う。
    """

    exit_code: int
    last_message: bytes | None
    diagnostic: RedactionResult
    evidence: PreflightEvidence


@dataclass(frozen=True)
class ReviewerTimedOut:
    """timeoutによりC-03がtreeを停止した。結果は採用しない。"""

    diagnostic: RedactionResult
    evidence: PreflightEvidence


def launch_codex_reviewer(
    *,
    home: CodexCanaryHome,
    codex_executable: Path,
    reviewer_env: Mapping[str, str],
    evidence_root: Path,
    prompt: str,
    timeout_seconds: float,
    grace_seconds: float,
) -> ReviewerCompleted | ReviewerTimedOut:
    """preflight -> prompt file -> spawn -> 結果の読み取り。preflightの失敗はそのまま伝播する。"""
    _validate_prompt(prompt)
    evidence = run_sandbox_preflight(
        home=home,
        codex_executable=codex_executable,
        reviewer_env=reviewer_env,
        evidence_root=evidence_root,
        timeout_seconds=timeout_seconds,
        grace_seconds=grace_seconds,
    )
    root = Path(evidence_root)
    prompt_path = root / _PROMPT_NAME
    last_message_path = root / _LAST_MESSAGE_NAME
    # pre-seedされた古い出力を今回の結果として返さない。`-o`は出力先を指定するだけで、
    # 全失敗経路で既存fileが更新される保証は無い。
    if last_message_path.exists():
        raise LaunchError("output")
    try:
        write_private_text(prompt_path, prompt)
    except (FsPermissionError, OSError) as error:
        raise LaunchError("prompt") from error
    try:
        invocation = build_codex_canary_invocation(
            home=home,
            codex_executable=codex_executable,
            reviewer_env=reviewer_env,
            last_message_path=last_message_path,
        )
    except CanaryError as error:
        raise LaunchError("configuration") from error
    spec = SpawnSpec(
        argv=invocation.argv,
        cwd=invocation.cwd,
        env=invocation.env,
        stdout_path=root / _STDOUT_NAME,
        stderr_path=root / _STDERR_NAME,
        stdin_path=prompt_path,
    )
    try:
        outcome = run_tree(spec, timeout_seconds=timeout_seconds, grace_seconds=grace_seconds)
    except SpawnError as error:
        raise LaunchError("spawn") from error
    except StopError as error:
        # timeout後の停止・closeの失敗。C-03のnative detailを持つ型をfacade外へ出さない
        raise LaunchError("stop") from error
    diagnostic = _read_diagnostic(root / _STDERR_NAME)
    if not isinstance(outcome, Completed):
        return ReviewerTimedOut(diagnostic=diagnostic, evidence=evidence)
    return ReviewerCompleted(
        exit_code=outcome.exit_code,
        last_message=_read_last_message(last_message_path),
        diagnostic=diagnostic,
        evidence=evidence,
    )


def _validate_prompt(prompt: str) -> None:
    """空・NUL・上限超・UTF-8へencodeできない文字列（unpaired surrogate）を起動前に拒否する。"""
    try:
        encoded = prompt.encode("utf-8")
    except UnicodeEncodeError as error:
        raise LaunchError("prompt") from error
    if not prompt.strip() or len(encoded) > MAX_PROMPT_BYTES or "\x00" in prompt:
        raise LaunchError("prompt")


def _read_diagnostic(stderr_path: Path) -> RedactionResult:
    """stderrを上限bytesまで読み、redactionを通してから公開する。"""
    try:
        with stderr_path.open("rb") as handle:
            raw = handle.read(MAX_DIAGNOSTIC_BYTES)
    except OSError as error:
        raise LaunchError("diagnostic") from error
    return redact(raw.decode("utf-8", errors="replace"))


def _read_last_message(path: Path) -> bytes | None:
    """今回のprocessが生成したfileを、上限+1 byteまでraw bytesのまま読む。"""
    try:
        with path.open("rb") as handle:
            return handle.read(MAX_LAST_MESSAGE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise LaunchError("output") from error
