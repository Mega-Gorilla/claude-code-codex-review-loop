# SPDX-License-Identifier: Apache-2.0
"""Auto modeの利用可否検出（AC-C06-10の検出部分）。

利用可否はaccount / model / provider / CLI versionに依存する環境依存の事実であり、
検出はI/OとしてC-06が担う。**選択規則そのものはC-04の`select_profile`が持つ**（Auto
不可時は用途別にacceptEdits / default / dontAskへ倒す純粋規則）。

- 判定材料は構造化された事実（exit codeとJSONとして解釈できるか）だけで、helpや
  mode名の文字列一致には依存しない（P-003。CLIの表示文言はversionで変わる）
- 起動失敗・timeoutは「利用不可」へ倒す（fail closed側で、C-04のfallback profileが
  受け止める）。誤検出でAuto可と判定した場合も、実行時のblockは
  `AWAITING_TOOL_PERMISSION`経路が受け止める
- 実行commandとtimeoutの既定値は持たない（必須引数。解決はC-12）
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..policy.permission_profile import ensure_argv_allowed
from ..process import Completed, SpawnError, SpawnSpec, run_tree
from .errors import IdentityError

# probe出力の機構上限（設定値ではない）。超過は「解釈できない出力」として利用不可へ倒す
MAX_PROBE_OUTPUT_BYTES: Final = 1_048_576

_PROBE_SUBCOMMAND: Final = ("auto-mode", "config")


@dataclass(frozen=True)
class AutoModeProbe:
    """検出probeの構造化結果。exit_codeがNoneのときは起動失敗またはtimeout。"""

    exit_code: int | None
    config_json_valid: bool


def detect_auto_mode(probe: AutoModeProbe) -> bool:
    """probe結果からAuto modeの利用可否を決める（純粋規則）。"""
    return probe.exit_code == 0 and probe.config_json_valid


def _read_probe_output(path: Path) -> bool:
    """probe出力がJSON objectとして解釈できるか（上限超過・不正はFalse）。"""
    try:
        raw = path.read_bytes()
    except OSError:  # pragma: no cover - 直前にredirect先として作成したfileの読み出し失敗
        return False
    if len(raw) > MAX_PROBE_OUTPUT_BYTES:
        return False
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(parsed, dict)


def probe_auto_mode(
    claude_command: tuple[str, ...],
    *,
    workdir: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    grace_seconds: float,
) -> AutoModeProbe:
    """Auto modeの設定照会を1回実行し、結果を構造化して返す（例外を投げない）。

    claude_commandはargv prefix（先頭は絶対path。envが非継承でPATH解決に依存しない
    ため）。実行はC-03のrun_tree経由で、argvは`ensure_argv_allowed`を通す（P-006）。
    """
    if not claude_command or not os.path.isabs(claude_command[0]):
        raise IdentityError("probe", "claude_commandの先頭は絶対pathでなければならない")
    argv = (*claude_command, *_PROBE_SUBCOMMAND)
    ensure_argv_allowed(argv)
    stdout_path = workdir / "auto-mode-probe.out"
    stderr_path = workdir / "auto-mode-probe.err"
    spec = SpawnSpec(argv=argv, cwd=workdir, env=env, stdout_path=stdout_path, stderr_path=stderr_path)
    try:
        outcome = run_tree(spec, timeout_seconds=timeout_seconds, grace_seconds=grace_seconds)
    except SpawnError:
        return AutoModeProbe(exit_code=None, config_json_valid=False)
    try:
        if not isinstance(outcome, Completed):
            return AutoModeProbe(exit_code=None, config_json_valid=False)
        return AutoModeProbe(exit_code=outcome.exit_code, config_json_valid=_read_probe_output(stdout_path))
    finally:
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)
