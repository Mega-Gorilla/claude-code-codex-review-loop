# SPDX-License-Identifier: Apache-2.0
"""C-07 local state（Phase 7）。

GitHub canonical conversationに対する**cache**としてのlocal state（checkpointとPR lock）
のI/Oを担う。GitHubで確認できないlocal出力を判断根拠にしないため、本packageは
「保存・読込・構造化した結果の提供」までを行い、GitHubとの照合・state再構築は
resume（同Phaseの後続PR）が行う。

file権限とatomic replaceはC-06（`identity.fs_permissions`）、schema検証はC-02、
pid生存判定はC-03（`process.is_process_alive`）を再利用する。配置と判断の正本は
ADR-0011。
"""

from .lock import (
    LockAcquired,
    LockCorrupt,
    LockHeld,
    LockInspection,
    LockOwner,
    LockResult,
    LockUnavailable,
    acquire_pr_lock,
    current_host,
    inspect_pr_lock,
    release_pr_lock,
)
from .paths import (
    CHECKPOINT_FILE_NAME,
    LOCK_SUFFIX,
    StatePathError,
    StatePaths,
    checkpoint_path,
    lock_path,
    prepare_state_root,
    repository_digest,
    run_directory,
)
from .store import (
    CheckpointLoaded,
    CheckpointLoadResult,
    CheckpointMigrationUnavailable,
    CheckpointMissing,
    CheckpointPermissionViolation,
    CheckpointSchemaInvalid,
    CheckpointStoreError,
    CheckpointUnreadable,
    load_checkpoint,
    save_checkpoint,
)

__all__ = [
    "CHECKPOINT_FILE_NAME",
    "LOCK_SUFFIX",
    "CheckpointLoadResult",
    "CheckpointLoaded",
    "CheckpointMigrationUnavailable",
    "CheckpointMissing",
    "CheckpointPermissionViolation",
    "CheckpointSchemaInvalid",
    "CheckpointStoreError",
    "CheckpointUnreadable",
    "LockAcquired",
    "LockCorrupt",
    "LockHeld",
    "LockInspection",
    "LockOwner",
    "LockResult",
    "LockUnavailable",
    "StatePathError",
    "StatePaths",
    "acquire_pr_lock",
    "checkpoint_path",
    "current_host",
    "inspect_pr_lock",
    "load_checkpoint",
    "lock_path",
    "prepare_state_root",
    "release_pr_lock",
    "repository_digest",
    "run_directory",
    "save_checkpoint",
]
