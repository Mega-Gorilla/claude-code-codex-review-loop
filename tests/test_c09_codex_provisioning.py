# SPDX-License-Identifier: Apache-2.0
"""C-09 provisioning成果物の複製（ADR-0029）のhermetic test。実Codex・実`~/.codex`は使わない。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claude_code_codex_review_loop.identity import create_private_dir
from claude_code_codex_review_loop.identity.fs_permissions import verify_private_file
from claude_code_codex_review_loop.runtime import codex_provisioning as module
from claude_code_codex_review_loop.runtime.codex_canary import prepare_codex_canary_home
from claude_code_codex_review_loop.runtime.codex_provisioning import (
    MARKER_RELATIVE_PATH,
    MAX_ARTIFACT_BYTES,
    USERS_RELATIVE_PATH,
    ProvisioningError,
    mirror_provisioning_artifacts,
)

_MARKER = {
    "version": 5,
    "offline_username": "CodexSandboxOffline",
    "online_username": "CodexSandboxOnline",
    "created_at": "2026-05-06T11:55:51Z",
    "proxy_ports": [],
    "allow_local_binding": False,
    "read_roots": [],
    "write_roots": [],
}
_USERS = {
    "version": 5,
    "offline": {"username": "CodexSandboxOffline", "password": "ZmFrZQ=="},
    "online": {"username": "CodexSandboxOnline", "password": "ZmFrZQ=="},
}


def _home(tmp_path: Path):
    private = tmp_path / "private"
    create_private_dir(private)
    workspace = (tmp_path / "checkout").resolve()
    real_repository = (tmp_path / "real-repository").resolve()
    state = (tmp_path / "state").resolve()
    for path in (workspace, real_repository, state):
        path.mkdir()
    return prepare_codex_canary_home(
        private_root=private.resolve(),
        name="codex-home",
        workspace_root=workspace,
        protected_roots=(real_repository, state),
    )


def _source(tmp_path: Path, marker: object = _MARKER, users: object = _USERS) -> Path:
    source = (tmp_path / "authoritative").resolve()
    (source / ".sandbox").mkdir(parents=True)
    (source / ".sandbox-secrets").mkdir()
    (source / MARKER_RELATIVE_PATH).write_text(
        marker if isinstance(marker, str) else json.dumps(marker), encoding="utf-8"
    )
    (source / USERS_RELATIVE_PATH).write_text(users if isinstance(users, str) else json.dumps(users), encoding="utf-8")
    (source / "auth.json").write_text('{"tokens": "not-copied"}', encoding="utf-8")
    return source


class TestMirrorProvisioningArtifacts:
    def test_copies_only_the_two_artifacts_verbatim_as_private_files(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        source = _source(tmp_path)
        mirror = mirror_provisioning_artifacts(home, source)
        assert mirror.source == source and (mirror.marker_version, mirror.users_version) == (5, 5)
        assert mirror.marker_path == home.root / MARKER_RELATIVE_PATH
        assert mirror.users_path == home.root / USERS_RELATIVE_PATH
        for copied, original in ((mirror.marker_path, MARKER_RELATIVE_PATH), (mirror.users_path, USERS_RELATIVE_PATH)):
            assert copied.read_text(encoding="utf-8") == (source / original).read_text(encoding="utf-8")
        verify_private_file(mirror.marker_path)
        verify_private_file(mirror.users_path)
        # provider認証・configは複製しない。専用homeの構成はconfigと2 dirだけ
        assert not (home.root / "auth.json").exists()
        assert sorted(path.name for path in home.root.iterdir()) == [".sandbox", ".sandbox-secrets", "config.toml"]
        assert sorted(path.name for path in (home.root / ".sandbox").iterdir()) == ["setup_marker.json"]

    def test_config_digest_is_unaffected(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        before = home.config_path.read_bytes()
        mirror_provisioning_artifacts(home, _source(tmp_path))
        assert home.config_path.read_bytes() == before

    def test_mirroring_twice_fails_closed_instead_of_overwriting(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        source = _source(tmp_path)
        mirror_provisioning_artifacts(home, source)
        with pytest.raises(ProvisioningError) as stopped:
            mirror_provisioning_artifacts(home, source)
        assert stopped.value.stage == "destination"

    @pytest.mark.parametrize("kind", ("relative", "missing", "file", "symlink"))
    def test_invalid_source_is_rejected(self, tmp_path: Path, kind: str) -> None:
        home = _home(tmp_path)
        real = _source(tmp_path)
        link = tmp_path / "link"
        if kind == "symlink":
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError:
                pytest.skip("symlinkを作成できない環境")
        candidates = {
            "relative": Path("relative"),
            "missing": (tmp_path / "missing").resolve(),
            "file": (real / "auth.json"),
            "symlink": link,
        }
        with pytest.raises(ProvisioningError) as stopped:
            mirror_provisioning_artifacts(home, candidates[kind])
        assert stopped.value.stage == "source"
        assert not (home.root / ".sandbox").exists()

    @pytest.mark.parametrize(
        ("marker", "users", "stage"),
        (
            ("{not json", _USERS, "marker"),
            (_MARKER, "{not json", "users"),
            ("[]", _USERS, "marker"),
            (_MARKER, "[]", "users"),
            ({**_MARKER, "version": "5"}, _USERS, "version"),
            ({**_MARKER, "version": 0}, _USERS, "version"),
            ({**_MARKER, "version": True}, _USERS, "version"),
            (_MARKER, {**_USERS, "version": None}, "version"),
            ({**_MARKER, "offline_username": ""}, _USERS, "usernames"),
            ({k: v for k, v in _MARKER.items() if k != "online_username"}, _USERS, "usernames"),
            ({**_MARKER, "proxy_ports": [3128]}, _USERS, "proxy"),
            ({**_MARKER, "allow_local_binding": True}, _USERS, "proxy"),
            ({k: v for k, v in _MARKER.items() if k != "proxy_ports"}, _USERS, "proxy"),
        ),
        ids=(
            "marker_not_json", "users_not_json", "marker_not_object", "users_not_object", "version_string",
            "version_zero", "version_bool", "users_version_missing", "offline_username_empty",
            "online_username_missing", "proxy_ports", "allow_local_binding", "proxy_ports_missing",
        ),
    )
    def test_invalid_artifacts_are_rejected_before_anything_is_written(
        self, tmp_path: Path, marker: object, users: object, stage: str
    ) -> None:
        home = _home(tmp_path)
        with pytest.raises(ProvisioningError) as stopped:
            mirror_provisioning_artifacts(home, _source(tmp_path, marker, users))
        assert stopped.value.stage == stage
        assert not (home.root / ".sandbox").exists() and not (home.root / ".sandbox-secrets").exists()

    @pytest.mark.parametrize("which", ("marker", "users"))
    def test_missing_symlinked_or_oversized_artifact_is_rejected(self, tmp_path: Path, which: str) -> None:
        home = _home(tmp_path)
        source = _source(tmp_path)
        target = source / (MARKER_RELATIVE_PATH if which == "marker" else USERS_RELATIVE_PATH)
        target.write_text("x" * (MAX_ARTIFACT_BYTES + 1), encoding="utf-8")
        with pytest.raises(ProvisioningError) as oversized:
            mirror_provisioning_artifacts(home, source)
        assert oversized.value.stage == which
        target.unlink()
        with pytest.raises(ProvisioningError) as missing:
            mirror_provisioning_artifacts(home, source)
        assert missing.value.stage == which
        try:
            target.symlink_to(tmp_path / "elsewhere.json")
        except OSError:
            pytest.skip("symlinkを作成できない環境")
        with pytest.raises(ProvisioningError) as linked:
            mirror_provisioning_artifacts(home, source)
        assert linked.value.stage == which

    def test_invalid_utf8_artifact_is_rejected(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        source = _source(tmp_path)
        (source / USERS_RELATIVE_PATH).write_bytes(b'{"version": 5, "x": "\xff"}')
        with pytest.raises(ProvisioningError) as stopped:
            mirror_provisioning_artifacts(home, source)
        assert stopped.value.stage == "users"

    def test_source_probe_os_error_is_classified(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _home(tmp_path)
        source = _source(tmp_path)
        original = Path.is_dir

        def failing(self: Path) -> bool:
            if self == source:
                raise OSError("test")
            return original(self)

        monkeypatch.setattr(Path, "is_dir", failing)
        with pytest.raises(ProvisioningError) as stopped:
            mirror_provisioning_artifacts(home, source)
        assert stopped.value.stage == "source"

    def test_unreadable_artifact_is_classified(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _home(tmp_path)
        source = _source(tmp_path)
        original = Path.read_bytes

        def failing(self: Path) -> bytes:
            if self.name == "setup_marker.json":
                raise PermissionError("test")
            return original(self)

        monkeypatch.setattr(Path, "read_bytes", failing)
        with pytest.raises(ProvisioningError) as stopped:
            mirror_provisioning_artifacts(home, source)
        assert stopped.value.stage == "marker"

    def test_destination_write_failure_is_classified(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = _home(tmp_path)
        monkeypatch.setattr(
            module, "write_private_text", lambda *args: (_ for _ in ()).throw(module.FsPermissionError("write", "test"))
        )
        with pytest.raises(ProvisioningError) as stopped:
            mirror_provisioning_artifacts(home, _source(tmp_path))
        assert stopped.value.stage == "destination"

    def test_errors_never_carry_artifact_contents(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        secret = "super-secret-blob"
        with pytest.raises(ProvisioningError) as stopped:
            source = _source(tmp_path, {**_MARKER, "version": 0}, {**_USERS, "blob": secret})
            mirror_provisioning_artifacts(home, source)
        assert secret not in str(stopped.value) and os.fspath(tmp_path) not in str(stopped.value)
