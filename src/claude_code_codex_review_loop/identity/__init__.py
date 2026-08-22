# SPDX-License-Identifier: Apache-2.0
"""C-06 canonical record検証とcredential隔離（Phase 6）。

C-05が取得した未検証metadataから、検証済みcanonical recordを生成する**唯一の**component。
actor解決・allowlist照合（D-031、fail closed）・record chain検証（7条件）・ユーザー判断の
external evidence受理を担う。C-07以降のcomponentは本componentの検証済みrecordだけを入力に
する（C-05の`Unverified*`をworkflowの判断根拠にしない）。chain spec・binding導出・
allowlist意味論の正本はADR-0008。
"""

from .actor import ActorClass, ResolvedActor, normalize_login, resolve_actor
from .allowlist import (
    AcceptedUserDecision,
    AllowlistUnavailable,
    DecisionAllowlist,
    DecisionAllowlistState,
    DecisionContext,
    DecisionRejection,
    DecisionValidity,
    ProducerAllowlist,
    RejectedUserDecision,
    accept_user_decision,
    revalidate_user_decision,
)
from .errors import IdentityError
from .record_chain import (
    ChainCheckpoint,
    ChainPayload,
    ChainVerification,
    KnownRecord,
    ProbeFound,
    ProbeMissing,
    ProbeOutcome,
    VerifiedRecord,
    compose_record_marker_payload,
    probe_known_records,
    verify_record_chain,
)

__all__ = [
    "AcceptedUserDecision",
    "ActorClass",
    "AllowlistUnavailable",
    "ChainCheckpoint",
    "ChainPayload",
    "ChainVerification",
    "DecisionAllowlist",
    "DecisionAllowlistState",
    "DecisionContext",
    "DecisionRejection",
    "DecisionValidity",
    "IdentityError",
    "KnownRecord",
    "ProbeFound",
    "ProbeMissing",
    "ProbeOutcome",
    "ProducerAllowlist",
    "RejectedUserDecision",
    "ResolvedActor",
    "VerifiedRecord",
    "accept_user_decision",
    "compose_record_marker_payload",
    "normalize_login",
    "probe_known_records",
    "resolve_actor",
    "revalidate_user_decision",
    "verify_record_chain",
]
