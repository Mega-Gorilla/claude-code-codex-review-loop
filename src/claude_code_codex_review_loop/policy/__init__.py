# SPDX-License-Identifier: Apache-2.0
"""C-04 security policy（Phase 4）。

GitHubへ問い合わせずに評価できる純粋なpolicy（credential redaction / permission
profile / trust rule）だけを持つ。I/O・network・時刻・環境に依存せず、actorの解決や
allowlist承認照合（D-031）はC-06 / identityが担う。設計判断（redaction patternの
管理・冪等性、trust判定のfail-closed意味論、profile値域とbypass非表現）はADR-0006を
正本とする。
"""

from .permission_profile import (
    ForbiddenFlagError,
    PermissionProfile,
    PolicyError,
    ProfilePurpose,
    ensure_argv_allowed,
    select_profile,
)
from .redaction import (
    REDACTION_PATTERNS,
    RedactionHit,
    RedactionPattern,
    RedactionResult,
    redact,
)
from .trust_rules import (
    RestrictedAction,
    TrustEvaluation,
    TrustInput,
    evaluate_trust,
)

__all__ = [
    "REDACTION_PATTERNS",
    "ForbiddenFlagError",
    "PermissionProfile",
    "PolicyError",
    "ProfilePurpose",
    "RedactionHit",
    "RedactionPattern",
    "RedactionResult",
    "RestrictedAction",
    "TrustEvaluation",
    "TrustInput",
    "ensure_argv_allowed",
    "evaluate_trust",
    "redact",
    "select_profile",
]
