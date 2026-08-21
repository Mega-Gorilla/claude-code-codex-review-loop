# SPDX-License-Identifier: Apache-2.0
"""gh実行層（transport/gh.py）の受入test。fake ghをprocess境界に置く（P-011）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from c05_support.helpers import SleepRecorder, make_context, make_policy

from claude_code_codex_review_loop.errors import ErrorCategory
from claude_code_codex_review_loop.process import SpawnError
from claude_code_codex_review_loop.transport import (
    GhApiError,
    GhContext,
    GhTimeoutError,
    RepoRef,
    TransportError,
    run_gh_api,
    run_gh_api_with_retry,
)
from claude_code_codex_review_loop.transport.gh import parse_include_output

_PING = ("-X", "GET", "repos/o/r/issues/comments/1")


class TestContextValidation:
    def test_relative_gh_command_is_rejected(self, tmp_path: Path) -> None:
        """envは非継承でPATH解決に依存できないため、gh_commandの先頭は絶対path必須。"""
        with pytest.raises(TransportError) as excinfo:
            GhContext(
                gh_command=("gh",), env={}, workdir=tmp_path, timeout_seconds=5.0, grace_seconds=1.0
            )
        assert excinfo.value.stage == "validate"

    def test_empty_gh_command_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(TransportError):
            GhContext(gh_command=(), env={}, workdir=tmp_path, timeout_seconds=5.0, grace_seconds=1.0)

    def test_repo_ref_rejects_path_injection(self) -> None:
        with pytest.raises(TransportError):
            RepoRef(owner="o/../evil", name="r")
        with pytest.raises(TransportError):
            RepoRef(owner="o", name="r?x=1")


class TestIncludeParsing:
    def test_http2_with_reason(self) -> None:
        raw = b"HTTP/2.0 201 Created\r\nContent-Type: application/json\r\n\r\n{}"
        parsed = parse_include_output(raw)
        assert parsed is not None
        status, headers, body = parsed
        assert (status, headers["content-type"], body) == (201, "application/json", b"{}")

    def test_http11_without_reason(self) -> None:
        parsed = parse_include_output(b"HTTP/1.1 204\r\n\r\n")
        assert parsed is not None
        assert parsed[0] == 204

    def test_lf_only_boundary(self) -> None:
        parsed = parse_include_output(b"HTTP/2.0 200 OK\nRetry-After: 3\n\nbody")
        assert parsed is not None
        assert parsed[1]["retry-after"] == "3"
        assert parsed[2] == b"body"

    def test_header_names_are_casefolded(self) -> None:
        parsed = parse_include_output(b"HTTP/2.0 200 OK\r\nX-RateLimit-Remaining: 0\r\n\r\n{}")
        assert parsed is not None
        assert parsed[1]["x-ratelimit-remaining"] == "0"

    def test_missing_status_line_returns_none(self) -> None:
        assert parse_include_output(b"garbled output") is None
        assert parse_include_output(b"not a status\r\n\r\nbody") is None


class TestRunGhApi:
    def test_missing_executable_raises_spawn_error(self, tmp_path: Path) -> None:
        context = GhContext(
            gh_command=(str(tmp_path / "missing-gh"),),
            env={},
            workdir=tmp_path,
            timeout_seconds=5.0,
            grace_seconds=1.0,
        )
        with pytest.raises(SpawnError):
            run_gh_api(context, _PING, max_output_bytes=1024)

    def test_timeout_raises_gh_timeout(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="timeout", timeout_seconds=2.0)
        with pytest.raises(GhTimeoutError):
            run_gh_api(context, _PING, max_output_bytes=1024)

    def test_success_returns_parsed_response(self, tmp_path: Path) -> None:
        from c05_support.helpers import seed_state

        seed_state(
            tmp_path,
            comments=[
                {
                    "id": 1,
                    "issue": 5,
                    "html_url": "https://example.invalid/c/1",
                    "body": "x",
                    "created_at": "2026-08-21T00:00:00Z",
                    "updated_at": "2026-08-21T00:00:00Z",
                    "user": {"login": "alice"},
                }
            ],
        )
        context = make_context(tmp_path)
        response = run_gh_api(context, _PING, max_output_bytes=65536)
        assert response.status == 200
        assert isinstance(response.body, dict)

    @pytest.mark.parametrize(
        "scenario,expected_category,expected_status",
        [
            ("a401", ErrorCategory.AUTH, 401),
            ("f403", ErrorCategory.AUTH, 403),
            ("rl403", ErrorCategory.TRANSIENT, 403),
            ("nf404", ErrorCategory.NOT_FOUND, 404),
            ("u422", ErrorCategory.PERMANENT, 422),
            ("r429", ErrorCategory.TRANSIENT, 429),
            ("s500", ErrorCategory.TRANSIENT, 500),
        ],
    )
    def test_http_failures_are_classified(
        self, tmp_path: Path, scenario: str, expected_category: ErrorCategory, expected_status: int
    ) -> None:
        context = make_context(tmp_path, scenario=scenario)
        with pytest.raises(GhApiError) as excinfo:
            run_gh_api(context, _PING, max_output_bytes=65536)
        assert excinfo.value.category is expected_category
        assert excinfo.value.http_status == expected_status

    def test_exit4_without_output_is_auth(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="e4")
        with pytest.raises(GhApiError) as excinfo:
            run_gh_api(context, _PING, max_output_bytes=65536)
        assert (excinfo.value.category, excinfo.value.http_status) == (ErrorCategory.AUTH, None)

    def test_exit2_is_permanent(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="e2")
        with pytest.raises(GhApiError) as excinfo:
            run_gh_api(context, _PING, max_output_bytes=65536)
        assert excinfo.value.category is ErrorCategory.PERMANENT

    def test_no_status_line_on_failure_is_transient(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="noinclude")
        with pytest.raises(GhApiError) as excinfo:
            run_gh_api(context, _PING, max_output_bytes=65536)
        assert (excinfo.value.category, excinfo.value.http_status) == (ErrorCategory.TRANSIENT, None)

    def test_graphql_error_type_is_classified(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="graphql_error")
        env = dict(context.env)
        env["CC_REVIEW_FAKE_GH_GRAPHQL_TYPE"] = "RATE_LIMITED"
        context = GhContext(
            gh_command=context.gh_command,
            env=env,
            workdir=context.workdir,
            timeout_seconds=context.timeout_seconds,
            grace_seconds=context.grace_seconds,
        )
        with pytest.raises(GhApiError) as excinfo:
            run_gh_api(context, ("graphql", "-f", "query=q"), max_output_bytes=65536)
        assert excinfo.value.category is ErrorCategory.TRANSIENT

    def test_graphql_error_without_type_is_permanent(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="graphql_error")
        with pytest.raises(GhApiError) as excinfo:
            run_gh_api(context, ("graphql", "-f", "query=q"), max_output_bytes=65536)
        assert excinfo.value.category is ErrorCategory.PERMANENT

    def test_non_utf8_output_is_structured_error(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="nonutf8")
        with pytest.raises(TransportError) as excinfo:
            run_gh_api(context, _PING, max_output_bytes=65536)
        assert excinfo.value.stage == "encoding"

    def test_non_json_output_is_structured_error(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="nonjson")
        with pytest.raises(TransportError) as excinfo:
            run_gh_api(context, _PING, max_output_bytes=65536)
        assert excinfo.value.stage == "json"

    def test_oversized_output_is_rejected(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="oversize")
        with pytest.raises(TransportError) as excinfo:
            run_gh_api(context, _PING, max_output_bytes=1024)
        assert excinfo.value.stage == "size"

    def test_forbidden_flag_in_argv_is_rejected_before_spawn(self, tmp_path: Path) -> None:
        """全gh起動直前のensure_argv_allowed（ADR-0006のruntime choke接続）。"""
        from claude_code_codex_review_loop.policy import ForbiddenFlagError

        context = make_context(tmp_path)
        flag = "--" + "bypass" + "Permissions"
        with pytest.raises(ForbiddenFlagError):
            run_gh_api(context, ("-X", "GET", "repos/o/r", flag), max_output_bytes=1024)

    def test_temp_files_are_cleaned_up(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="nf404")
        with pytest.raises(GhApiError):
            run_gh_api(context, _PING, max_output_bytes=65536)
        leftovers = [p.name for p in tmp_path.glob("gh-*")]
        assert leftovers == []


class TestRetry:
    def test_transient_then_success_waits_retry_after(self, tmp_path: Path) -> None:
        from c05_support.helpers import seed_state

        seed_state(
            tmp_path,
            comments=[
                {
                    "id": 1,
                    "issue": 5,
                    "html_url": "u",
                    "body": "x",
                    "created_at": "t",
                    "updated_at": "t",
                    "user": {"login": "a"},
                }
            ],
        )
        context = make_context(tmp_path, scenario="s500,ok")
        recorder = SleepRecorder()
        response = run_gh_api_with_retry(
            context, _PING, max_output_bytes=65536, policy=make_policy(sleep=recorder)
        )
        assert response.status == 200
        assert recorder.calls == [1.0]  # fakeのRetry-After: 1を尊重

    def test_backoff_is_used_without_server_hint(self, tmp_path: Path) -> None:
        from c05_support.helpers import seed_state

        seed_state(tmp_path, comments=[])
        context = make_context(tmp_path, scenario="s500_noheader,nf404")
        recorder = SleepRecorder()
        with pytest.raises(GhApiError) as excinfo:
            run_gh_api_with_retry(
                context, _PING, max_output_bytes=65536, policy=make_policy(sleep=recorder, backoff_seconds=0.25)
            )
        assert excinfo.value.category is ErrorCategory.NOT_FOUND  # retry後の結果
        assert recorder.calls == [0.25]

    def test_ratelimit_reset_wait_is_computed_from_now(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="rl403,e2")
        recorder = SleepRecorder()
        with pytest.raises(GhApiError):
            run_gh_api_with_retry(
                context,
                _PING,
                max_output_bytes=65536,
                policy=make_policy(sleep=recorder, now=lambda: 1990.0, max_wait_seconds=60.0),
            )
        assert recorder.calls == [10.0]  # reset(2000) - now(1990)

    def test_wait_exceeding_cap_gives_up_immediately(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="rl403")
        recorder = SleepRecorder()
        with pytest.raises(GhApiError) as excinfo:
            run_gh_api_with_retry(
                context,
                _PING,
                max_output_bytes=65536,
                policy=make_policy(sleep=recorder, now=lambda: 0.0, max_wait_seconds=60.0),
            )
        assert excinfo.value.category is ErrorCategory.TRANSIENT
        assert recorder.calls == []  # 眠らずに即諦める

    def test_attempts_are_bounded(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="s500")
        recorder = SleepRecorder()
        with pytest.raises(GhApiError):
            run_gh_api_with_retry(
                context, _PING, max_output_bytes=65536, policy=make_policy(sleep=recorder, max_attempts=3)
            )
        assert len(recorder.calls) == 2  # 3回試行 = 待機2回

    def test_non_transient_is_not_retried(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="a401,ok")
        recorder = SleepRecorder()
        with pytest.raises(GhApiError) as excinfo:
            run_gh_api_with_retry(
                context, _PING, max_output_bytes=65536, policy=make_policy(sleep=recorder)
            )
        assert excinfo.value.category is ErrorCategory.AUTH
        assert recorder.calls == []

    def test_timeout_is_not_retried(self, tmp_path: Path) -> None:
        """timeoutは成否不明でありretryせず、冪等flow（marker検索）が回復する。"""
        context = make_context(tmp_path, scenario="timeout,ok", timeout_seconds=2.0)
        recorder = SleepRecorder()
        with pytest.raises(GhTimeoutError):
            run_gh_api_with_retry(
                context, _PING, max_output_bytes=65536, policy=make_policy(sleep=recorder)
            )
        assert recorder.calls == []


def test_gh_command_prefix_is_injectable() -> None:
    """fake ghの注入はargv prefix tupleで行う（OS分岐なし・sys.executableは絶対path）。"""
    assert Path(sys.executable).is_absolute()


class TestParsingUnits:
    def test_header_line_without_colon_is_skipped(self) -> None:
        raw = b"HTTP/2.0 200 OK\r\ngarbage-line-without-colon\r\nRetry-After: 2\r\n\r\n{}"
        parsed = parse_include_output(raw)
        assert parsed is not None
        assert parsed[1] == {"retry-after": "2"}

    def test_exit0_without_status_line_is_include_error(self, tmp_path: Path) -> None:
        context = make_context(tmp_path, scenario="ok_noinclude")
        with pytest.raises(TransportError) as excinfo:
            run_gh_api(context, _PING, max_output_bytes=1024)
        assert excinfo.value.stage == "include"

    def test_graphql_error_types_extraction(self) -> None:
        from claude_code_codex_review_loop.transport.gh import _graphql_error_types

        assert _graphql_error_types(["not a dict"]) is None
        assert _graphql_error_types({"errors": "not a list"}) is None
        assert _graphql_error_types({"errors": []}) is None
        assert _graphql_error_types({"errors": [{"type": "NOT_FOUND"}, {"message": "x"}, 5]}) == ("NOT_FOUND",)

    def test_parse_float_header_rejects_non_digits(self) -> None:
        from claude_code_codex_review_loop.transport.gh import _parse_float_header

        assert _parse_float_header({"retry-after": "soon"}, "retry-after") is None
        assert _parse_float_header({}, "retry-after") is None
        assert _parse_float_header({"retry-after": "30"}, "retry-after") == 30.0

