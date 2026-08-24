# SPDX-License-Identifier: Apache-2.0
"""artifact bindの照合の受入test（**AC-C07-05**。ADR-0012）。

「GitHub recordとlocal artifactが、いずれもapproved head SHAへbindされている」ことを
確認し、確認できないartifactはcacheとして破棄する（silentに直さず、理由を返す）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from c06_support.helpers import HEAD
from c07_support.helpers import RUN, state_paths, verified_chain

from claude_code_codex_review_loop.domain.values import RecordKind
from claude_code_codex_review_loop.identity.fs_permissions import create_private_dir, write_private_text
from claude_code_codex_review_loop.state import (
    ArtifactBinding,
    ArtifactCheck,
    ArtifactStatus,
    artifact_content_hash,
    read_artifact_bindings,
    run_directory,
    verify_artifact_bindings,
)

_NEW_HEAD = "b" * 40
_CONTENT = "artifactの中身"
_CONTENT_HASH = hashlib.sha256(_CONTENT.encode("utf-8")).hexdigest()
_BINDING = "cr:run-1:00000001:" + "0" * 64


def _binding(**overrides: object) -> ArtifactBinding:
    fields: dict[str, object] = {
        "path": "review.json",
        "kind": "REVIEW_RESULT",
        "content_hash": _CONTENT_HASH,
        "approved_head_sha": HEAD,
        "record_binding": _BINDING,
        "comment_id": "1001",
    }
    fields.update(overrides)
    return ArtifactBinding(**fields)  # type: ignore[arg-type]


def _verify(
    binding: ArtifactBinding, *, digest: str | None = _CONTENT_HASH, head: str = HEAD
) -> ArtifactCheck:
    checks = verify_artifact_bindings(
        (binding,), approved_head_sha=head, record_bindings={_BINDING}, digest=lambda path: digest
    )
    assert len(checks) == 1
    return checks[0]


class TestReadArtifactBindings:
    def test_reads_records(self) -> None:
        bindings = read_artifact_bindings(
            {
                "artifact_records": [
                    {
                        "path": "review.json",
                        "kind": "REVIEW_RESULT",
                        "content_hash": _CONTENT_HASH,
                        "approved_head_sha": HEAD,
                        "record_binding": _BINDING,
                    }
                ]
            }
        )
        assert len(bindings) == 1
        assert bindings[0].record_binding == _BINDING and bindings[0].comment_id is None

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"artifact_records": "x"},
            {"artifact_records": ["x"]},
            {"artifact_records": [{"path": "a", "kind": "k", "content_hash": "h"}]},
            {"artifact_records": [{"path": 1, "kind": "k", "content_hash": "h", "approved_head_sha": HEAD}]},
        ],
        ids=["absent", "not_list", "not_object", "missing_head", "non_string_path"],
    )
    def test_unreadable_entries_are_ignored(self, payload: dict[str, object]) -> None:
        assert read_artifact_bindings(payload) == ()


class TestVerifyArtifactBindings:
    def test_bound_artifact_is_usable(self) -> None:
        check = _verify(_binding())
        assert check.status is ArtifactStatus.BOUND and check.usable is True

    def test_stale_head_is_discarded_without_reading(self) -> None:
        """approved headが違うartifactはfileを読む前に破棄する。"""
        def _explode(path: str) -> str:
            raise AssertionError("head不一致のartifactを読んではならない")

        checks = verify_artifact_bindings(
            (_binding(approved_head_sha=_NEW_HEAD),),
            approved_head_sha=HEAD,
            record_bindings={_BINDING},
            digest=_explode,
        )
        assert checks[0].status is ArtifactStatus.STALE_HEAD and checks[0].usable is False

    @pytest.mark.parametrize(
        "record_binding",
        [None, "cr:run-1:00000009:" + "1" * 64],
        ids=["absent", "unknown"],
    )
    def test_artifact_without_verified_record_is_discarded(self, record_binding: str | None) -> None:
        check = _verify(_binding(record_binding=record_binding))
        assert check.status is ArtifactStatus.UNBOUND_RECORD

    def test_unreadable_artifact_is_discarded(self) -> None:
        check = _verify(_binding(), digest=None)
        assert check.status is ArtifactStatus.MISSING

    def test_content_mismatch_is_discarded(self) -> None:
        check = _verify(_binding(), digest="f" * 64)
        assert check.status is ArtifactStatus.CONTENT_MISMATCH
        assert check.detail is not None

    def test_record_bindings_come_from_the_verified_chain(self) -> None:
        """binding集合は検証済みrecordから作る（未検証markerを根拠にしない）。"""
        records = verified_chain([RecordKind.REVIEW_RESULT]).records
        checks = verify_artifact_bindings(
            (_binding(record_binding=records[0].key),),
            approved_head_sha=HEAD,
            record_bindings={record.key for record in records},
            digest=lambda path: _CONTENT_HASH,
        )
        assert checks[0].status is ArtifactStatus.BOUND


class TestArtifactContentHash:
    def _run_dir(self, tmp_path: Path) -> Path:
        return run_directory(state_paths(tmp_path), RUN)

    def test_hashes_private_file_under_base(self, tmp_path: Path) -> None:
        base = self._run_dir(tmp_path)
        write_private_text(base / "review.json", _CONTENT)
        assert artifact_content_hash(base, "review.json") == _CONTENT_HASH

    def test_nested_path_is_supported(self, tmp_path: Path) -> None:
        base = self._run_dir(tmp_path)
        create_private_dir(base / "artifacts")
        write_private_text(base / "artifacts" / "review.json", _CONTENT)
        assert artifact_content_hash(base, "artifacts/review.json") == _CONTENT_HASH

    def test_absent_file_returns_none(self, tmp_path: Path) -> None:
        assert artifact_content_hash(self._run_dir(tmp_path), "review.json") is None

    @pytest.mark.parametrize(
        "recorded",
        ["../escape.json", "sub/../../escape.json"],
        ids=["parent", "traversal"],
    )
    def test_path_outside_base_is_refused(self, tmp_path: Path, recorded: str) -> None:
        """base外を指すartifact pathは読まない（containment。fail closed）。"""
        base = self._run_dir(tmp_path)
        write_private_text(base.parent / "escape.json", _CONTENT)
        assert artifact_content_hash(base, recorded) is None

    def test_symlink_escaping_base_is_refused(self, tmp_path: Path) -> None:
        """base配下のsymlinkでも、解決先がbase外なら読まない。"""
        base = self._run_dir(tmp_path)
        outside = tmp_path.resolve() / "escape.json"
        outside.write_text(_CONTENT, encoding="utf-8")
        try:
            (base / "review.json").symlink_to(outside)
        except (OSError, NotImplementedError):  # pragma: no cover - symlink不可環境
            pytest.skip("symlinkを作成できない環境")
        assert artifact_content_hash(base, "review.json") is None

    def test_absolute_path_is_refused(self, tmp_path: Path) -> None:
        base = self._run_dir(tmp_path)
        target = base / "review.json"
        write_private_text(target, _CONTENT)
        assert artifact_content_hash(base, str(target)) is None

    def test_shared_file_entity_is_refused(self, tmp_path: Path) -> None:
        """作成者限定でない（外部と実体を共有する）artifactは信用しない（AC-C06-05）。"""
        base = self._run_dir(tmp_path)
        outside = tmp_path.resolve() / "shared.json"
        outside.write_text(_CONTENT, encoding="utf-8")
        try:
            (base / "review.json").hardlink_to(outside)
        except (OSError, NotImplementedError):  # pragma: no cover - hard link不可環境
            pytest.skip("hard linkを作成できない環境")
        assert artifact_content_hash(base, "review.json") is None

    def test_directory_is_not_an_artifact(self, tmp_path: Path) -> None:
        base = self._run_dir(tmp_path)
        create_private_dir(base / "artifacts")
        assert artifact_content_hash(base, "artifacts") is None
