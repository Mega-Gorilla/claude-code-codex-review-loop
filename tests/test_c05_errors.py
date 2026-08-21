# SPDX-License-Identifier: Apache-2.0
"""error分類（P-003 / ADR-0007の分類表）の受入test。"""

from __future__ import annotations

import pytest

from claude_code_codex_review_loop.errors import (
    ErrorCategory,
    classify_gh_failure,
    classify_graphql_error_type,
)


@pytest.mark.parametrize(
    "status,exit_code,retry_after,remaining_zero,expected",
    [
        (None, 4, False, False, ErrorCategory.AUTH),  # exit 4はrequest前の認証失敗
        (401, 4, True, True, ErrorCategory.AUTH),  # exit 4はstatusより優先
        (None, 2, False, False, ErrorCategory.PERMANENT),  # cancel
        (None, 1, False, False, ErrorCategory.TRANSIENT),  # status行なし（network断）
        (401, 1, False, False, ErrorCategory.AUTH),
        (403, 1, True, False, ErrorCategory.TRANSIENT),  # Retry-Afterあり
        (403, 1, False, True, ErrorCategory.TRANSIENT),  # remaining==0
        (403, 1, False, False, ErrorCategory.AUTH),  # 権限拒否
        (404, 1, False, False, ErrorCategory.NOT_FOUND),
        (410, 1, False, False, ErrorCategory.NOT_FOUND),
        (409, 1, False, False, ErrorCategory.TRANSIENT),
        (429, 1, False, False, ErrorCategory.TRANSIENT),
        (500, 1, False, False, ErrorCategory.TRANSIENT),
        (502, 1, False, False, ErrorCategory.TRANSIENT),
        (504, 1, False, False, ErrorCategory.TRANSIENT),
        (422, 1, False, False, ErrorCategory.PERMANENT),
        (400, 1, False, False, ErrorCategory.PERMANENT),
        (451, 1, False, False, ErrorCategory.PERMANENT),
    ],
)
def test_gh_failure_classification_table(
    status: int | None, exit_code: int, retry_after: bool, remaining_zero: bool, expected: ErrorCategory
) -> None:
    result = classify_gh_failure(
        status, exit_code, retry_after_present=retry_after, ratelimit_remaining_zero=remaining_zero
    )
    assert result is expected


@pytest.mark.parametrize(
    "error_type,expected",
    [
        ("RATE_LIMITED", ErrorCategory.TRANSIENT),
        ("NOT_FOUND", ErrorCategory.NOT_FOUND),
        ("FORBIDDEN", ErrorCategory.AUTH),
        ("SOMETHING_ELSE", ErrorCategory.PERMANENT),
    ],
)
def test_graphql_error_type_classification(error_type: str, expected: ErrorCategory) -> None:
    assert classify_graphql_error_type(error_type) is expected
