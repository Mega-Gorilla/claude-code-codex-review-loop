# SPDX-License-Identifier: Apache-2.0
"""Issue #52 PR-B1: provider選択のcodecと事前検証契約（実CLIは起動しない）。"""

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, dataclass, field, replace

import pytest
from c02_support.helpers import REPRESENTATIVE

from claude_code_codex_review_loop.runtime.agent_adapter import (
    AdapterKey,
    AdapterReadiness,
    preflight_selection,
)
from claude_code_codex_review_loop.runtime.agent_selection import (
    AgentSelection,
    RoleSelection,
    SelectionRejected,
    decode_selection,
    restore_selection,
)
from claude_code_codex_review_loop.schema import REGISTRY, SchemaKind, repair_and_validate


def payload():
    return json.loads(json.dumps(REPRESENTATIVE[SchemaKind.AGENT_SELECTION]))


def raw(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def selection(coder="claude", reviewer="codex", mode="active"):
    value = payload()
    value["coder"].update(provider=coder, mode=mode)
    value["reviewer"]["provider"] = reviewer
    result = decode_selection(raw(value))
    assert isinstance(result, AgentSelection), result
    return result


def restored(data, **kwargs):
    target = dict(run_id="run-1", repository="owner/repo", number=12)
    target.update(kwargs)
    return restore_selection(data, **target)


@dataclass
class FakeProbe:
    key: AdapterKey
    failure: str | None = None
    calls: list[RoleSelection] = field(default_factory=list)

    def check(self, selected):
        self.calls.append(selected)
        return AdapterReadiness(self.failure)


def probes_for(selected):
    return [
        FakeProbe(AdapterKey(role, agent.provider, agent.mode, agent.safety_profile, agent.adapter_contract_version))
        for role, agent in (("coder", selected.coder), ("reviewer", selected.reviewer))
    ]


@pytest.mark.parametrize("coder", ["claude", "codex"])
@pytest.mark.parametrize("reviewer", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["active", "headless"])
def test_four_combinations_and_coder_modes_round_trip(coder, reviewer, mode):
    selected = selection(coder, reviewer, mode)
    assert decode_selection(selected.to_bytes()) == selected
    assert restored(selected.to_bytes(), requested=selected) == selected
    assert len(selected.digest) == 64
    assert selected.coder is not selected.reviewer
    probes = probes_for(selected)
    active = coder if mode == "active" else None
    assert preflight_selection(selected, probes, active_provider=active) is None
    assert probes[0] is not probes[1]
    assert probes[0].calls == [selected.coder]
    assert probes[1].calls == [selected.reviewer]


def test_model_is_independent_of_provider_and_same_model_is_allowed():
    selected = selection("codex", "codex")
    selected = replace(selected, reviewer=replace(selected.reviewer, model=selected.coder.model))
    assert decode_selection(selected.to_bytes()) == selected
    assert preflight_selection(selected, probes_for(selected), active_provider="codex") is None


def test_snapshot_is_detached_from_mutable_input_and_deeply_frozen():
    value = payload()
    selected = decode_selection(raw(value))
    assert isinstance(selected, AgentSelection)
    value["coder"]["provider"] = "codex"
    assert selected.coder.provider == "claude"
    with pytest.raises(FrozenInstanceError):
        selected.number = 99
    with pytest.raises(FrozenInstanceError):
        selected.coder.model = "changed"


def test_canonical_digest_ignores_json_format_and_key_order():
    value = payload()
    left = decode_selection(raw(value))
    right = decode_selection(json.dumps(dict(reversed(list(value.items()))), indent=2).encode())
    assert left.digest == right.digest


@pytest.mark.parametrize("path,value", [
    (("coder", "provider"), "unknown"),
    (("reviewer", "provider"), "Claude"),
    (("coder", "mode"), "fresh"),
    (("reviewer", "mode"), "active"),
    (("reviewer", "mode"), "headless"),
    (("coder", "safety_profile"), "reviewer_isolated"),
    (("reviewer", "safety_profile"), "coder_workspace"),
    (("coder", "adapter_contract_version"), 0),
    (("reviewer", "adapter_contract_version"), -1),
    (("coder", "adapter_contract_version"), True),
    (("reviewer", "adapter_contract_version"), 1.0),
    (("coder", "model"), ""),
    (("coder", "model"), "   "),
    (("reviewer", "model"), "a\nb"),
    (("reviewer", "model"), "a" * 201),
    (("coder", "model"), None),
    (("coder",), []),
    (("reviewer",), "codex"),
    (("number",), 0),
    (("number",), -1),
    (("number",), True),
    (("schema_version",), 2),
    (("schema_version",), 1.0),
])
def test_invalid_settings_are_rejected(path, value):
    data = payload()
    target = data if len(path) == 1 else data[path[0]]
    target[path[-1]] = value
    result = decode_selection(raw(data))
    assert isinstance(result, SelectionRejected) and result.code == "selection_invalid"


@pytest.mark.parametrize("field", ["provider", "model", "mode", "safety_profile", "adapter_contract_version"])
@pytest.mark.parametrize("role", ["coder", "reviewer"])
def test_missing_fields_are_not_defaulted_even_by_repair(role, field):
    data = payload()
    del data[role][field]
    result = repair_and_validate(REGISTRY[SchemaKind.AGENT_SELECTION], raw(data))
    assert not result.ok
    assert any(error.code == "required_missing" for error in result.errors)


@pytest.mark.parametrize("role", [None, "coder", "reviewer"])
def test_unknown_options_and_secret_keys_are_not_stored_or_echoed(role):
    data = payload()
    (data if role is None else data[role])["secret-token-key"] = "secret-token-value"
    result = decode_selection(raw(data))
    assert isinstance(result, SelectionRejected)
    assert "secret-token" not in repr(result)
    assert any(error.code == "unknown_field" for error in result.errors)


@pytest.mark.parametrize("field,value", [
    ("run_id", "../escape"), ("repository", "owner"),
    ("repository", "owner/repo/extra"), ("repository", "/repo"),
])
def test_invalid_targets_are_rejected(field, value):
    data = payload()
    data[field] = value
    assert decode_selection(raw(data)) == SelectionRejected("selection_target_invalid")


@pytest.mark.parametrize("data", [b"{", b"[]", b"\xff", b" " * 65537], ids=["json", "root", "utf8", "size"])
def test_invalid_encoding_and_size_fail_closed(data):
    assert isinstance(decode_selection(data), SelectionRejected)


@pytest.mark.parametrize("target", [
    {"run_id": "run-2"}, {"repository": "owner/other"}, {"number": 13},
])
def test_snapshot_cannot_be_rebound_to_another_target(target):
    assert restored(selection().to_bytes(), **target) == SelectionRejected("selection_target_mismatch")


@pytest.mark.parametrize("role", ["coder", "reviewer"])
@pytest.mark.parametrize("field,value", [
    ("provider", "changed"), ("model", "different"), ("mode", "changed"),
    ("safety_profile", "changed"), ("adapter_contract_version", 2),
])
def test_any_role_setting_change_requires_new_run(role, field, value):
    selected = selection()
    changed = replace(selected, **{role: replace(getattr(selected, role), **{field: value})})
    assert changed.digest != selected.digest
    assert restored(selected.to_bytes(), requested=changed) == SelectionRejected("selection_changed_new_run_required")


def test_old_run_has_no_inferred_provider_or_migration():
    assert restored(None) == SelectionRejected("selection_missing_new_run_required")
    assert isinstance(restored(raw({"schema_version": 1, "speaker": "Codex", "model": "test"})), SelectionRejected)
    assert REGISTRY[SchemaKind.AGENT_SELECTION].migrations is None


def test_another_process_restores_identical_bytes_without_session_memory(tmp_path):
    selected = selection("codex", "claude", "headless")
    snapshot = tmp_path / "selection.json"
    snapshot.write_bytes(selected.to_bytes())
    script = (
        "import sys; from claude_code_codex_review_loop.runtime.agent_selection import restore_selection; "
        "s=restore_selection(sys.stdin.buffer.read(),run_id='run-1',repository='owner/repo',number=12); "
        "sys.stdout.buffer.write(s.to_bytes())"
    )
    result = subprocess.run([sys.executable, "-c", script], input=snapshot.read_bytes(),
                            capture_output=True, cwd=tmp_path, check=True, timeout=30)
    assert result.stdout == selected.to_bytes()


@pytest.mark.parametrize("active", [None, "codex"])
def test_active_provider_mismatch_stops_before_probing(active):
    selected = selection()
    probes = probes_for(selected)
    outcome = preflight_selection(selected, probes, active_provider=active)
    assert outcome == SelectionRejected("active_provider_mismatch")
    assert not any(probe.calls for probe in probes)


def test_no_silent_conversion_from_active_to_headless():
    selected = selection(mode="headless")
    outcome = preflight_selection(selected, [], active_provider="claude")
    assert outcome == SelectionRejected("active_host_in_headless_mode")


def test_invalid_typed_selection_is_revalidated_before_probing():
    selected = selection()
    invalid = replace(selected, reviewer=replace(selected.reviewer, safety_profile="coder_workspace"))
    assert isinstance(preflight_selection(invalid, [], active_provider="claude"), SelectionRejected)


def test_default_empty_registry_does_not_claim_native_support():
    assert preflight_selection(selection(), [], active_provider="claude") == SelectionRejected("adapter_unavailable")


def test_duplicate_registration_is_rejected():
    selected = selection()
    probes = probes_for(selected)
    outcome = preflight_selection(selected, probes + [probes[0]], active_provider="claude")
    assert outcome == SelectionRejected("adapter_duplicate")


@pytest.mark.parametrize("field,value", [
    ("role", "coder"), ("provider", "claude"), ("mode", "active"),
    ("safety_profile", "coder_workspace"), ("contract_version", 2),
])
def test_wrong_reviewer_adapter_key_cannot_be_used_as_fallback(field, value):
    selected = selection()
    probes = probes_for(selected)
    probes[1].key = replace(probes[1].key, **{field: value})
    assert preflight_selection(selected, probes, active_provider="claude") == SelectionRejected("adapter_unavailable")
    assert probes[1].calls == []


@pytest.mark.parametrize("failure", [
    "cli_missing", "authentication_unavailable", "capability_unavailable", "version_unsupported",
])
@pytest.mark.parametrize("role", [0, 1])
def test_native_readiness_failure_is_propagated_without_another_provider(failure, role):
    selected = selection()
    probes = probes_for(selected)
    probes[role].failure = failure
    assert preflight_selection(selected, probes, active_provider="claude") == SelectionRejected(failure)
    assert probes[role].calls
    if role == 0:
        assert probes[1].calls == []
