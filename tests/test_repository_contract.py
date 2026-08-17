from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_project_identity_is_consistent() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "Claude Code–Codex Review Loop" in readme
    assert 'name = "claude-code-codex-review-loop"' in pyproject
    assert "`cc-review`" in readme


def test_license_and_migration_notice_are_present() -> None:
    apache_license = (ROOT / "LICENSE").read_text(encoding="utf-8")
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    legacy_license = (ROOT / "LICENSES" / "MIT-coding-review-agent-loop.txt").read_text(
        encoding="utf-8"
    )

    assert "Apache License" in apache_license
    assert "Version 2.0" in apache_license
    assert "coding-review-agent-loop" in notice
    assert "MIT License" in legacy_license


def test_target_experience_is_an_agreed_baseline() -> None:
    target = (ROOT / "docs" / "plans" / "target-experience.md").read_text(encoding="utf-8")

    assert "| Status | **Agreed** |" in target
    assert "cc-review" in target
