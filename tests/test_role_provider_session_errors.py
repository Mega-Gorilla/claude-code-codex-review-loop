# SPDX-License-Identifier: Apache-2.0
"""role設定の初期化・guardのnegative / crash test。実LLMは起動しない。"""

import json
import subprocess
import sys
from dataclasses import replace

import pytest
from c07_support.helpers import RUN
from c08_support.runtime import ISSUED_AT, fixed_clock, gate_host, round_ports
from test_role_provider_session import advance, environment, prepared

from claude_code_codex_review_loop.runtime import agent_session as module
from claude_code_codex_review_loop.runtime import drive
from claude_code_codex_review_loop.runtime.agent_session import check_agent_binding, initialize_agent_session
from claude_code_codex_review_loop.runtime.config import ConfigUnavailable, read_session_config
from claude_code_codex_review_loop.state import checkpoint_path, load_checkpoint, save_checkpoint
from claude_code_codex_review_loop.workflow import (
    EmergencyStopFailed,
    EngineStopped,
    request_emergency_stop,
)


@pytest.mark.parametrize("which", ["selection", "session_target", "checkpoint_target", "session", "checkpoint"])
def test_initializer_rejects_invalid_input_before_writing(tmp_path, which):
    paths, _, args = prepared(tmp_path)
    if which == "selection":
        selected = args["selection"]
        args["selection"] = replace(selected, coder=replace(selected.coder, model=""))
    elif which.endswith("_target"):
        args[which.removesuffix("_target")]["number"] = 99
    else:
        args[which]["schema_version"] = 999
    assert initialize_agent_session(paths, **args) is not None
    assert not checkpoint_path(paths, RUN).exists()


@pytest.mark.parametrize("position", [1, 2, 3])
def test_hard_process_exit_leaves_no_runnable_partial_initialization(tmp_path, position):
    paths, _, args = prepared(tmp_path)
    raw = json.dumps({**args, "selection": json.loads(args["selection"].to_bytes())})
    script = """
import json, os, sys
from pathlib import Path
from claude_code_codex_review_loop.state import prepare_state_root
from claude_code_codex_review_loop.runtime import agent_session as m
from claude_code_codex_review_loop.runtime.agent_selection import decode_selection
args = json.loads(sys.stdin.read())
args['selection'] = decode_selection(json.dumps(args['selection']).encode())
real, counter = m.replace_private_text, 0
def write(path, text):
    global counter
    real(path, text)
    counter += 1
    if counter == int(sys.argv[2]):
        os._exit(71)
m.replace_private_text = write
m.initialize_agent_session(prepare_state_root(Path(sys.argv[1])), **args)
"""
    result = subprocess.run([sys.executable, "-c", script, str(paths.root), str(position)],
                            input=raw, text=True, capture_output=True, timeout=30, check=False)
    assert result.returncode == 71, result.stderr
    assert isinstance(read_session_config(paths, RUN), ConfigUnavailable) == (position != 3)
    assert initialize_agent_session(paths, **args).code == "agent_initialization_unavailable"
    # 子processの終了を確認済み。このtest自身のstale guardだけを解除する。
    cp = checkpoint_path(paths, RUN)
    cp.with_name(cp.name + ".guard").rmdir()
    retry = initialize_agent_session(paths, **args)
    assert (retry is None) == (position != 3)


@pytest.mark.parametrize("changed", ["initial_state", "checkpoint_corrupt"])
def test_preparing_retry_cannot_change_the_initial_state(tmp_path, monkeypatch, changed):
    paths, _, args = prepared(tmp_path)
    real = module.replace_private_text
    count = 0

    def write(path, text):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError()
        real(path, text)

    with monkeypatch.context() as patch:
        patch.setattr(module, "replace_private_text", write)
        initialize_agent_session(paths, **args)
    if changed == "initial_state":
        args["checkpoint"]["schema_version"] = 1
    else:
        real(checkpoint_path(paths, RUN), "{}")
    assert initialize_agent_session(paths, **args).code == "agent_initialization_conflict_new_run_required"


def test_read_error_is_structured_without_exception_text(tmp_path, monkeypatch):
    env = environment(tmp_path)

    def fail(*args):
        raise OSError("secret")

    monkeypatch.setattr(module, "read_session_config", fail)
    result = check_agent_binding(env.paths, env.config)
    assert result.code == "agent_binding_unavailable" and "secret" not in repr(result)


@pytest.mark.parametrize("kind", ["invalid", "target", "checkpoint_target"])
def test_typed_and_checkpoint_target_guards(tmp_path, monkeypatch, kind):
    env = environment(tmp_path)
    selected = env.config.agent_selection
    if kind == "invalid":
        selected = replace(selected, coder=replace(selected.coder, model=""))
    elif kind == "target":
        selected = replace(selected, number=99)
    else:
        cp = checkpoint_path(env.paths, RUN)
        payload = load_checkpoint(cp).payload
        payload["number"] = 99
        save_checkpoint(cp, payload)
    config = replace(env.config, agent_selection=selected)
    monkeypatch.setattr(module, "read_session_config", lambda *args: config)
    result = check_agent_binding(env.paths, config)
    assert result.code == ("selection_invalid" if kind == "invalid" else "selection_target_mismatch")


@pytest.mark.parametrize("current", ["selected", "preparing"])
def test_cached_legacy_caller_cannot_ignore_new_session(tmp_path, monkeypatch, current):
    env = environment(tmp_path)
    cp = checkpoint_path(env.paths, RUN)
    payload = load_checkpoint(cp).payload
    del payload["agent_selection"]
    save_checkpoint(cp, payload)
    if current == "preparing":
        monkeypatch.setattr(module, "read_session_config", lambda *args: ConfigUnavailable("initializing"))
    result = check_agent_binding(env.paths, replace(env.config, agent_selection=None))
    assert isinstance(result, EngineStopped)


@pytest.mark.parametrize("failure", [False, True])
def test_broken_binding_does_not_prevent_durable_emergency_stop(tmp_path, monkeypatch, failure):
    from claude_code_codex_review_loop.runtime import session

    env = environment(tmp_path)
    request_emergency_stop(paths=env.paths, run_id=RUN, repository=env.config.repository,
                           number=env.config.number, requested_at=ISSUED_AT)
    cp = checkpoint_path(env.paths, RUN)
    payload = load_checkpoint(cp).payload
    payload["agent_selection"] = {"digest": "a" * 64}
    save_checkpoint(cp, payload)
    if failure:
        monkeypatch.setattr(session, "_emergency", lambda *args: EmergencyStopFailed("failed"))
    result = advance(env)
    assert result.outcome.code == ("emergency_stop_failed" if failure else "selection_binding_mismatch")
    assert result.trace.stopped == (0 if failure else 1)


def test_bad_checkpoint_stays_fail_closed_on_safety_path(tmp_path):
    env = environment(tmp_path)
    checkpoint_path(env.paths, RUN).unlink()
    assert advance(env).outcome.code == "checkpoint_unavailable"


def test_drive_rechecks_binding_before_executing_host(tmp_path, monkeypatch):
    from claude_code_codex_review_loop.runtime import host as host_module

    env = environment(tmp_path)
    host = gate_host(env)
    monkeypatch.setattr(host_module, "check_agent_binding", lambda *args: EngineStopped("changed", ""))
    result = drive(host, paths=env.paths, config=env.config, ports=round_ports(env),
                   clock=fixed_clock(), max_rounds=2)
    assert result.outcome.code == "changed" and host.executed == []


def test_partial_ready_config_requires_initial_checkpoint_hash(tmp_path):
    env = environment(tmp_path)
    path = module.config_path(env.paths, RUN)
    payload = json.loads(path.read_bytes())
    del payload["agent_initial_checkpoint"]
    module.replace_private_text(path, json.dumps(payload))
    assert read_session_config(env.paths, RUN) == ConfigUnavailable("agent_initialization_invalid")


def test_legacy_in_memory_caller_without_session_file_keeps_old_contract(tmp_path):
    env = environment(tmp_path)
    cp = checkpoint_path(env.paths, RUN)
    payload = load_checkpoint(cp).payload
    del payload["agent_selection"]
    save_checkpoint(cp, payload)
    module.config_path(env.paths, RUN).unlink()
    config = replace(env.config, agent_selection=None)
    assert check_agent_binding(env.paths, config) is None
    assert module.check_agent_execution(config, module.AgentExecution()) is None
