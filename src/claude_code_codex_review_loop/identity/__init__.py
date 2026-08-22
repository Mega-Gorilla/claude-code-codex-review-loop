# SPDX-License-Identifier: Apache-2.0
"""C-06 canonical record検証とcredential隔離（Phase 6）。

C-05が取得した未検証metadataから、検証済みcanonical recordを生成する**唯一の**component。
actor解決・allowlist照合（D-031、fail closed）・record chain検証（7条件）・ユーザー判断の
external evidence受理を担う。C-07以降のcomponentは本componentの検証済みrecordだけを入力に
する（C-05の`Unverified*`をworkflowの判断根拠にしない）。chain spec・binding導出・
allowlist意味論の正本はADR-0008。

あわせてagentごとのcredential到達可能範囲（reviewer envの構築）、OS別のfile権限、
tool permissionのresume gateとauthority分離を持つ（P-009 / P-015。正本はADR-0009）。
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
from .auto_mode import AutoModeProbe, detect_auto_mode, probe_auto_mode
from .credentials import (
    COPY_ENV_NAMES,
    CredentialIsolationError,
    ReviewerHome,
    build_reviewer_env,
    prepare_reviewer_home,
)
from .errors import IdentityError
from .fs_permissions import (
    FsPermissionError,
    create_private_dir,
    verify_private_dir,
    verify_private_file,
    write_private_text,
)
from .permissions import (
    PermissionCheckpoint,
    PermissionResumeError,
    ResumeRejection,
    ResumeRequest,
    ResumeTicket,
    validate_permission_resume,
)
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
    "COPY_ENV_NAMES",
    "AcceptedUserDecision",
    "ActorClass",
    "AllowlistUnavailable",
    "AutoModeProbe",
    "ChainCheckpoint",
    "ChainPayload",
    "ChainVerification",
    "CredentialIsolationError",
    "DecisionAllowlist",
    "DecisionAllowlistState",
    "DecisionContext",
    "DecisionRejection",
    "DecisionValidity",
    "FsPermissionError",
    "IdentityError",
    "KnownRecord",
    "PermissionCheckpoint",
    "PermissionResumeError",
    "ProbeFound",
    "ProbeMissing",
    "ProbeOutcome",
    "ProducerAllowlist",
    "RejectedUserDecision",
    "ResolvedActor",
    "ResumeRejection",
    "ResumeRequest",
    "ResumeTicket",
    "ReviewerHome",
    "VerifiedRecord",
    "accept_user_decision",
    "build_reviewer_env",
    "compose_record_marker_payload",
    "create_private_dir",
    "detect_auto_mode",
    "normalize_login",
    "prepare_reviewer_home",
    "probe_auto_mode",
    "probe_known_records",
    "resolve_actor",
    "revalidate_user_decision",
    "validate_permission_resume",
    "verify_private_dir",
    "verify_private_file",
    "verify_record_chain",
    "write_private_text",
]
