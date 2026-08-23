# SPDX-License-Identifier: Apache-2.0
"""tool permissionのresume gate（AC-C06-04）とauthority分離（AC-C06-11）。

tool permissionとworkflow承認は別のauthorityである。本moduleは「blockされた操作を
再実行してよいか」だけを判定し、**merge・follow-up Issue作成・仕様判断のいかなる承認も
生成しない**。そのためにdomainの承認event（`*Verified`）やevidence型を一切importせず、
戻り値`ResumeTicket`はrecord evidenceと型的に無関係である。

- 入力はlocal checkpointとlocal resume要求だけで、GitHub由来の値を引数に取らない
  （GitHub commentだけではlocal tool permissionを付与しない、を構造で保証する）
- 判定はcheckpoint値との**全field完全一致**。scopeの縮小も一致しない限り拒否する
  （停止点の操作だけを再実行する。曖昧な部分一致で範囲を推測しない）
- headが変わっていれば拒否（head binding不変条件）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique

from .errors import IdentityError


@unique
class ResumeRejection(Enum):
    """resume拒否の型付き理由。"""

    PERMISSION_ID_MISMATCH = "PERMISSION_ID_MISMATCH"
    HEAD_CHANGED = "HEAD_CHANGED"
    TOOL_MISMATCH = "TOOL_MISMATCH"
    SCOPE_CHANGED = "SCOPE_CHANGED"


class PermissionResumeError(IdentityError):
    """resume要求が停止点と一致しない（再実行を許可しない）。"""

    def __init__(self, reason: ResumeRejection) -> None:
        super().__init__("resume", f"resume要求が停止点と一致しない: {reason.value}")
        self.reason = reason


def _require_fields(stage: str, values: dict[str, str]) -> None:
    for name, value in values.items():
        if not value:
            raise IdentityError(stage, f"{name}は空にできない")


@dataclass(frozen=True)
class PermissionCheckpoint:
    """block時にcheckpointへ保存したpermission情報（envelopeのpermission sectionと対応）。

    値の意味論はC-06にとって不透明で、等価比較のみを行う。空値は「何にでも一致する
    停止点」を作ってしまうため構築時に拒否する（fail closed）。
    """

    permission_id: str
    blocked_tool: str
    requested_scope: str
    head_sha: str

    def __post_init__(self) -> None:
        _require_fields(
            "permission",
            {
                "permission_id": self.permission_id,
                "blocked_tool": self.blocked_tool,
                "requested_scope": self.requested_scope,
                "head_sha": self.head_sha,
            },
        )


@dataclass(frozen=True)
class ResumeRequest:
    """明示resume時の要求（ユーザーが標準permission UIで許可した後の再実行要求）。"""

    permission_id: str
    tool: str
    scope: str
    current_head_sha: str

    def __post_init__(self) -> None:
        _require_fields(
            "resume",
            {
                "permission_id": self.permission_id,
                "tool": self.tool,
                "scope": self.scope,
                "current_head_sha": self.current_head_sha,
            },
        )


@dataclass(frozen=True)
class ResumeTicket:
    """停止した操作**だけ**の再実行許可。workflow承認のevidenceではない（AC-C06-11）。"""

    permission_id: str
    tool: str
    scope: str
    head_sha: str


def validate_permission_resume(checkpoint: PermissionCheckpoint, request: ResumeRequest) -> ResumeTicket:
    """resume要求を停止点と照合し、一致する場合だけ再実行許可を返す（AC-C06-04）。"""
    if request.permission_id != checkpoint.permission_id:
        raise PermissionResumeError(ResumeRejection.PERMISSION_ID_MISMATCH)
    if request.current_head_sha != checkpoint.head_sha:
        raise PermissionResumeError(ResumeRejection.HEAD_CHANGED)
    if request.tool != checkpoint.blocked_tool:
        raise PermissionResumeError(ResumeRejection.TOOL_MISMATCH)
    if request.scope != checkpoint.requested_scope:
        raise PermissionResumeError(ResumeRejection.SCOPE_CHANGED)
    return ResumeTicket(
        permission_id=checkpoint.permission_id,
        tool=checkpoint.blocked_tool,
        scope=checkpoint.requested_scope,
        head_sha=checkpoint.head_sha,
    )
