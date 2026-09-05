# SPDX-License-Identifier: Apache-2.0
"""PR-B2: 実file・fake GitHub・別processでrole設定の保存と再開を検証する。"""

import json
import subprocess
import sys
from dataclasses import replace

import pytest
from c05_support.helpers import seed_state
from c06_support.helpers import seed_dict
from c07_support.helpers import NUMBER, RUN, chain_comments_of, checkpoint_payload, state_paths
from c08_support.helpers import user_machine_state
from c08_support.runtime import (
    ISSUED_AT,
    RuntimeEnv,
    fixed_clock,
    gate_host,
    round_ports,
    session_payload,
    user_submit_for,
)
from test_role_provider_contract import probes_for, selection

from claude_code_codex_review_loop.domain.values import Awaiting, RecordKind, State
from claude_code_codex_review_loop.identity.fs_permissions import replace_private_text, verify_private_file
from claude_code_codex_review_loop.runtime import agent_session as module
from claude_code_codex_review_loop.runtime import drive, step, submit_result
from claude_code_codex_review_loop.runtime.agent_session import (
    AgentExecution,
    check_agent_binding,
    initialize_agent_session,
)
from claude_code_codex_review_loop.runtime.config import (
    ConfigUnavailable,
    SessionConfig,
    config_path,
    read_session_config,
    write_session_config,
)
from claude_code_codex_review_loop.state import checkpoint_path, load_checkpoint, run_directory, save_checkpoint
from claude_code_codex_review_loop.workflow import EngineStopped, Terminal, with_machine_state


def prepared(tmp_path, selected=None):
    selected = selected or selection()
    directory = tmp_path / "gh"
    directory.mkdir()
    seed_state(directory, comments=[
        seed_dict(comment, issue=NUMBER) for comment in chain_comments_of([RecordKind.FINAL_REPORT])
    ])
    paths = state_paths(tmp_path)
    initial = with_machine_state(checkpoint_payload(), user_machine_state(Awaiting.USER_INPUT_GATE))
    return paths, directory, dict(selection=selected, session=session_payload(directory), checkpoint=initial)


def environment(tmp_path, selected=None):
    paths, directory, args = prepared(tmp_path, selected)
    assert initialize_agent_session(paths, **args) is None
    config = read_session_config(paths, RUN)
    assert isinstance(config, SessionConfig)
    return RuntimeEnv(paths, run_directory(paths, RUN), directory, config)


def invoke(env, *args):
    result = subprocess.run([
        sys.executable, "-m", "claude_code_codex_review_loop.runtime", *args,
        "--state-root", str(env.paths.root), "--run", RUN,
    ], capture_output=True, text=True, check=False, timeout=45)
    assert result.stdout, result.stderr
    return result.returncode, json.loads(result.stdout)


def advance(env, **kwargs):
    clock = fixed_clock()
    return step(paths=env.paths, config=env.config, ports=round_ports(env),
                id_source=clock.id_source, issued_at=ISSUED_AT, **kwargs)


@pytest.mark.parametrize("coder", ["claude", "codex"])
@pytest.mark.parametrize("reviewer", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["active", "headless"])
def test_bound_round_preserves_selection_and_legacy_identifiers(tmp_path, coder, reviewer, mode):
    selected = selection(coder, reviewer, mode)
    env = environment(tmp_path, selected)
    execution = AgentExecution(coder if mode == "active" else None, tuple(probes_for(selected)))
    host = gate_host(env)
    result = drive(host, paths=env.paths, config=env.config, ports=round_ports(env),
                   clock=fixed_clock(), max_rounds=5, execution=execution)
    assert isinstance(result.outcome, Terminal) and result.outcome.state == State.CANCELLED
    assert result.rounds == 3
    assert len(env.comments()) == 4
    checkpoint = load_checkpoint(checkpoint_path(env.paths, RUN)).payload
    assert checkpoint["agent_selection"] == {"digest": selected.digest}
    assert read_session_config(env.paths, RUN).agent_selection == selected
    assert check_agent_binding(env.paths, env.config) is None
    verify_private_file(config_path(env.paths, RUN))
    verify_private_file(checkpoint_path(env.paths, RUN))


@pytest.mark.parametrize("coder,reviewer", [("claude", "codex"), ("codex", "claude"),
                                            ("claude", "claude"), ("codex", "codex")])
def test_process_resume_and_submit_replay_keep_binding_and_nonce(tmp_path, coder, reviewer):
    env = environment(tmp_path, selection(coder, reviewer))
    code, first = invoke(env, "advance", "--active-provider", coder)
    assert code == 0 and first["outcome"] == "AWAIT_USER"
    code, resumed = invoke(env, "advance", "--active-provider", coder)
    assert code == 0 and resumed["reissued"] is True
    assert first["request_id"] == resumed["request_id"]
    assert first["envelope_path"] == resumed["envelope_path"]
    submit = user_submit_for(first["envelope_path"], first["result_path"])
    submit_path = env.run_dir / "submit.json"
    replace_private_text(submit_path, json.dumps(submit))
    assert invoke(env, "submit", "--result", str(submit_path))[1]["outcome"] == "ACCEPTED"
    assert invoke(env, "submit", "--result", str(submit_path))[1]["outcome"] == "REPLAYED"
    checkpoint = checkpoint_path(env.paths, RUN)
    before_post = checkpoint.read_text(encoding="utf-8")
    assert invoke(env, "advance")[1]["state"] == "CANCELLED"
    # 投稿後・checkpoint消費前のcrash window。別processの再試行で重複を作らない。
    replace_private_text(checkpoint, before_post)
    assert invoke(env, "advance")[1]["state"] == "CANCELLED"
    assert len(env.comments()) == 2
    assert check_agent_binding(env.paths, env.config) is None


@pytest.mark.parametrize("position", [1, 2, 3])
def test_initialization_interruption_never_exposes_partial_run(tmp_path, monkeypatch, position):
    paths, _, args = prepared(tmp_path)
    real = module.replace_private_text
    calls = []

    def interrupt(path, text):
        calls.append(path)
        if len(calls) == position:
            raise OSError("secret-disk-error")
        real(path, text)

    with monkeypatch.context() as patch:
        patch.setattr(module, "replace_private_text", interrupt)
        result = initialize_agent_session(paths, **args)
    assert result.code == "agent_initialization_unavailable"
    assert "secret" not in repr(result)
    assert isinstance(read_session_config(paths, RUN), ConfigUnavailable)
    assert initialize_agent_session(paths, **args) is None
    assert isinstance(read_session_config(paths, RUN), SessionConfig)
    assert initialize_agent_session(paths, **args).code == "agent_initialization_conflict_new_run_required"


@pytest.mark.parametrize("which", ["session", "checkpoint"])
def test_existing_legacy_run_is_not_rebound(tmp_path, which):
    paths, _, args = prepared(tmp_path)
    if which == "session":
        write_session_config(paths, RUN, args[which])
    else:
        save_checkpoint(checkpoint_path(paths, RUN), args[which])
    assert initialize_agent_session(paths, **args).code == "agent_initialization_conflict_new_run_required"


@pytest.mark.parametrize("role", ["coder", "reviewer"])
@pytest.mark.parametrize("field,value", [("provider", "codex"), ("model", "other"),
                                         ("adapter_contract_version", 2)])
def test_disk_selection_change_blocks_cached_and_reloaded_callers(tmp_path, role, field, value):
    env = environment(tmp_path, selection("claude", "claude"))
    path = config_path(env.paths, RUN)
    payload = json.loads(path.read_bytes())
    payload["agent_selection"][role][field] = value
    replace_private_text(path, json.dumps(payload))
    assert check_agent_binding(env.paths, env.config).code == "selection_changed_new_run_required"
    changed = read_session_config(env.paths, RUN)
    assert check_agent_binding(env.paths, changed).code == "selection_binding_mismatch"
    before = checkpoint_path(env.paths, RUN).read_bytes()
    assert advance(env).outcome.code == "selection_changed_new_run_required"
    assert submit_result(b"{}", paths=env.paths, config=env.config, ports=round_ports(env),
                         accepted_at=ISSUED_AT).code == "selection_changed_new_run_required"
    assert checkpoint_path(env.paths, RUN).read_bytes() == before
    assert len(env.comments()) == 1


@pytest.mark.parametrize("mutation", ["checkpoint_missing", "binding_missing", "binding_changed", "session_missing",
                                     "selection_missing", "initializing", "legacy_caller"])
def test_bound_run_cannot_silently_fall_back_to_legacy(tmp_path, mutation):
    env = environment(tmp_path)
    cp = checkpoint_path(env.paths, RUN)
    cfg = config_path(env.paths, RUN)
    if mutation == "checkpoint_missing":
        cp.unlink()
    elif mutation in ("binding_missing", "binding_changed"):
        payload = load_checkpoint(cp).payload
        if mutation == "binding_missing":
            del payload["agent_selection"]
        else:
            payload["agent_selection"] = {"digest": "b" * 64}
        save_checkpoint(cp, payload)
    elif mutation == "session_missing":
        cfg.unlink()
    elif mutation == "legacy_caller":
        env = replace(env, config=replace(env.config, agent_selection=None))
    else:
        payload = json.loads(cfg.read_bytes())
        if mutation == "selection_missing":
            del payload["agent_selection"]
        else:
            payload["agent_initialization"] = "preparing"
        replace_private_text(cfg, json.dumps(payload))
    assert isinstance(check_agent_binding(env.paths, env.config), EngineStopped)
    assert len(env.comments()) == 1


@pytest.mark.parametrize("active,available,code", [("codex", True, "active_provider_mismatch"),
                                                  ("claude", False, "adapter_unavailable"),
                                                  ("claude", True, None)])
def test_probe_gate_prevents_host_execution_but_allows_human_cancel(tmp_path, active, available, code):
    env = environment(tmp_path)
    host = gate_host(env)
    execution = AgentExecution(active, tuple(probes_for(env.config.agent_selection)) if available else ())
    result = drive(host, paths=env.paths, config=env.config, ports=round_ports(env),
                   clock=fixed_clock(), max_rounds=5, execution=execution)
    if code:
        assert result.outcome.code == code
        assert host.executed == ["user:GATE_QUESTION"]
    else:
        assert isinstance(result.outcome, Terminal)


def test_module_entry_point_rejects_missing_native_adapter(tmp_path):
    env = environment(tmp_path)
    host = gate_host(env)
    first = advance(env)
    raw = host.execute(first.outcome)
    submit_result(raw, paths=env.paths, config=env.config, ports=round_ports(env), accepted_at=ISSUED_AT)
    # 本物のmodule entry pointはC-10 payload未実装で止まる。adapterを捏造しない。
    code, outcome = invoke(env, "advance", "--active-provider", "claude")
    assert code == 3 and outcome["code"] == "port_unavailable"
