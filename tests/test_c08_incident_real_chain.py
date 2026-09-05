# SPDX-License-Identifier: Apache-2.0
"""実際のcomment改変・削除を通したincidentの完走・別process resume（Phase 8 PR-3d）。

fake ghの本文と更新日時を変更し、製品のChainRecordsが違反と検証済みrecord列を
導出する。violationsだけの差し替えでは、末尾recordの除外による採番衝突を見逃す。
削除時はcheckpointへ既知recordとhigh-water markを保存し、incidentが欠番を埋めて
liveのgapを消す経路も検査する。重複投稿は未観測だが、再開の回帰条件として検査する。
未実装のpayload・本文・event portだけを既存のfakeで補い、chain検証は差し替えない。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from c06_support.helpers import make_comment
from c07_support.helpers import RUN, chain_comments_of, conversation_section
from c08_support.runtime import RuntimeEnv, runtime_env
from test_c08_incident import (
    FakeIncidentPayloads,
    _drive,
    _incident_kwargs,
    _payload,
    _ports,
    _RecordedQueue,
    _recording_state,
    violation,
)

from claude_code_codex_review_loop.domain.values import RecordKind, State
from claude_code_codex_review_loop.identity.record_chain import ChainPayload, parse_record_marker
from claude_code_codex_review_loop.runtime import SessionConfig, read_session_config
from claude_code_codex_review_loop.state import checkpoint_path, prepare_state_root, run_directory, save_checkpoint
from claude_code_codex_review_loop.workflow import (
    IncidentRecorded,
    Terminal,
    read_machine_state,
    read_recorded_violations,
    record_incident,
    with_machine_state,
)

CASES = [(1, 0), (2, 1), (3, 1)]
CASE_IDS = ["only-record", "tail", "middle-control"]


def _damaged_env(tmp_path: Path, count: int, damaged_index: int, deleted: bool) -> RuntimeEnv:
    kinds = (RecordKind.REVIEW_RESULT,) * count
    env = runtime_env(
        tmp_path,
        state=_recording_state(deferred=(violation(),)),
        seeded=kinds,
        extra={"conversation": conversation_section(chain_comments_of(list(kinds)))} if deleted else None,
    )
    comments = env.comments()
    comment = comments[damaged_index]
    assert isinstance(comment, dict)
    if deleted:
        del comments[damaged_index]
    else:
        comment["body"] = "改変された本文\n" + str(comment["body"])
        comment["updated_at"] = "2026-09-05T00:00:00Z"
    env.seed(comments)
    chain = env.ports().records.chain(RUN)
    if deleted:
        assert len(chain.violations) == 2
        assert chain.violations[0].binding.value.startswith("iv:gap:")
        assert chain.violations[1].binding.value.startswith("iv:missing:")
        assert chain.assurance_high_water == count
        if damaged_index == count - 1:
            assert chain.max_seq == count - 1 < chain.assurance_high_water
    else:
        assert len(chain.violations) == 1
        assert chain.violations[0].binding.value.startswith("iv:edited:")
    assert [record.seq for record in chain.records] == [
        seq for seq in range(1, count + 1) if seq != damaged_index + 1
    ]
    # C-01が受理済みの違反集合を保存する。値は製品のchain検証から得たものだけ。
    save_checkpoint(
        checkpoint_path(env.paths, RUN),
        with_machine_state(_payload(env), _recording_state(deferred=chain.violations)),
    )
    return env


def _current_ports(env: RuntimeEnv):
    state = read_machine_state(_payload(env))
    return _ports(env, recorded=tuple(ref.binding for ref in state.deferred_integrity))


def _assert_completed(env: RuntimeEnv, count: int, original_bindings: tuple[str, ...], deleted: bool) -> None:
    payload = _payload(env)
    assert read_machine_state(payload).state is State.CANCELLED
    assert read_machine_state(payload).deferred_integrity == ()
    assert read_recorded_violations(payload) == original_bindings
    assert "transaction" not in payload
    chain = env.ports().records.chain(RUN)
    # 台帳へ残るだけでは足りない。投稿で欠番を埋め、liveのgapを消してはならない。
    assert tuple(ref.binding.value for ref in chain.violations) == original_bindings
    incidents = [record for record in chain.records if record.kind is RecordKind.INTEGRITY_INCIDENT]
    assert len(incidents) == 1
    assert incidents[0].seq > count  # 改変・削除された末尾の番号も再利用しない。
    assert len(env.comments()) == count + 1 - int(deleted)


@pytest.mark.parametrize(("count", "damaged_index"), CASES, ids=CASE_IDS)
@pytest.mark.parametrize("deleted", [False, True], ids=["edited", "deleted-known"])
def test_a_damaged_chain_records_its_incident_without_reusing_sequence(
    tmp_path: Path, count: int, damaged_index: int, deleted: bool
) -> None:
    env = _damaged_env(tmp_path, count, damaged_index, deleted)
    bindings = tuple(ref.binding.value for ref in env.ports().records.chain(RUN).violations)
    result = _drive(env, _current_ports(env))
    assert result.outcome == Terminal(state=State.CANCELLED), result
    _assert_completed(env, count, bindings, deleted)
    again = _drive(env, _current_ports(env))
    assert again.outcome == Terminal(state=State.CANCELLED)
    assert again.trace.persisted == ()
    _assert_completed(env, count, bindings, deleted)


def _resume_in_child(root: str, directory: str) -> None:
    paths = prepare_state_root(Path(root))
    config = read_session_config(paths, RUN)
    assert isinstance(config, SessionConfig), config
    env = RuntimeEnv(paths, run_directory(paths, RUN), Path(directory), config)
    result = _drive(env, _current_ports(env))
    print(json.dumps({"terminal": result.outcome == Terminal(state=State.CANCELLED),
                      "outcome": repr(result.outcome), "persisted": result.trace.persisted}))


def _child(env: RuntimeEnv, cwd: Path) -> dict[str, object]:
    cwd.mkdir()
    # childはfixtureを作り直さず、disk上のsession/checkpointとfake ghだけを読み直す。
    test_dir = str(Path(__file__).resolve().parent)
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = os.pathsep.join(filter(None, [test_dir, child_env.get("PYTHONPATH")]))
    completed = subprocess.run(
        [sys.executable, "-c",
         "import sys; from test_c08_incident_real_chain import _resume_in_child; "
         "_resume_in_child(*sys.argv[1:])", str(env.paths.root), str(env.directory)],
        cwd=cwd, env=child_env, capture_output=True, text=True, encoding="utf-8", check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.splitlines()[-1])


@pytest.mark.parametrize(("count", "damaged_index"), CASES, ids=CASE_IDS)
@pytest.mark.parametrize("deleted", [False, True], ids=["edited", "deleted-known"])
@pytest.mark.parametrize("after_post", [False, True], ids=["before-post", "after-post-before-checkpoint"])
def test_incident_resume_preserves_sequence_and_records_once(
    tmp_path: Path, count: int, damaged_index: int, deleted: bool, after_post: bool
) -> None:
    env = _damaged_env(tmp_path, count, damaged_index, deleted)
    bindings = tuple(ref.binding.value for ref in env.ports().records.chain(RUN).violations)
    produced = record_incident(**_incident_kwargs(env, FakeIncidentPayloads()))
    assert isinstance(produced, IncidentRecorded), produced
    pending = _payload(env)
    if after_post:
        # 投稿後・checkpoint消費前のcrashを再現する。実投稿経路でfake ghへ書き、
        # durable stateだけを投稿前へ戻す。childは投稿済みrecordを再利用する必要がある。
        _drive(env, _current_ports(env))
        assert len(env.comments()) == count + 1 - int(deleted)
        save_checkpoint(checkpoint_path(env.paths, RUN), pending)
    result = _child(env, tmp_path / "resume")
    assert result["terminal"] is True, result
    _assert_completed(env, count, bindings, deleted)
    again = _child(env, tmp_path / "resume-again")
    assert again["terminal"] is True, again
    assert again["persisted"] == []
    _assert_completed(env, count, bindings, deleted)


def test_serial_incidents_link_to_the_previous_verified_incident(tmp_path: Path) -> None:
    env = _damaged_env(tmp_path, count=2, damaged_index=1, deleted=True)
    before = env.ports().records.chain(RUN)
    first, remaining = before.violations
    # gapを受理して作成したrecordの永続化前に、同じ実chainからmissingも検出する。
    # 違反は捏造せず、C-01の既知集合だけを先行する検出時点へ設定する。
    save_checkpoint(
        checkpoint_path(env.paths, RUN),
        with_machine_state(_payload(env), _recording_state(deferred=(first,))),
    )
    payloads = FakeIncidentPayloads()
    produced = record_incident(**_incident_kwargs(env, payloads))
    assert isinstance(produced, IncidentRecorded), produced
    events = _RecordedQueue((first.binding,), (remaining.binding,))
    ports = replace(_ports(env, incident=payloads), events=events)
    result = _drive(env, ports)
    assert result.outcome == Terminal(state=State.CANCELLED), result
    assert result.trace.incidents == 1  # 1件目は上で作成済み。I-VRで残余の1件を追加する。
    assert len(set(result.trace.persisted)) == events.calls == 2
    assert [call.violation_bindings for call in payloads.calls] == [
        (first.binding,), (remaining.binding,),
    ]
    after = env.ports().records.chain(RUN)
    assert after.violations == before.violations
    assert [(record.seq, record.kind) for record in after.records] == [
        (1, RecordKind.REVIEW_RESULT),
        (3, RecordKind.INTEGRITY_INCIDENT),
        (4, RecordKind.INTEGRITY_INCIDENT),
    ]
    for previous, incident in zip(after.records, after.records[1:], strict=False):
        marker = parse_record_marker(make_comment(int(incident.comment_id), incident.body))
        assert isinstance(marker, ChainPayload)
        assert marker.audit_prev == previous.seq
        assert marker.prev == previous.body_hash
    payload = _payload(env)
    assert read_recorded_violations(payload) == tuple(ref.binding.value for ref in before.violations)
    assert read_machine_state(payload).deferred_integrity == ()
    assert "transaction" not in payload
    assert len(env.comments()) == 3
    again = _drive(env, ports)
    assert again.outcome == Terminal(state=State.CANCELLED)
    assert again.trace.persisted == ()
    assert len(env.comments()) == 3
