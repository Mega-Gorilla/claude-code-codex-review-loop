# SPDX-License-Identifier: Apache-2.0
"""PR metadataの取得（advertised headの観測。C-05 / ADR-0012）。

resume（C-07）が「headが動いたか」を判定するには、PRが現在advertiseしているhead SHAを
構造化して観測できる必要がある。C-05にはcomment / threadの取得しかなかったため、
**read primitiveを1つだけ**追加する。

- 戻り値は`UnverifiedComment`と同じく**未検証metadata**であり、本componentは
  canonical recordを確定しない（AC-C05-05）。承認の失効判定はC-07、trust判定はC-04が行う
- 取得値は加工しない。`head.repo`がnull（fork元repositoryの削除等）の場合だけ空文字列へ
  写す。これはC-04の`TrustInput`が「head repositoryが空ならforkとして扱う」（fail closed）
  という既存規約と一致する
- 隔離checkoutのHEADとadvertised headの一致確認はC-09（AC-C09-04）の責務で、本moduleは
  advertised側だけを返す
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ErrorCategory
from .conversation import MAX_SINGLE_RESPONSE_BYTES
from .gh import GhContext, RepoRef, RetryPolicy, TransportError, run_gh_api_with_retry


@dataclass(frozen=True)
class UnverifiedPullRequest:
    """GitHubから取得した未検証のPR metadata。

    head_repository / base_repositoryは`owner/name`。取得できない場合は空文字列で、
    fork判定（C-04）はそれをforkとして扱う。
    """

    number: int
    state: str
    merged: bool
    head_sha: str
    head_ref: str
    head_repository: str
    base_sha: str
    base_ref: str
    base_repository: str
    author_login: str | None
    updated_at: str


def _as_dict(value: object, detail: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TransportError("metadata", detail, ErrorCategory.PERMANENT)
    return {str(key): item for key, item in value.items()}


def _as_str(value: object, detail: str) -> str:
    if not isinstance(value, str):
        raise TransportError("metadata", detail, ErrorCategory.PERMANENT)
    return value


def _repository_of(side: dict[str, object], label: str) -> str:
    """head / baseのrepository full name（取得できなければ空文字列 = fork扱い）。"""
    repository = side.get("repo")
    if repository is None:
        return ""
    return _as_str(_as_dict(repository, f"{label}.repoがobjectでもnullでもない").get("full_name"),
                   f"{label}.repo.full_nameが文字列でない")


def _side(data: dict[str, object], label: str) -> tuple[str, str, str]:
    """head / baseの(sha, ref, repository)を取り出す。"""
    side = _as_dict(data.get(label), f"PR応答の{label}がobjectでない")
    return (
        _as_str(side.get("sha"), f"{label}.shaが文字列でない"),
        _as_str(side.get("ref"), f"{label}.refが文字列でない"),
        _repository_of(side, label),
    )


def pull_request_from_json(data: object) -> UnverifiedPullRequest:
    """GitHubのPR応答を型へ写す（意味的な加工をしない）。"""
    body = _as_dict(data, "PR応答がobjectでない")
    number = body.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        raise TransportError("metadata", "PR応答のnumberが整数でない", ErrorCategory.PERMANENT)
    merged = body.get("merged")
    if not isinstance(merged, bool):
        raise TransportError("metadata", "PR応答のmergedがbooleanでない", ErrorCategory.PERMANENT)
    # 削除済みaccount等でuserはnullになり得る（commentと同じ扱い）
    user = body.get("user")
    author_login = None if user is None else _as_str(
        _as_dict(user, "PR応答のuserがobjectでもnullでもない").get("login"), "user.loginが文字列でない"
    )
    head_sha, head_ref, head_repository = _side(body, "head")
    base_sha, base_ref, base_repository = _side(body, "base")
    return UnverifiedPullRequest(
        number=number,
        state=_as_str(body.get("state"), "PR応答のstateが文字列でない"),
        merged=merged,
        head_sha=head_sha,
        head_ref=head_ref,
        head_repository=head_repository,
        base_sha=base_sha,
        base_ref=base_ref,
        base_repository=base_repository,
        author_login=author_login,
        updated_at=_as_str(body.get("updated_at"), "PR応答のupdated_atが文字列でない"),
    )


def get_pull_request(
    context: GhContext, repo: RepoRef, number: int, *, policy: RetryPolicy
) -> UnverifiedPullRequest:
    """PRのadvertised metadataを取得する（読取のみ。bounded retry）。"""
    response = run_gh_api_with_retry(
        context,
        (f"repos/{repo.slug}/pulls/{number}",),
        max_output_bytes=MAX_SINGLE_RESPONSE_BYTES,
        policy=policy,
    )
    return pull_request_from_json(response.body)
