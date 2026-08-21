# SPDX-License-Identifier: Apache-2.0
"""C-05の`gh` CLI実行層（唯一のGitHub I/O入口）。

- 実行はC-03の`run_tree`経由（explicit env・argv list・stdout/stderrはfile）。
  stdinはDEVNULL固定のため、投稿本文は必ずfile経由で渡す（P-005と一致）
- 全`gh api`呼び出しへ`--include`を付け、HTTP status行とheaderを構造化取得する。
  error分類はexit code・status・GraphQLの`errors[].type`のみを根拠にする（P-003）
- argvはSpawnSpec構築前に必ず`ensure_argv_allowed`を通す（P-006のruntime choke）
- `gh_command`はargv prefixとして注入可能（本番は`("gh"の絶対path,)`、testは
  `(sys.executable, fake_gh.py)`）。先頭は絶対path必須 — C-03のenvは非継承であり、
  子processのPATH解決に依存しないため
- timeout / retryの既定値は持たない（必須引数。既定値の解決はPhase 12のC-12）
- 規約の正本はADR-0007
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..errors import ErrorCategory, classify_gh_failure, classify_graphql_error_type
from ..policy import ensure_argv_allowed
from ..process import Completed, SpawnSpec, run_tree
from ..schema.validate import parse_json


class TransportError(Exception):
    """C-05の構造化errorの基底。stageは失敗した段階、categoryは分類。"""

    def __init__(self, stage: str, detail: str, category: ErrorCategory) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail
        self.category = category


class GhTimeoutError(TransportError):
    """ghの実行timeout。成否を推測せず、idempotency marker検索で回復する（AC-C05-02）。"""

    def __init__(self) -> None:
        super().__init__("timeout", "ghがtimeoutした（成否不明）", ErrorCategory.TRANSIENT)


class GhApiError(TransportError):
    """`gh api`の失敗。分類はexit code・HTTP status・GraphQL error typeによる。"""

    def __init__(
        self,
        category: ErrorCategory,
        *,
        http_status: int | None,
        exit_code: int,
        retry_after_seconds: float | None = None,
        ratelimit_reset_epoch: float | None = None,
    ) -> None:
        super().__init__(
            "api", f"ghが失敗した（exit={exit_code}, status={http_status}, category={category.value}）", category
        )
        self.http_status = http_status
        self.exit_code = exit_code
        self.retry_after_seconds = retry_after_seconds
        self.ratelimit_reset_epoch = ratelimit_reset_epoch


_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class RepoRef:
    """対象repository。ownerとnameはendpoint pathへ埋め込むため文字集合を検証する。"""

    owner: str
    name: str

    def __post_init__(self) -> None:
        for label, value in (("owner", self.owner), ("name", self.name)):
            if not _NAME_PATTERN.fullmatch(value):
                raise TransportError("validate", f"repositoryの{label}が不正な文字を含む", ErrorCategory.PERMANENT)

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class GhContext:
    """gh実行の文脈。envは明示指定のみが子へ渡る（C-03のexplicit env契約）。

    認証は認証済み`gh`（呼び出し側が構成したenv）へ委譲し、本moduleはcredentialを
    保持しない（P-015）。timeout / graceは必須引数で、既定値はC-12が解決する。
    """

    gh_command: tuple[str, ...]
    env: Mapping[str, str]
    workdir: Path
    timeout_seconds: float
    grace_seconds: float

    def __post_init__(self) -> None:
        if not self.gh_command:
            raise TransportError("validate", "gh_commandが空である", ErrorCategory.PERMANENT)
        if not Path(self.gh_command[0]).is_absolute():
            raise TransportError(
                "validate", "gh_commandの先頭は絶対pathでなければならない（envは非継承でPATH解決に依存しない）",
                ErrorCategory.PERMANENT,
            )


@dataclass(frozen=True)
class ApiResponse:
    """`gh api`の成功応答。headersはlowercase keyへ正規化済み。bodyはparse済みJSON。"""

    status: int
    headers: Mapping[str, str]
    body: object


@dataclass(frozen=True)
class GhResult:
    exit_code: int
    stdout: bytes


def _merged_env(context: GhContext) -> dict[str, str]:
    merged = dict(context.env)
    # 対話promptとupdate通知を無効化する（余計なI/Oとstderr汚染の防止）
    merged["GH_PROMPT_DISABLED"] = "1"
    merged["GH_NO_UPDATE_NOTIFIER"] = "1"
    return merged


def write_private_file(workdir: Path, prefix: str, text: str) -> Path:
    """作成者のみ読書き可能な一時fileへtextを書く（投稿本文のfile渡し用。P-005）。"""

    def _opener(target: str, flags: int) -> int:
        return os.open(target, flags, 0o600)

    path = workdir / f"{prefix}-{uuid.uuid4().hex}.txt"
    with open(path, "w", encoding="utf-8", newline="\n", opener=_opener) as handle:
        handle.write(text)
    return path


def run_gh(context: GhContext, argv_tail: Sequence[str], *, max_output_bytes: int) -> GhResult:
    """ghを1回実行し、exit codeとstdout bytesを返す。timeoutはGhTimeoutError。"""
    argv = (*context.gh_command, *argv_tail)
    ensure_argv_allowed(argv)
    stdout_path = context.workdir / f"gh-out-{uuid.uuid4().hex}.bin"
    stderr_path = context.workdir / f"gh-err-{uuid.uuid4().hex}.log"
    try:
        spec = SpawnSpec(
            argv=argv,
            cwd=context.workdir,
            env=_merged_env(context),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        result = run_tree(spec, timeout_seconds=context.timeout_seconds, grace_seconds=context.grace_seconds)
        if not isinstance(result, Completed):
            raise GhTimeoutError()
        size = stdout_path.stat().st_size
        if size > max_output_bytes:
            raise TransportError(
                "size", f"gh出力が上限を超えた（{size} > {max_output_bytes}）", ErrorCategory.PERMANENT
            )
        return GhResult(exit_code=result.exit_code, stdout=stdout_path.read_bytes())
    finally:
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)


_STATUS_LINE = re.compile(rb"^HTTP/[0-9.]+ (\d{3})(?: [^\r\n]*)?$")


def parse_include_output(raw: bytes) -> tuple[int, dict[str, str], bytes] | None:
    """`--include`出力をstatus / headers（lowercase）/ body bytesへ分解する。

    status行が無い（network断等でresponseが得られない）場合はNoneを返し、分類は
    exit codeだけで行う。redirectはgh内部でfollowされるためheader blockは常に1つ。
    """
    crlf = raw.find(b"\r\n\r\n")
    lf = raw.find(b"\n\n")
    candidates = [(pos, sep) for pos, sep in ((crlf, 4), (lf, 2)) if pos >= 0]
    if not candidates:
        return None
    boundary, sep_len = min(candidates)
    head = raw[:boundary]
    body = raw[boundary + sep_len :]
    lines = head.replace(b"\r\n", b"\n").split(b"\n")
    match = _STATUS_LINE.match(lines[0])
    if match is None:
        return None
    status = int(match.group(1))
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        if separator:
            headers[name.decode("utf-8", errors="replace").strip().casefold()] = value.decode(
                "utf-8", errors="replace"
            ).strip()
    return status, headers, body


def _parse_float_header(headers: Mapping[str, str], name: str) -> float | None:
    value = headers.get(name)
    if value is None or not value.isdigit():
        return None
    return float(value)


def _decode_json_body(body: bytes) -> object:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TransportError("encoding", "gh出力がUTF-8でない", ErrorCategory.PERMANENT) from exc
    try:
        return parse_json(text)
    except ValueError as exc:
        raise TransportError("json", "gh出力がJSONとして解釈できない", ErrorCategory.PERMANENT) from exc


def _graphql_error_types(body: object) -> tuple[str, ...] | None:
    """GraphQL失敗（HTTP 200 + exit 1）の`errors[].type`を構造化fieldとして取り出す。"""
    if not isinstance(body, dict):
        return None
    errors = body.get("errors")
    if not isinstance(errors, list) or not errors:
        return None
    types: list[str] = []
    for entry in errors:
        if isinstance(entry, dict) and isinstance(entry.get("type"), str):
            types.append(entry["type"])
    return tuple(types)


def run_gh_api(context: GhContext, rest: Sequence[str], *, max_output_bytes: int) -> ApiResponse:
    """`gh api --include ...`を実行し、成功応答を返す。失敗は分類済みのGhApiError。

    restには`api`と`--include`を含めない（本関数が常に付加し、--includeの
    付け忘れを構造的に防ぐ）。
    """
    result = run_gh(context, ("api", "--include", *rest), max_output_bytes=max_output_bytes)
    parsed = parse_include_output(result.stdout)
    if result.exit_code == 0:
        if parsed is None:
            raise TransportError("include", "成功応答からstatus行を解析できない", ErrorCategory.PERMANENT)
        status, headers, body = parsed
        return ApiResponse(status=status, headers=headers, body=_decode_json_body(body))
    if parsed is None:
        raise GhApiError(
            classify_gh_failure(None, result.exit_code, retry_after_present=False, ratelimit_remaining_zero=False),
            http_status=None,
            exit_code=result.exit_code,
        )
    status, headers, body = parsed
    if status == 200:
        graphql_types = _graphql_error_types(_decode_json_body(body))
        category = (
            classify_graphql_error_type(graphql_types[0]) if graphql_types else ErrorCategory.PERMANENT
        )
        raise GhApiError(category, http_status=status, exit_code=result.exit_code)
    retry_after = _parse_float_header(headers, "retry-after")
    reset_epoch = _parse_float_header(headers, "x-ratelimit-reset")
    remaining_zero = headers.get("x-ratelimit-remaining") == "0"
    raise GhApiError(
        classify_gh_failure(
            status,
            result.exit_code,
            retry_after_present=retry_after is not None,
            ratelimit_remaining_zero=remaining_zero,
        ),
        http_status=status,
        exit_code=result.exit_code,
        retry_after_seconds=retry_after,
        ratelimit_reset_epoch=reset_epoch if remaining_zero else None,
    )


@dataclass(frozen=True)
class RetryPolicy:
    """読み取り系のbounded retry方針。値の既定はC-12が解決する（本moduleは持たない）。

    sleep / nowを注入可能にし、testを実時間から独立させる。
    """

    max_attempts: int
    backoff_seconds: float
    max_wait_seconds: float
    sleep: Callable[[float], None]
    now: Callable[[], float]


def run_gh_api_with_retry(
    context: GhContext,
    rest: Sequence[str],
    *,
    max_output_bytes: int,
    policy: RetryPolicy,
) -> ApiResponse:
    """TRANSIENTのみをbounded retryする。待機はserverのretry情報を優先する。

    待機時間の優先順: Retry-After（秒）-> rate limit resetまでの残り -> 固定backoff。
    待機がpolicy.max_wait_secondsを超える場合は眠らずに即座に諦める（primary rate
    limitのresetは1時間先があり得るため）。timeout（GhTimeoutError）はretryしない
    （成否不明。冪等flowが回復する）。
    """
    attempt = 1
    while True:
        try:
            return run_gh_api(context, rest, max_output_bytes=max_output_bytes)
        except GhApiError as exc:
            if exc.category is not ErrorCategory.TRANSIENT or attempt >= policy.max_attempts:
                raise
            if exc.retry_after_seconds is not None:
                wait = exc.retry_after_seconds
            elif exc.ratelimit_reset_epoch is not None:
                wait = max(0.0, exc.ratelimit_reset_epoch - policy.now())
            else:
                wait = policy.backoff_seconds
            if wait > policy.max_wait_seconds:
                raise
            policy.sleep(wait)
            attempt += 1
