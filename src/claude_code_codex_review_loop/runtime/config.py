# SPDX-License-Identifier: Apache-2.0
"""session config（Phase 8 PR-3b1。ADR-0020）。

engineは既定値を持たない（解決はC-12）。entry pointは全設定を受け取る必要があるが、20項目超を
CLI引数にすると扱えないため、**run directory内の`session.json`**へ置く。

checkpointと同じrun directoryに置くのは、**別processが同じportを再構成できる**ようにする
ためである。cross-process resume（AC-C08-06）はcheckpointだけでは成立せず、portの構成も
復元できて初めて「同じrunの続き」になる。

時間はschema上すべてmillisecondsの整数で、ここで秒へ換算する（validatorがfloatを扱わない
ため。単位換算は決定論的で、既定値の解決ではない）。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ..identity.allowlist import ProducerAllowlist
from ..identity.errors import IdentityError
from ..identity.fs_permissions import replace_private_text, verify_private_file
from ..schema.projection import canonical_json
from ..schema.registry import validate
from ..schema.session import SESSION_CONFIG
from ..state import StatePaths, run_directory
from ..transport.gh import GhContext, RepoRef, RetryPolicy

CONFIG_FILE = "session.json"
SESSION_CONFIG_VERSION = 1


@dataclass(frozen=True)
class ConfigUnavailable:
    """session configを読めない（推測して既定値で埋めない）。"""

    detail: str


@dataclass(frozen=True)
class SessionConfig:
    """1 runの実行設定。**すべて明示値**で、本classは既定値を持たない。"""

    run_id: str
    repository: str
    number: int
    head_sha: str
    gh_command: tuple[str, ...]
    gh_workdir: Path
    gh_timeout_seconds: float
    gh_grace_seconds: float
    gh_env: Mapping[str, str]
    retry_max_attempts: int
    retry_backoff_seconds: float
    retry_max_wait_seconds: float
    search_since: str | None
    search_attempts: int
    search_backoff_seconds: float
    search_max_pages: int
    detection_head: str
    producer_logins: frozenset[str]
    max_result_bytes: int
    retry_budget: int
    speaker: str
    model: str
    user_speaker: str
    halt_grace_seconds: float

    @property
    def repo(self) -> RepoRef:
        """`owner/name`をC-05のrefへ写す（形式違反は構築時に拒否される）。"""
        owner, _, name = self.repository.partition("/")
        return RepoRef(owner=owner, name=name)

    @property
    def producers(self) -> ProducerAllowlist:
        """chain recordの正当な投稿者集合（C-06）。"""
        return ProducerAllowlist(logins=self.producer_logins)

    def context(self) -> GhContext:
        """gh実行の文脈（C-05）。"""
        return GhContext(
            gh_command=self.gh_command,
            env=dict(self.gh_env),
            workdir=self.gh_workdir,
            timeout_seconds=self.gh_timeout_seconds,
            grace_seconds=self.gh_grace_seconds,
        )

    def policy(self) -> RetryPolicy:
        """bounded retry方針。sleep / nowは実時間を使う（engineではなくruntimeの責務）。"""
        return RetryPolicy(
            max_attempts=self.retry_max_attempts,
            backoff_seconds=self.retry_backoff_seconds,
            max_wait_seconds=self.retry_max_wait_seconds,
            sleep=time.sleep,
            now=time.monotonic,
        )


def config_path(paths: StatePaths, run_id: str) -> Path:
    """run directory内のsession config path（checkpointと同じ場所）。"""
    return run_directory(paths, run_id) / CONFIG_FILE


def _seconds(payload: Mapping[str, object], name: str) -> float:
    return float(int(str(payload[name]))) / 1000.0


def _text(payload: Mapping[str, object], name: str) -> str:
    return str(payload[name])


def _int(payload: Mapping[str, object], name: str) -> int:
    return int(str(payload[name]))


def _strings(payload: Mapping[str, object], name: str) -> tuple[str, ...]:
    """検証済みpayloadのstring配列（schemaが要素型を保証する）。"""
    return tuple(str(item) for item in cast(Sequence[object], payload[name]))


def _env(payload: Mapping[str, object], name: str) -> dict[str, str]:
    """検証済みpayloadのstring map（schemaがkey / valueの型を保証する）。"""
    return {
        str(key): str(value)
        for key, value in cast(Mapping[str, object], payload[name]).items()
    }


def read_session_config(
    paths: StatePaths, run_id: str
) -> SessionConfig | ConfigUnavailable:
    """session configを読む（schema検証と権限検証を通す）。

    `run_id`が一致しないconfigは受理しない。別runの設定で走らせると、checkpointと
    portの指す先がずれる。
    """
    path = config_path(paths, run_id)
    if not path.is_file():
        return ConfigUnavailable(detail=f"session configが無い: {path.name}")
    try:
        verify_private_file(path)
    except IdentityError as error:
        return ConfigUnavailable(detail=f"session configが作成者限定でない: {error}")
    result = validate(SESSION_CONFIG, path.read_bytes())
    if not result.ok or result.payload is None:
        codes = ",".join(sorted(error.code for error in result.errors))
        return ConfigUnavailable(
            detail=f"session configが検証を通らない（stage={result.stage}, {codes}）"
        )
    payload = result.payload
    if payload.get("run_id") != run_id:
        return ConfigUnavailable(detail="session configのrun IDが一致しない")
    since = payload["search_since"]
    return SessionConfig(
        run_id=run_id,
        repository=_text(payload, "repository"),
        number=_int(payload, "number"),
        head_sha=_text(payload, "head_sha"),
        gh_command=_strings(payload, "gh_command"),
        gh_workdir=Path(_text(payload, "gh_workdir")),
        gh_timeout_seconds=_seconds(payload, "gh_timeout_ms"),
        gh_grace_seconds=_seconds(payload, "gh_grace_ms"),
        gh_env=_env(payload, "gh_env"),
        retry_max_attempts=_int(payload, "retry_max_attempts"),
        retry_backoff_seconds=_seconds(payload, "retry_backoff_ms"),
        retry_max_wait_seconds=_seconds(payload, "retry_max_wait_ms"),
        search_since=None if since is None else str(since),
        search_attempts=_int(payload, "search_attempts"),
        search_backoff_seconds=_seconds(payload, "search_backoff_ms"),
        search_max_pages=_int(payload, "search_max_pages"),
        detection_head=_text(payload, "detection_head"),
        producer_logins=frozenset(_strings(payload, "producer_logins")),
        max_result_bytes=_int(payload, "max_result_bytes"),
        retry_budget=_int(payload, "retry_budget"),
        speaker=_text(payload, "speaker"),
        model=_text(payload, "model"),
        user_speaker=_text(payload, "user_speaker"),
        halt_grace_seconds=_seconds(payload, "halt_grace_ms"),
    )


def write_session_config(
    paths: StatePaths, run_id: str, payload: Mapping[str, object]
) -> Path:
    """session configをrun directoryへ作成者限定で書く（schema検証を通してから）。

    書き手はrunを開始するcomponent（本Phaseはtest、将来はC-12）。engineは書かない。
    """
    result = validate(SESSION_CONFIG, canonical_json(payload).encode("utf-8"))
    if not result.ok:
        codes = ",".join(sorted(error.code for error in result.errors))
        raise ValueError(f"session configが検証を通らない（{codes}）")
    path = config_path(paths, run_id)
    replace_private_text(path, canonical_json(payload))
    return path
