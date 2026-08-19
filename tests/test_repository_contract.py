# SPDX-License-Identifier: Apache-2.0

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

REPOSITORY_OWNER = "Mega-Gorilla"
CURRENT_REPOSITORY = "claude-code-codex-review-loop"

DOCUMENT_PATHS = (
    "CONTRIBUTING.md",
    "README.md",
    "docs/README.md",
    "docs/glossary.md",
    "docs/architecture/README.md",
    "docs/architecture/overview.md",
    "docs/decisions/0001-independent-v2.md",
    "docs/decisions/0002-independent-reimplementation.md",
    "docs/examples/final-report.md",
    "docs/examples/terminal-experience.md",
    "docs/plans/implementation-plan.md",
    "docs/plans/target-experience.md",
    "docs/research/reference-implementation-assessment.md",
    "plugin/README.md",
    "wrappers/README.md",
)


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
    assert "| Status | **Agreed** |" in target
    assert "cc-review" in target


def test_implementation_plan_derives_from_the_agreed_baseline() -> None:
    """implementation planがtarget experienceとroadmap Issueへ紐付いていることを確認する。"""

    plan = (ROOT / "docs" / "plans" / "implementation-plan.md").read_text(encoding="utf-8")

    assert "[target-experience.md](target-experience.md)" in plan
    assert "Issue #2" in plan


def test_documents_declare_their_authority() -> None:
    """informativeな文書が、要件と取り違えられない表示を持つことを確認する。"""

    informative = {
        "docs/examples/final-report.md": "Non-normative example",
        "docs/examples/terminal-experience.md": "Non-normative example",
        "docs/research/reference-implementation-assessment.md": "Research",
    }

    for path, marker in informative.items():
        text = (ROOT / path).read_text(encoding="utf-8")
        assert marker in text, path


def test_only_the_current_repository_is_referenced_for_the_owner() -> None:
    """同一owner配下では本repository以外を正式文書の参照として残さない。"""

    # repository参照を抽出し、完全一致で本repositoryだけを許可する。
    # 参考実装の出典は別ownerのため対象外で、ADR-0002へ限定して記録する。
    reference = re.compile(rf"{REPOSITORY_OWNER}/([\w.-]+)")

    for path in DOCUMENT_PATHS:
        text = (ROOT / path).read_text(encoding="utf-8")
        others = sorted(
            {
                name.removesuffix(".git")
                for name in reference.findall(text)
                if name.removesuffix(".git") != CURRENT_REPOSITORY
            }
        )
        assert not others, f"{path}: {others}"


def test_versioned_project_files_declare_apache_license() -> None:
    paths = DOCUMENT_PATHS + (
        ".github/workflows/test.yml",
        "pyproject.toml",
        "src/claude_code_codex_review_loop/__init__.py",
        "tests/test_repository_contract.py",
    )

    for path in paths:
        text = (ROOT / path).read_text(encoding="utf-8")
        assert any(
            "SPDX-License-Identifier: Apache-2.0" in line for line in text.splitlines()[:3]
        ), path


def test_cli_naming_is_unified_on_cc_review() -> None:
    """旧CLI名`agent-loop`と関連namespaceが設計baselineへ再混入しないことを確認する。"""

    # 参考実装のrepository名に含まれる`-agent-loop`は出典表記として許容するため、
    # 直前がhyphenまたは英数字の場合は検出対象から除外する。
    legacy_tokens = (
        re.compile(r"(?<![\w-])agent-loop"),
        re.compile(r"AGENT_LOOP"),
        re.compile(r"agent_loop"),
    )

    for path in DOCUMENT_PATHS:
        text = (ROOT / path).read_text(encoding="utf-8")
        for token in legacy_tokens:
            assert not token.search(text), f"{path}: {token.pattern}"
