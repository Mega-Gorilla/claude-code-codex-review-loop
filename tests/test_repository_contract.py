# SPDX-License-Identifier: Apache-2.0

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_project_identity_is_consistent() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "Claude Code–Codex Review Loop" in readme
    assert 'name = "claude-code-codex-review-loop"' in pyproject
    assert "`cc-review`" in readme


def test_apache_license_and_notice_are_present() -> None:
    apache_license = (ROOT / "LICENSE").read_text(encoding="utf-8")
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")

    assert "Apache License" in apache_license
    assert "Version 2.0" in apache_license
    assert "Claude Code–Codex Review Loop" in notice
    assert "Copyright 2026 Mega-Gorilla" in notice


def test_target_experience_is_an_agreed_baseline() -> None:
    target = (ROOT / "docs" / "plans" / "target-experience.md").read_text(encoding="utf-8")
    migration = (
        ROOT / "docs" / "decisions" / "migration-from-coding-review-agent-loop.md"
    ).read_text(encoding="utf-8")

    assert "| Status | **Agreed** |" in target
    assert "cc-review" in target


def test_fork_identifiers_are_not_referenced() -> None:
    """削除予定のfork repository固有の識別子を正式な参照として残さない。"""

    paths = (
        "README.md",
        "docs/README.md",
        "docs/architecture/README.md",
        "docs/decisions/0001-independent-v2.md",
        "docs/decisions/migration-from-coding-review-agent-loop.md",
        "docs/plans/target-experience.md",
    )
    # fork元の本家<https://github.com/wwind123/coding-review-agent-loop>は出典として許容する。
    fork_identifier = "Mega-Gorilla/coding-review-agent-loop"

    for path in paths:
        text = (ROOT / path).read_text(encoding="utf-8")
        assert fork_identifier not in text, path


def test_versioned_project_files_declare_apache_license() -> None:
    paths = (
        "README.md",
        ".github/workflows/test.yml",
        "pyproject.toml",
        "docs/README.md",
        "docs/architecture/README.md",
        "docs/decisions/0001-independent-v2.md",
        "docs/decisions/migration-from-coding-review-agent-loop.md",
        "docs/plans/target-experience.md",
        "plugin/README.md",
        "src/claude_code_codex_review_loop/__init__.py",
        "tests/test_repository_contract.py",
        "wrappers/README.md",
    )

    for path in paths:
        text = (ROOT / path).read_text(encoding="utf-8")
        assert any(
            "SPDX-License-Identifier: Apache-2.0" in line for line in text.splitlines()[:3]
        ), path
def test_cli_naming_is_unified_on_cc_review() -> None:
    """旧CLI名`agent-loop`と関連namespaceが設計baselineへ再混入しないことを確認する。"""

    paths = (
        "README.md",
        "docs/README.md",
        "docs/architecture/README.md",
        "docs/plans/target-experience.md",
        "plugin/README.md",
        "wrappers/README.md",
    )
    legacy_tokens = ("agent-loop", "AGENT_LOOP", "agent_loop")

    for path in paths:
        text = (ROOT / path).read_text(encoding="utf-8")
        # 旧repository名`coding-review-agent-loop`は出典表記として許容する。
        text = text.replace("coding-review-agent-loop", "")
        for token in legacy_tokens:
            assert token not in text, f"{path}: {token}"
