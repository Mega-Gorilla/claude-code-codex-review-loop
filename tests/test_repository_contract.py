# SPDX-License-Identifier: Apache-2.0

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

DOCUMENT_PATHS = (
    "README.md",
    "docs/README.md",
    "docs/architecture/README.md",
    "docs/decisions/0001-independent-v2.md",
    "docs/decisions/0002-independent-reimplementation.md",
    "docs/plans/target-experience.md",
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


def test_superseded_fork_identifiers_are_not_referenced() -> None:
    """本repository以外のreview agent loop系forkを正式な参照として残さない。"""

    # 本repository`Mega-Gorilla/claude-code-codex-review-loop`以外の、
    # 同一owner配下のreview agent loop系repositoryを検出する。
    # 参考実装の出典（別owner）はADR-0002へ記録するため対象外とする。
    superseded_fork = re.compile(r"Mega-Gorilla/(?!claude-code-codex-review-loop)[\w.-]*review[\w.-]*loop")

    for path in DOCUMENT_PATHS:
        text = (ROOT / path).read_text(encoding="utf-8")
        found = superseded_fork.findall(text)
        assert not found, f"{path}: {found}"


def test_versioned_project_files_declare_apache_license() -> None:
    paths = (
        "README.md",
        ".github/workflows/test.yml",
        "pyproject.toml",
        "docs/README.md",
        "docs/architecture/README.md",
        "docs/decisions/0001-independent-v2.md",
        "docs/decisions/0002-independent-reimplementation.md",
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
