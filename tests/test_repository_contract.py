# SPDX-License-Identifier: Apache-2.0

import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

REPOSITORY_OWNER = "Mega-Gorilla"
CURRENT_REPOSITORY = "claude-code-codex-review-loop"

# SPDX表示を求めないfile。LICENSEとNOTICEは原文を保つ。
SPDX_EXEMPT = frozenset({"LICENSE", "NOTICE"})

# SPDX表示を求めるsuffix。
VERSIONED_SUFFIXES = frozenset(
    {".md", ".py", ".toml", ".yml", ".yaml", ".ps1", ".sh", ".psm1", ".psd1"}
)

# 選択移植した第三者成果物のpathと、保持する元licenseのSPDX ID。
# 登録fileはApache-2.0を強制しない。出典、理由、適用license、移植後testは、
# ADR-0002のSelective porting policyに従い移植PRへ記録する。
THIRD_PARTY_FILES: dict[str, str] = {}


def _tracked_files() -> tuple[str, ...]:
    """git管理下のfileを列挙する。手動listを持たず、追加漏れを構造的に防ぐ。"""

    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - git不在環境
        pytest.skip("git ls-files を実行できない")
    return tuple(line for line in result.stdout.splitlines() if line)


def versioned_files() -> tuple[str, ...]:
    """SPDX表示を求めるversion管理対象file。"""

    return tuple(
        path
        for path in _tracked_files()
        if Path(path).suffix in VERSIONED_SUFFIXES and Path(path).name not in SPDX_EXEMPT
    )


def project_files() -> tuple[str, ...]:
    """本project独自の成果物。Apache-2.0を必須とする。"""

    return tuple(path for path in versioned_files() if path not in THIRD_PARTY_FILES)


def _declared_spdx(path: str) -> str | None:
    """先頭3行からSPDX identifierを取り出す。"""

    text = (ROOT / path).read_text(encoding="utf-8")
    for line in text.splitlines()[:3]:
        match = re.search(r"SPDX-License-Identifier:\s*(\S+)", line)
        if match:
            return match.group(1).rstrip("-->").strip()
    return None


def document_paths() -> tuple[str, ...]:
    """文書として検査するMarkdown。"""

    return tuple(
        path
        for path in _tracked_files()
        if path.endswith(".md") and Path(path).name not in SPDX_EXEMPT
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

    for path in document_paths():
        text = (ROOT / path).read_text(encoding="utf-8")
        others = sorted(
            {
                name.removesuffix(".git")
                for name in reference.findall(text)
                if name.removesuffix(".git") != CURRENT_REPOSITORY
            }
        )
        assert not others, f"{path}: {others}"


def test_project_files_declare_apache_license() -> None:
    """本project独自の成果物はApache-2.0を宣言する。"""

    paths = project_files()
    assert len(paths) >= 15, f"discoveryが機能していない: {len(paths)}件"

    for path in paths:
        assert _declared_spdx(path) == "Apache-2.0", path


def test_third_party_files_declare_their_recorded_license() -> None:
    """選択移植した第三者成果物は、登録した元licenseを保持する。"""

    for path, expected in THIRD_PARTY_FILES.items():
        assert (ROOT / path).exists(), f"登録されたfileが存在しない: {path}"
        assert _declared_spdx(path) == expected, path


def test_cli_naming_is_unified_on_cc_review() -> None:
    """旧CLI名`agent-loop`と関連namespaceが設計baselineへ再混入しないことを確認する。"""

    # 参考実装のrepository名に含まれる`-agent-loop`は出典表記として許容するため、
    # 直前がhyphenまたは英数字の場合は検出対象から除外する。
    legacy_tokens = (
        re.compile(r"(?<![\w-])agent-loop"),
        re.compile(r"AGENT_LOOP"),
        re.compile(r"agent_loop"),
    )

    for path in document_paths():
        text = (ROOT / path).read_text(encoding="utf-8")
        for token in legacy_tokens:
            assert not token.search(text), f"{path}: {token.pattern}"
