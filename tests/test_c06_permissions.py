# SPDX-License-Identifier: Apache-2.0
"""resume gateとauthority分離の受入test（AC-C06-04 / AC-C06-11）。"""

from __future__ import annotations

import ast
import inspect

import pytest

from claude_code_codex_review_loop.domain import values as domain_values
from claude_code_codex_review_loop.identity import (
    IdentityError,
    PermissionCheckpoint,
    PermissionResumeError,
    ResumeRejection,
    ResumeRequest,
    ResumeTicket,
    validate_permission_resume,
)
from claude_code_codex_review_loop.identity import permissions as permissions_module

_CHECKPOINT = PermissionCheckpoint(
    permission_id="perm-1",
    blocked_tool="Bash(git push)",
    requested_scope="push once to feature branch",
    head_sha="a" * 40,
)


def _request(**overrides: str) -> ResumeRequest:
    fields = {
        "permission_id": "perm-1",
        "tool": "Bash(git push)",
        "scope": "push once to feature branch",
        "current_head_sha": "a" * 40,
    }
    fields.update(overrides)
    return ResumeRequest(**fields)  # type: ignore[arg-type]


class TestConstruction:
    @pytest.mark.parametrize("field", ["permission_id", "blocked_tool", "requested_scope", "head_sha"])
    def test_empty_checkpoint_field_is_rejected(self, field: str) -> None:
        """空値は「何にでも一致する停止点」を作るため構築時に拒否する（fail closed）。"""
        fields = {
            "permission_id": "perm-1",
            "blocked_tool": "Bash(git push)",
            "requested_scope": "push once",
            "head_sha": "a" * 40,
        }
        fields[field] = ""
        with pytest.raises(IdentityError) as excinfo:
            PermissionCheckpoint(**fields)  # type: ignore[arg-type]
        assert excinfo.value.stage == "permission"

    @pytest.mark.parametrize("field", ["permission_id", "tool", "scope", "current_head_sha"])
    def test_empty_request_field_is_rejected(self, field: str) -> None:
        with pytest.raises(IdentityError) as excinfo:
            _request(**{field: ""})
        assert excinfo.value.stage == "resume"


class TestResumeGate:
    """AC-C06-04: resumeは停止点の操作だけを再実行する。"""

    def test_exact_match_yields_ticket(self) -> None:
        ticket = validate_permission_resume(_CHECKPOINT, _request())
        assert ticket == ResumeTicket(
            permission_id="perm-1",
            tool="Bash(git push)",
            scope="push once to feature branch",
            head_sha="a" * 40,
        )

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"permission_id": "perm-2"}, ResumeRejection.PERMISSION_ID_MISMATCH),
            ({"current_head_sha": "b" * 40}, ResumeRejection.HEAD_CHANGED),
            ({"tool": "Bash(git push --force)"}, ResumeRejection.TOOL_MISMATCH),
            ({"scope": "push anything"}, ResumeRejection.SCOPE_CHANGED),
        ],
    )
    def test_mismatch_is_rejected_with_reason(self, overrides: dict[str, str], reason: ResumeRejection) -> None:
        with pytest.raises(PermissionResumeError) as excinfo:
            validate_permission_resume(_CHECKPOINT, _request(**overrides))
        assert excinfo.value.reason is reason
        assert excinfo.value.stage == "resume"

    def test_narrower_scope_is_also_rejected(self) -> None:
        """縮小scopeも一致しない限り拒否する（範囲を推測しない決定的な等値比較）。"""
        with pytest.raises(PermissionResumeError) as excinfo:
            validate_permission_resume(_CHECKPOINT, _request(scope="push once"))
        assert excinfo.value.reason is ResumeRejection.SCOPE_CHANGED


class TestAuthoritySeparation:
    """AC-C06-11: tool permissionの許可はworkflow承認を生成・代行しない。"""

    def test_ticket_is_not_record_evidence(self) -> None:
        ticket = validate_permission_resume(_CHECKPOINT, _request())
        assert not isinstance(ticket, domain_values.RecordEvidence)
        assert not hasattr(ticket, "binding") and not hasattr(ticket, "kind")

    def test_module_does_not_reference_domain_approval_types(self) -> None:
        """承認event / evidence型をimportしないことを構造として固定する。"""
        tree = ast.parse(inspect.getsource(permissions_module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "domain" not in alias.name, alias.name
            elif isinstance(node, ast.ImportFrom):
                assert "domain" not in (node.module or ""), node.module
        for value in vars(permissions_module).values():
            module_name = getattr(value, "__module__", "")
            assert not module_name.startswith("claude_code_codex_review_loop.domain"), value

    def test_gate_takes_no_github_input(self) -> None:
        """GitHub由来の値を引数に取らない（commentだけでtool permissionを付与できない）。"""
        signature = inspect.signature(validate_permission_resume)
        assert list(signature.parameters) == ["checkpoint", "request"]
        request_fields = set(inspect.signature(ResumeRequest).parameters)
        assert request_fields == {"permission_id", "tool", "scope", "current_head_sha"}
