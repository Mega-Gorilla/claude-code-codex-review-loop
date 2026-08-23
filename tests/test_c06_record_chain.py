# SPDX-License-Identifier: Apache-2.0
"""record chain検証の受入test（AC-C06-06 / 07 / 08 / 09）。chain specはADR-0008。"""

from __future__ import annotations

import json

import pytest
from c01_support.helpers import to_waiting_ci
from c06_support.helpers import HEAD, RUN, chain_bodies, chain_comments, make_comment, marker_payload

from claude_code_codex_review_loop.domain import State, transition
from claude_code_codex_review_loop.domain import events as ev
from claude_code_codex_review_loop.domain.commands import InvalidateApprovals
from claude_code_codex_review_loop.domain.values import IntegrityEvidenceRef, RecordIntegrityBlock, RecordKind
from claude_code_codex_review_loop.identity import (
    ChainCheckpoint,
    ChainPayload,
    ChainVerification,
    IdentityError,
    KnownRecord,
    ProbeFound,
    ProbeMissing,
    ProbeOutcome,
    ProducerAllowlist,
    compose_record_marker_payload,
    verify_record_chain,
)
from claude_code_codex_review_loop.identity.record_chain import _parse_chain_payload
from claude_code_codex_review_loop.schema.projection import DecodedProjection
from claude_code_codex_review_loop.transport.conversation import UnverifiedComment, body_hash_of
from claude_code_codex_review_loop.transport.marker import attach_marker

_PRODUCERS = ProducerAllowlist(logins=frozenset({"controller-bot"}))
_DETECTION_HEAD = "d" * 40


def _verify(
    comments: tuple[UnverifiedComment, ...],
    *,
    checkpoint: ChainCheckpoint | None = None,
    probes: dict[str, ProbeOutcome] | None = None,
    run_id: str = RUN,
) -> ChainVerification:
    return verify_record_chain(
        comments,
        run_id=run_id,
        detection_head=_DETECTION_HEAD,
        producers=_PRODUCERS,
        checkpoint=checkpoint,
        probes=probes if probes is not None else {},
    )


def _known(comment: UnverifiedComment, seq: int) -> KnownRecord:
    return KnownRecord(seq=seq, comment_id=comment.comment_id, body_hash=comment.body_hash)


def _marker_body(payload_json: str) -> str:
    return "x\n\n<!-- CC_REVIEW_META:v1 " + payload_json + " -->"


class TestCheckpointConstruction:
    def test_negative_high_water_is_rejected(self) -> None:
        with pytest.raises(IdentityError):
            ChainCheckpoint(high_water_mark=-1, known_records=())

    def test_duplicate_seq_is_rejected(self) -> None:
        records = (KnownRecord(1, "10", "a" * 64), KnownRecord(1, "11", "b" * 64))
        with pytest.raises(IdentityError):
            ChainCheckpoint(high_water_mark=2, known_records=records)

    def test_duplicate_comment_id_is_rejected(self) -> None:
        records = (KnownRecord(1, "10", "a" * 64), KnownRecord(2, "10", "b" * 64))
        with pytest.raises(IdentityError):
            ChainCheckpoint(high_water_mark=2, known_records=records)

    @pytest.mark.parametrize("seq", [0, 3])
    def test_seq_outside_high_water_is_rejected(self, seq: int) -> None:
        with pytest.raises(IdentityError):
            ChainCheckpoint(high_water_mark=2, known_records=(KnownRecord(seq, "10", "a" * 64),))

    def test_valid_checkpoint(self) -> None:
        checkpoint = ChainCheckpoint(high_water_mark=1, known_records=(KnownRecord(1, "10", "a" * 64),))
        assert checkpoint.high_water_mark == 1


class TestComposePayload:
    def test_round_trip_with_parser(self) -> None:
        """composeした payloadはparserで同値へ戻る（producerとverifierの共有規約）。"""
        payload = marker_payload(kind=RecordKind.FIX_RESULT, seq=2, prev="e" * 64)
        comment = make_comment(1, attach_marker("body", payload))
        parsed = _parse_chain_payload(comment)
        assert parsed == ChainPayload(
            key=str(payload["key"]),
            kind=RecordKind.FIX_RESULT,
            run=RUN,
            head=HEAD,
            seq=2,
            prev="e" * 64,
            projection=DecodedProjection(payload_hash=str(payload["pay"])),
        )

    def test_empty_key_is_rejected(self) -> None:
        with pytest.raises(IdentityError):
            compose_record_marker_payload(
                key="", kind=RecordKind.REVIEW_RESULT, run_id=RUN, head_sha=HEAD, seq=1, prev_body_hash=None
            )

    def test_projection_cannot_override_structural_keys(self) -> None:
        """意味情報の射影が識別・順序・連結のkeyを書き換えられない（ADR-0010）。"""
        with pytest.raises(IdentityError, match="構造key"):
            compose_record_marker_payload(
                key="k",
                kind=RecordKind.REVIEW_RESULT,
                run_id=RUN,
                head_sha=HEAD,
                seq=1,
                prev_body_hash=None,
                projection={"run": "other-run"},
            )

    def test_oversized_projection_is_rejected(self) -> None:
        """markerが本文の代替へ肥大化する方向を、marker attach / 投稿より前に塞ぐ。"""
        with pytest.raises(IdentityError, match="上限byte数"):
            compose_record_marker_payload(
                key="k",
                kind=RecordKind.REVIEW_RESULT,
                run_id=RUN,
                head_sha=HEAD,
                seq=1,
                prev_body_hash=None,
                projection={"sid": "x" * 2100},
            )

    def test_seq_below_one_is_rejected(self) -> None:
        with pytest.raises(IdentityError):
            compose_record_marker_payload(
                key="k", kind=RecordKind.REVIEW_RESULT, run_id=RUN, head_sha=HEAD, seq=0, prev_body_hash=None
            )

    def test_genesis_with_prev_is_rejected(self) -> None:
        with pytest.raises(IdentityError):
            compose_record_marker_payload(
                key="k", kind=RecordKind.REVIEW_RESULT, run_id=RUN, head_sha=HEAD, seq=1, prev_body_hash="e" * 64
            )

    def test_successor_without_prev_is_rejected(self) -> None:
        with pytest.raises(IdentityError):
            compose_record_marker_payload(
                key="k", kind=RecordKind.REVIEW_RESULT, run_id=RUN, head_sha=HEAD, seq=2, prev_body_hash=None
            )

    def test_malformed_prev_is_rejected(self) -> None:
        with pytest.raises(IdentityError):
            compose_record_marker_payload(
                key="k", kind=RecordKind.REVIEW_RESULT, run_id=RUN, head_sha=HEAD, seq=2, prev_body_hash="XYZ"
            )


class TestCanonicalMarkerForm:
    """条件2: 末尾1行の正規形式以外の予約token出現は「Controller以外のmarker」。"""

    def test_token_without_trailing_marker(self) -> None:
        comment = make_comment(1, "本文に CC_REVIEW_META がある")
        assert isinstance(_parse_chain_payload(comment), str)

    def test_extra_token_besides_marker(self) -> None:
        body = attach_marker(
            "本文に cc_review_meta がもう1つ",
            compose_record_marker_payload(
                key="k", kind=RecordKind.REVIEW_RESULT, run_id=RUN, head_sha=HEAD, seq=1, prev_body_hash=None
            ),
        )
        assert isinstance(_parse_chain_payload(make_comment(1, body)), str)

    @pytest.mark.parametrize(
        "payload_json",
        [
            # --- canonical encoding（sorted keysのcompact JSON）でreachする形状違反 ---
            '{"broken": }',  # JSON不正
            '{"authorization":"x","head":"h","key":"k","kind":"REVIEW_RESULT","run":"run-1","seq":1}',  # 許可外key
            '{"head":"h","kind":"REVIEW_RESULT","run":"run-1","seq":1}',  # key欠如
            '{"head":"h","key":"","kind":"REVIEW_RESULT","run":"run-1","seq":1}',  # key空
            '{"head":"h","key":"k","kind":"REVIEW_RESULT","run":"run-1","seq":true}',  # seqがbool
            '{"head":"h","key":"k","kind":"REVIEW_RESULT","run":"run-1","seq":"1"}',  # seqが文字列
            '{"head":"h","key":"k","kind":"REVIEW_RESULT","run":"run-1","seq":0}',  # seqが0
            '{"head":"h","key":"k","kind":"REVIEW_RESULT","prev":null,"run":"run-1","seq":1}',  # genesisにprev
            '{"head":"h","key":"k","kind":"REVIEW_RESULT","run":"run-1","seq":2}',  # 後続にprevなし
            '{"head":"h","key":"k","kind":"REVIEW_RESULT","prev":"XYZ","run":"run-1","seq":2}',  # prev形式不正
            '{"head":"h","key":"k","kind":"UNKNOWN_KIND","run":"run-1","seq":1}',  # kind未知
            # --- canonical encoding自体の違反（key・型が正しくても非正規形式） ---
            '{"key":"k","head":"h","kind":"REVIEW_RESULT","run":"run-1","seq":1}',  # 非sorted key順
            '{"head":"h", "key":"k","kind":"REVIEW_RESULT","run":"run-1","seq":1}',  # 空白入り
            '{"head":"h",\n"key":"k","kind":"REVIEW_RESULT","run":"run-1","seq":1}',  # 複数行JSON
            '{"head":"h","head":"h","key":"k","kind":"REVIEW_RESULT","run":"run-1","seq":1}',  # 重複key
        ],
    )
    def test_malformed_payload_variants(self, payload_json: str) -> None:
        comment = make_comment(1, _marker_body(payload_json))
        assert isinstance(_parse_chain_payload(comment), str)

    def test_oversized_payload_is_rejected(self) -> None:
        """canonical encodingでもpayload byte上限（2048）超過は非正規形式。"""
        oversized = json.dumps(
            {"head": "h", "key": "K" * 2500, "kind": "REVIEW_RESULT", "run": RUN, "seq": 1},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        comment = make_comment(1, _marker_body(oversized))
        assert isinstance(_parse_chain_payload(comment), str)

    def test_trailing_whitespace_after_marker_is_rejected(self) -> None:
        body = attach_marker(
            "record 1",
            compose_record_marker_payload(
                key="k", kind=RecordKind.REVIEW_RESULT, run_id=RUN, head_sha=HEAD, seq=1, prev_body_hash=None
            ),
        )
        assert isinstance(_parse_chain_payload(make_comment(1, body + "\n")), str)
        assert isinstance(_parse_chain_payload(make_comment(1, body + " ")), str)

    def test_marker_not_alone_on_final_line_is_rejected(self) -> None:
        canonical = json.dumps(
            {"head": HEAD, "key": "k", "kind": "REVIEW_RESULT", "run": RUN, "seq": 1},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        inline = f"本文と同じ行 <!-- CC_REVIEW_META:v1 {canonical} -->"
        assert isinstance(_parse_chain_payload(make_comment(1, inline)), str)

    def test_valid_genesis_parses(self) -> None:
        parsed = _parse_chain_payload(chain_comments(1)[0])
        assert isinstance(parsed, ChainPayload) and parsed.seq == 1 and parsed.prev is None


class TestSevenConditions:
    """AC-C06-06: 7条件を個別に検出してviolation（-> BLOCKED）にする。"""

    def _single(self, verification: ChainVerification, binding: str) -> IntegrityEvidenceRef:
        assert not verification.is_intact
        assert len(verification.violations) == 1
        violation = verification.violations[0]
        assert violation.binding.value == binding
        assert violation.head.value == _DETECTION_HEAD
        return violation

    def test_1_unauthorized_actor(self) -> None:
        comments = chain_comments(1, author="intruder")
        verification = _verify(comments)
        self._single(verification, f"iv:actor:{RUN}:c1001")
        assert verification.records == ()

    def test_1_missing_author_is_unauthorized(self) -> None:
        comments = chain_comments(1, author=None)
        self._single(_verify(comments), f"iv:actor:{RUN}:c1001")

    def test_2_embedded_marker(self) -> None:
        comment = make_comment(1001, "本文に CC_REVIEW_META を埋め込む")
        verification = _verify((comment,))
        violation = self._single(verification, f"iv:marker:{RUN}:c1001")
        descriptor = json.loads(violation.descriptor.value)
        assert descriptor["type"] == "marker" and "reason" in descriptor

    def test_3_body_tamper_against_checkpoint(self) -> None:
        comments = chain_comments(1)
        checkpoint = ChainCheckpoint(
            high_water_mark=1,
            known_records=(KnownRecord(seq=1, comment_id="1001", body_hash="f" * 64),),
        )
        verification = _verify(comments, checkpoint=checkpoint)
        violation = self._single(verification, f"iv:tamper:{RUN}:c1001")
        descriptor = json.loads(violation.descriptor.value)
        assert descriptor["expected"] == "f" * 64
        assert descriptor["observed"] == comments[0].body_hash
        assert verification.records == ()  # 改変されたrecordはVerifiedRecordにならない

    def test_4_edited_record(self) -> None:
        body = chain_bodies(1)[0]
        comment = make_comment(1001, body, updated_at="2026-08-21T12:00:00Z")
        self._single(_verify((comment,)), f"iv:edited:{RUN}:c1001")

    def test_5_middle_deletion_gap(self) -> None:
        comments = chain_comments(3)
        verification = _verify((comments[0], comments[2]))
        violation = self._single(verification, f"iv:gap:{RUN}:s00000002")
        assert json.loads(violation.descriptor.value) == {"high_water": 0, "seq": 2, "type": "gap"}

    def test_6_reorder_chain_mismatch(self) -> None:
        first = chain_comments(1)[0]
        forged_payload = marker_payload(seq=2, prev="e" * 64)
        second = make_comment(1002, attach_marker("record 2", forged_payload))
        verification = _verify((first, second))
        violation = self._single(verification, f"iv:chain:{RUN}:s00000002")
        descriptor = json.loads(violation.descriptor.value)
        assert descriptor["expected"] == first.body_hash and descriptor["observed"] == "e" * 64
        # 破れたseq 2はrecordsに入らず、seq 1は残る（差分提示用）
        assert tuple(record.seq for record in verification.records) == (1,)

    def test_7_known_record_missing(self) -> None:
        comments = chain_comments(1)
        checkpoint = ChainCheckpoint(
            high_water_mark=2,
            known_records=(_known(comments[0], 1), KnownRecord(seq=2, comment_id="1002", body_hash="b" * 64)),
        )
        probes: dict[str, ProbeOutcome] = {"1002": ProbeMissing(comment_id="1002")}
        verification = _verify(comments, checkpoint=checkpoint, probes=probes)
        bindings = tuple(violation.binding.value for violation in verification.violations)
        # 404（条件7）とseq 2のgap（条件5）は別条件・別binding
        assert f"iv:missing:{RUN}:c1002" in bindings
        assert f"iv:gap:{RUN}:s00000002" in bindings


class TestHighWaterMark:
    """AC-C06-07: N以下の欠落（後方欠落と中間gap）を検出してBLOCKED材料にする。"""

    def test_reconstructed_max_below_high_water(self) -> None:
        comments = chain_comments(2)
        checkpoint = ChainCheckpoint(
            high_water_mark=4, known_records=(_known(comments[0], 1), _known(comments[1], 2))
        )
        verification = _verify(comments, checkpoint=checkpoint)
        bindings = tuple(violation.binding.value for violation in verification.violations)
        assert bindings == (f"iv:gap:{RUN}:s00000003", f"iv:gap:{RUN}:s00000004")
        assert verification.max_seq == 2 and verification.assurance_high_water == 4

    def test_gap_within_high_water(self) -> None:
        comments = chain_comments(3)
        checkpoint = ChainCheckpoint(
            high_water_mark=3, known_records=(_known(comments[0], 1), _known(comments[2], 3))
        )
        verification = _verify((comments[0], comments[2]), checkpoint=checkpoint)
        assert tuple(v.binding.value for v in verification.violations) == (f"iv:gap:{RUN}:s00000002",)

    def test_exact_high_water_is_intact(self) -> None:
        comments = chain_comments(2)
        checkpoint = ChainCheckpoint(
            high_water_mark=2, known_records=(_known(comments[0], 1), _known(comments[1], 2))
        )
        verification = _verify(comments, checkpoint=checkpoint)
        assert verification.is_intact and verification.max_seq == 2


class TestKnownRecordProbeIntegration:
    """AC-C06-08はrecordの実在を、AC-C06-07はsequenceの連続性を扱う（分担）。"""

    def test_known_record_found_by_probe_is_intact(self) -> None:
        comments = chain_comments(2)
        checkpoint = ChainCheckpoint(
            high_water_mark=2, known_records=(_known(comments[0], 1), _known(comments[1], 2))
        )
        # 取得窓はseq 2しか覆っていないが、probeがseq 1の現存を確認した
        probes: dict[str, ProbeOutcome] = {"1001": ProbeFound(comment=comments[0])}
        verification = _verify((comments[1],), checkpoint=checkpoint, probes=probes)
        assert verification.is_intact
        assert tuple(record.seq for record in verification.records) == (1, 2)


class TestNegativeGuarantee:
    """AC-C06-09: checkpoint喪失時とNより後のtail truncationは検出しない（負の保証）。"""

    def test_fresh_resume_does_not_detect_tail_truncation(self) -> None:
        comments = chain_comments(3)
        verification = _verify(comments[:2], checkpoint=None)  # 末尾（seq 3）が削除された
        assert verification.is_intact  # 検出できない = 正常なconversationとして扱う
        assert verification.max_seq == 2
        assert verification.assurance_high_water == 0  # 保証境界なし

    def test_deletion_after_high_water_is_not_detected(self) -> None:
        comments = chain_comments(4)
        checkpoint = ChainCheckpoint(
            high_water_mark=2, known_records=(_known(comments[0], 1), _known(comments[1], 2))
        )
        verification = _verify(comments[:2], checkpoint=checkpoint)  # seq 3 / 4が削除された
        assert verification.is_intact
        assert verification.assurance_high_water == 2  # 保証はN=2まで（それ以降は残存risk）


class TestDuplicateCanonical:
    """ADR-0007 決定7の履行: timeout再投稿の良性重複は正を選択、不一致はviolation。"""

    def test_benign_duplicate_selects_earliest(self) -> None:
        body = chain_bodies(1)[0]
        earlier = make_comment(999, body, created_at="2026-08-21T10:00:00Z")
        later = make_comment(1002, body, created_at="2026-08-21T10:00:05Z")
        verification = _verify((later, earlier))
        assert verification.is_intact
        assert tuple(record.comment_id for record in verification.records) == ("999",)

    def test_created_at_tie_breaks_by_numeric_id(self) -> None:
        body = chain_bodies(1)[0]
        small = make_comment(999, body)
        large = make_comment(1002, body)
        verification = _verify((large, small))
        assert tuple(record.comment_id for record in verification.records) == ("999",)

    def test_conflicting_duplicate_is_violation(self) -> None:
        genesis = marker_payload(seq=1)
        first = make_comment(1001, attach_marker("正のrecord", genesis))
        second = make_comment(1002, attach_marker("差し替えられたrecord", genesis))
        verification = _verify((first, second))
        violation = verification.violations[0]
        assert violation.binding.value == f"iv:seqconflict:{RUN}:s00000001"
        assert json.loads(violation.descriptor.value)["comment_ids"] == ["1001", "1002"]
        assert verification.records == ()  # 正を確定できないため両方除外


class TestDedupeAndScope:
    def test_since_boundary_redelivery_uses_latest_observation(self) -> None:
        """同一comment IDの再配送はupdated_at最大の観測を採用する（編集検知が成立する）。"""
        body = chain_bodies(1)[0]
        stale = make_comment(1001, body)
        edited = make_comment(1001, body, updated_at="2026-08-21T12:00:00Z")
        verification = _verify((stale, edited))
        assert tuple(v.binding.value for v in verification.violations) == (f"iv:edited:{RUN}:c1001",)

    def test_older_redelivery_does_not_replace_latest(self) -> None:
        body = chain_bodies(1)[0]
        edited = make_comment(1001, body, updated_at="2026-08-21T12:00:00Z")
        stale = make_comment(1001, body)
        verification = _verify((edited, stale))  # 古い観測が後に来ても最新の観測を保持する
        assert tuple(v.binding.value for v in verification.violations) == (f"iv:edited:{RUN}:c1001",)

    def test_other_run_and_plain_comments_are_outside_chain(self) -> None:
        comments = chain_comments(1)
        other_run = chain_comments(1, run_id="run-2", start_id=3001)
        plain = make_comment(4001, "markerのない通常comment", author="anyone")
        verification = _verify(comments + other_run + (plain,))
        assert verification.is_intact
        assert tuple(record.seq for record in verification.records) == (1,)

    def test_empty_input_without_checkpoint(self) -> None:
        verification = _verify(())
        assert verification.is_intact and verification.max_seq == 0 and verification.records == ()


class TestIdempotentBindings:
    def test_same_input_yields_identical_violations(self) -> None:
        comments = chain_comments(3, author="intruder")
        first = _verify((comments[0], comments[2]))
        second = _verify((comments[2], comments[0]))  # 入力順にも依存しない
        assert first.violations == second.violations
        bindings = [violation.binding.value for violation in first.violations]
        assert bindings == sorted(bindings)  # canonical order（binding昇順）


class TestVerifiedRecords:
    def test_happy_path_record_fields(self) -> None:
        comments = chain_comments(2)
        verification = _verify(comments)
        assert verification.is_intact
        record = verification.records[1]
        expected_key = marker_payload(seq=2, prev=body_hash_of(comments[0].body), body="record 2")["key"]
        assert (record.seq, record.kind, record.key) == (2, RecordKind.REVIEW_RESULT, expected_key)
        assert record.projection.result == "CHANGES_REQUESTED" and record.projection.round == 1
        assert record.comment_id == "1002" and record.author_login == "controller-bot"
        assert record.head_sha == HEAD and record.body_hash == body_hash_of(comments[1].body)
        assert record.url == comments[1].url and record.created_at == comments[1].created_at


class TestBlockedTransition:
    """AC-C06-06統合: C-06のviolationがC-01遷移でBLOCKED（RecordIntegrityBlock）へ到達する。"""

    def _scenarios(self) -> dict[str, ChainVerification]:
        comments = chain_comments(3)
        tampered_checkpoint = ChainCheckpoint(
            high_water_mark=1, known_records=(KnownRecord(1, "1001", "f" * 64),)
        )
        missing_checkpoint = ChainCheckpoint(
            high_water_mark=1, known_records=(KnownRecord(1, "9999", "f" * 64),)
        )
        forged_second = make_comment(
            1002,
            attach_marker(
                "record 2",
                compose_record_marker_payload(
                    key="turn-2", kind=RecordKind.REVIEW_RESULT, run_id=RUN, head_sha=HEAD, seq=2,
                    prev_body_hash="e" * 64,
                ),
            ),
        )
        return {
            "actor": _verify(chain_comments(1, author="intruder")),
            "marker": _verify((make_comment(1001, "埋め込み CC_REVIEW_META"),)),
            "tamper": _verify(comments[:1], checkpoint=tampered_checkpoint),
            "edited": _verify((make_comment(1001, chain_bodies(1)[0], updated_at="2026-08-21T12:00:00Z"),)),
            "gap": _verify((comments[0], comments[2])),
            "chain": _verify((comments[0], forged_second)),
            "missing": _verify((), checkpoint=missing_checkpoint, probes={"9999": ProbeMissing("9999")}),
        }

    def test_each_condition_reaches_blocked(self) -> None:
        for label, verification in self._scenarios().items():
            assert not verification.is_intact, label
            machine = to_waiting_ci()
            blocked, commands = transition(
                machine, ev.RecordIntegrityViolationDetected(verification.violations[0])
            )
            assert blocked.state is State.BLOCKED, label
            assert isinstance(blocked.block, RecordIntegrityBlock), label
            assert InvalidateApprovals() in commands, label


class TestMisuse:
    def test_uncovered_known_id_is_caller_error(self) -> None:
        """probe忘れ（取得窓にもprobe結果にも無い既知ID）はviolationではなくIdentityError。"""
        checkpoint = ChainCheckpoint(high_water_mark=1, known_records=(KnownRecord(1, "9999", "f" * 64),))
        with pytest.raises(IdentityError) as excinfo:
            _verify((), checkpoint=checkpoint, probes={})
        assert excinfo.value.stage == "probe"
