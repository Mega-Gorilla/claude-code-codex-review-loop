# SPDX-License-Identifier: Apache-2.0

import os
import re
import tomllib
from pathlib import Path

import pytest

from claude_code_codex_review_loop import __version__

ROOT = Path(__file__).resolve().parents[1]

# Phase 0ではv X.Y.Zの厳格な形式のみを扱う。pre-release形式は必要になった時点で拡張する。
_RELEASE_TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")


def parse_release_tag(tag: str) -> str:
    """release tagからversionを取り出す。不正な形式はValueErrorとする。"""

    match = _RELEASE_TAG_RE.match(tag)
    if match is None:
        raise ValueError(f"release tagの形式が不正: {tag!r}（期待する形式: vX.Y.Z）")
    return match.group(1)


def release_tag_matches(tag: str, version: str) -> bool:
    """release tagとpackage versionの一致を判定する。"""

    return parse_release_tag(tag) == version


def test_pyproject_and_package_versions_match() -> None:
    """通常CI: pyproject.tomlのversionとpackageの__version__が一致する。"""

    with (ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    assert pyproject["project"]["version"] == __version__


def test_release_tag_parsing_accepts_valid_form() -> None:
    assert parse_release_tag("v0.1.0") == "0.1.0"
    assert parse_release_tag("v12.34.56") == "12.34.56"


def test_release_tag_matches_same_version() -> None:
    assert release_tag_matches("v0.1.0", "0.1.0") is True


def test_release_tag_detects_mismatch() -> None:
    assert release_tag_matches("v0.1.0", "0.2.0") is False


@pytest.mark.parametrize(
    "malformed",
    ["0.1.0", "v1.2", "v1.2.3.4", "v1.2.3-rc1", "V1.2.3", "v1.2.3 ", ""],
)
def test_release_tag_rejects_malformed_forms(malformed: str) -> None:
    with pytest.raises(ValueError):
        parse_release_tag(malformed)


@pytest.mark.skipif(not os.environ.get("RELEASE_TAG"), reason="tag buildでのみ実行する")
def test_release_tag_matches_package_version() -> None:
    """tag build: vX.Y.Z tagとpackage versionの不一致をfailにする。"""

    tag = os.environ["RELEASE_TAG"]
    assert release_tag_matches(tag, __version__), f"tag {tag!r} とpackage version {__version__!r} が一致しない"
