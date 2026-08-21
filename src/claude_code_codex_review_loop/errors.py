# SPDX-License-Identifier: Apache-2.0
"""構造化error分類（P-003）。

分類の根拠はexit code・HTTP status・構造化field（`gh api`のstatus行、GraphQLの
`errors[].type`）に限定し、出力文字列の部分一致を根拠にしない。分類表の判断
（403のrate limit判別、status不明時のTRANSIENT扱い等）はADR-0007を正本とする。

componentごとの具体的な例外型は各component側（transport等）が定義し、本moduleは
分類の語彙（ErrorCategory）と純粋な分類関数だけを持つ。
"""

from __future__ import annotations

from enum import Enum, unique


@unique
class ErrorCategory(Enum):
    """失敗の分類。retry可否とrecoveryの起点になる。"""

    TRANSIENT = "TRANSIENT"  # bounded retryの対象（rate limit / 5xx / network断）
    NOT_FOUND = "NOT_FOUND"  # 対象が存在しない（404 / 410。既知recordの消失検知に使う）
    AUTH = "AUTH"  # 認証・認可の失敗（再認証が必要）
    PERMANENT = "PERMANENT"  # retryしても解消しない失敗


# ghのexit code（`gh help exit-codes`）: 0=成功 / 1=失敗全般 / 2=cancel / 4=認証が必要
_GH_EXIT_AUTH = 4
_GH_EXIT_CANCEL = 2


def classify_gh_failure(
    http_status: int | None,
    exit_code: int,
    *,
    retry_after_present: bool,
    ratelimit_remaining_zero: bool,
) -> ErrorCategory:
    """`gh`失敗の分類（ADR-0007の分類表）。

    - exit 4はHTTP requestすら発行されない認証未設定のため、status不在でもAUTH
    - 403はrate limitの構造化根拠（Retry-After header、またはx-ratelimit-remaining==0）が
      ある場合のみTRANSIENT、それ以外はAUTH
    - status行が取得できないexit 1（network断等）はTRANSIENT（bounded retryが
      誤分類の被害を有限化する）
    """
    if exit_code == _GH_EXIT_AUTH:
        return ErrorCategory.AUTH
    if exit_code == _GH_EXIT_CANCEL:
        return ErrorCategory.PERMANENT
    if http_status is None:
        return ErrorCategory.TRANSIENT
    if http_status == 401:
        return ErrorCategory.AUTH
    if http_status == 403:
        if retry_after_present or ratelimit_remaining_zero:
            return ErrorCategory.TRANSIENT
        return ErrorCategory.AUTH
    if http_status in (404, 410):
        return ErrorCategory.NOT_FOUND
    if http_status in (409, 429) or http_status >= 500:
        return ErrorCategory.TRANSIENT
    return ErrorCategory.PERMANENT


# GraphQLはHTTP 200 + exit 1 + errors[].typeで失敗を表す。typeは完全一致で分類する
_GRAPHQL_TYPE_MAP: dict[str, ErrorCategory] = {
    "RATE_LIMITED": ErrorCategory.TRANSIENT,
    "NOT_FOUND": ErrorCategory.NOT_FOUND,
    "FORBIDDEN": ErrorCategory.AUTH,
}


def classify_graphql_error_type(error_type: str) -> ErrorCategory:
    """GraphQL errorの`type` field（構造化値）の完全一致による分類。未知typeはPERMANENT。"""
    return _GRAPHQL_TYPE_MAP.get(error_type, ErrorCategory.PERMANENT)
