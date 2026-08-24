# SPDX-License-Identifier: Apache-2.0
"""head照合と承認の失効判定（AC-C07-03。C-07 / ADR-0012）。

不変条件「review承認とmerge承認は特定のhead SHAへ結び付き、headが変われば失効する」を
resume時に成立させる。判定は**純粋**で、入力はGitHub由来の値に限る。

- 承認の有効性は「承認recordのmarkerが持つhead」と「PRが現在advertiseしているhead」の
  一致だけで決める。checkpointは**変化の分類**（coder pushか外部更新か）にしか使わない。
  分類を誤っても、失効した承認が甦らない構造にする
- canonical recordはappend-onlyなので、旧headの承認は履歴に残り続ける。同種の承認が
  現headにも存在する場合、旧世代は**superseded**（診断用に保持するが判定へ影響しない）と
  し、現headの承認が無い場合だけ**voided**として失効させる。そうしないと、再承認済みの
  runが以後永久にfallbackし続ける
- merge gateへの復帰（`SAME_HEAD_VALIDATED`）は、**GitHub上で確認できた現headの承認**を
  必須にする。local checkpointの`approved_sha`だけで復帰させない（GitHub canonical）
- coder更新と外部更新の最終的な区別はAC-C10-06（Phase 10）の責務で、ここは観測された
  事実（分類と根拠）を返すに留める
- `ResumeVerdict`はresume preflightの**判定**であり、event名ではない。C-01のどのevent
  （`ResumeValidated` / `ResumeFallbackRequired` / `ResumeSameHeadValidated` /
  `HeadChangedExternally`）へ写すかは現在のstateにも依存するため、**構築はC-10**が行う
  （例: `FALLBACK_REQUIRED`はFAILED / BLOCKEDでは`ResumeFallbackRequired`、MERGE_FAILEDでは
  M-HCの`HeadChangedExternally`）。record -> eventの対応表を持ち込まない
- 観測が成立しない場合（head SHAの形式不正等）は判定せず`HeadUnobservable`で停止する
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final

from ..domain.states import State
from ..domain.values import RecordKind
from ..identity.record_chain import VerifiedRecord
from ..transport.pull_request import UnverifiedPullRequest

_SHA_PATTERN: Final = re.compile(r"[0-9a-f]{40}\Z")
_APPROVED_VERDICT: Final = "APPROVED"
# merge gateへ復帰する（`SAME_HEAD_VALIDATED`）ために現headでの確認が要る承認種別
_MERGE_GATE_APPROVALS: Final = frozenset({RecordKind.MERGE_APPROVAL, RecordKind.REVIEW_RESULT})


@dataclass(frozen=True)
class HeadObservation:
    """resume時点のPR advertised head（検証済みの形式であることだけを保証する）。"""

    head_sha: str
    base_sha: str
    pull_state: str
    merged: bool


@dataclass(frozen=True)
class HeadUnobservable:
    """headを観測できない（推測して判定しない）。"""

    detail: str


def observe_head(pull: UnverifiedPullRequest) -> HeadObservation | HeadUnobservable:
    """C-05の未検証metadataから観測値を作る（形式が満たせなければ停止）。"""
    for label, value in (("head", pull.head_sha), ("base", pull.base_sha)):
        if _SHA_PATTERN.fullmatch(value) is None:
            return HeadUnobservable(detail=f"PRの{label} SHAが40桁小文字hexでない")
    return HeadObservation(
        head_sha=pull.head_sha, base_sha=pull.base_sha, pull_state=pull.state, merged=pull.merged
    )


@dataclass(frozen=True)
class CheckpointHeads:
    """checkpointが記録するhead群（cache。判断根拠ではなく分類にだけ使う）。"""

    observed_sha: str | None = None
    approved_sha: str | None = None
    base_sha: str | None = None
    coder_pushed_sha: str | None = None


def _optional_sha(section: object, key: str) -> str | None:
    if not isinstance(section, dict):
        return None
    value = section.get(key)
    return value if isinstance(value, str) else None


def read_checkpoint_heads(payload: Mapping[str, object]) -> CheckpointHeads:
    """checkpoint payloadからhead群を読む（schema検証済みの値だけを見る）。"""
    heads = payload.get("heads")
    coder = payload.get("coder")
    return CheckpointHeads(
        observed_sha=_optional_sha(heads, "observed_sha"),
        approved_sha=_optional_sha(heads, "approved_sha"),
        base_sha=_optional_sha(heads, "base_sha"),
        coder_pushed_sha=_optional_sha(coder, "pushed_head_sha"),
    )


@dataclass(frozen=True)
class ApprovalEvidence:
    """head bindingを持つ承認record（検証済みchainから収集する）。"""

    kind: RecordKind
    binding: str
    head_sha: str
    comment_id: str
    detail: str | None


@unique
class ApprovalState(Enum):
    """承認1件の現在の扱い。"""

    VALID = "VALID"  # 現在のadvertised headへbindされている
    SUPERSEDED = "SUPERSEDED"  # 同種の承認が現headにもある（過去世代。判定へ影響しない）
    VOIDED = "VOIDED"  # 現headに同種の承認が無く、失効している


@dataclass(frozen=True)
class ApprovalStatus:
    """承認の現在の扱い。失効・置換の理由は診断のために保持する。"""

    evidence: ApprovalEvidence
    state: ApprovalState
    reason: str | None = None

    @property
    def valid(self) -> bool:
        return self.state is ApprovalState.VALID


def collect_approvals(records: Sequence[VerifiedRecord]) -> tuple[ApprovalEvidence, ...]:
    """head bindingを持つ承認を集める（merge承認とreview承認の2種のみ）。

    `MERGE_APPROVAL`のheadは`approved_head_sha`、`REVIEW_RESULT`のheadは
    `target_head_sha`が射影元（ADR-0010の`PROJECTION_SPECS`）で、いずれもmarkerの
    `head`として観測できる。承認以外のrecord（CHANGES_REQUESTED等）は集めない。
    """
    approvals: list[ApprovalEvidence] = []
    for record in records:
        result = record.projection.result
        if record.kind is RecordKind.MERGE_APPROVAL or (
            record.kind is RecordKind.REVIEW_RESULT and result == _APPROVED_VERDICT
        ):
            approvals.append(
                ApprovalEvidence(
                    kind=record.kind,
                    binding=record.key,
                    head_sha=record.head_sha,
                    comment_id=record.comment_id,
                    detail=result,
                )
            )
    return tuple(approvals)


@unique
class HeadChange(Enum):
    """観測headとcheckpointの記録の関係（分類。失効判定の根拠にはしない）。"""

    UNCHANGED = "UNCHANGED"
    CODER_PUSH = "CODER_PUSH"
    EXTERNAL_UPDATE = "EXTERNAL_UPDATE"
    UNKNOWN = "UNKNOWN"


@unique
class ResumeVerdict(Enum):
    """resume preflightの判定（C-01 eventへの写像はstateにも依存する。構築はC-10）。"""

    VALIDATED = "VALIDATED"
    FALLBACK_REQUIRED = "FALLBACK_REQUIRED"
    SAME_HEAD_VALIDATED = "SAME_HEAD_VALIDATED"


@dataclass(frozen=True)
class HeadReconciliation:
    """照合結果。観測事実をそのまま同梱し、消費側が見落とせないようにする。"""

    verdict: ResumeVerdict
    change: HeadChange
    observation: HeadObservation
    approvals: tuple[ApprovalStatus, ...]
    detail: str

    def _of(self, state: ApprovalState) -> tuple[ApprovalEvidence, ...]:
        return tuple(status.evidence for status in self.approvals if status.state is state)

    @property
    def valid_approvals(self) -> tuple[ApprovalEvidence, ...]:
        return self._of(ApprovalState.VALID)

    @property
    def voided_approvals(self) -> tuple[ApprovalEvidence, ...]:
        return self._of(ApprovalState.VOIDED)

    @property
    def superseded_approvals(self) -> tuple[ApprovalEvidence, ...]:
        return self._of(ApprovalState.SUPERSEDED)


def _classify(observation: HeadObservation, heads: CheckpointHeads) -> HeadChange:
    if heads.observed_sha is None:
        return HeadChange.UNKNOWN
    if heads.observed_sha == observation.head_sha:
        return HeadChange.UNCHANGED
    if heads.coder_pushed_sha == observation.head_sha:
        return HeadChange.CODER_PUSH
    return HeadChange.EXTERNAL_UPDATE


def _evaluate_approvals(
    approvals: Sequence[ApprovalEvidence], head_sha: str
) -> tuple[ApprovalStatus, ...]:
    """承認ごとの扱いを決める（現head / superseded / 失効）。

    canonical recordはappend-onlyなので、旧headの承認は履歴に残り続ける。同種の承認が
    現headにも存在するなら、旧世代は置き換え済み（`SUPERSEDED`）として判定から外す。
    現headに同種の承認が無い場合だけ失効（`VOIDED`）として扱う。
    """
    current_kinds = {evidence.kind for evidence in approvals if evidence.head_sha == head_sha}
    statuses: list[ApprovalStatus] = []
    for evidence in approvals:
        if evidence.head_sha == head_sha:
            statuses.append(ApprovalStatus(evidence=evidence, state=ApprovalState.VALID))
        elif evidence.kind in current_kinds:
            statuses.append(
                ApprovalStatus(
                    evidence=evidence,
                    state=ApprovalState.SUPERSEDED,
                    reason=f"同種の承認が現在のheadにも存在する（旧head {evidence.head_sha}）",
                )
            )
        else:
            statuses.append(
                ApprovalStatus(
                    evidence=evidence,
                    state=ApprovalState.VOIDED,
                    reason=f"承認は{evidence.head_sha}へbindされており、現在のheadと一致しない",
                )
            )
    return tuple(statuses)


def reconcile_head(
    observation: HeadObservation,
    *,
    records: Sequence[VerifiedRecord],
    heads: CheckpointHeads,
    checkpoint_state: State | None = None,
    external_approvals: Sequence[ApprovalEvidence] = (),
) -> HeadReconciliation:
    """advertised headと承認recordを照合し、resume preflightの判定を返す。

    承認は「bind先headが現在のadvertised headと一致するか」だけで判定する。同種の
    現head承認で置き換えられた旧世代は判定へ影響させず（`SUPERSEDED`）、置き換えの
    無い旧承認が1件でもあれば`FALLBACK_REQUIRED`（継続破棄・承認失効 + fresh review）
    とする。

    `external_approvals`はGitHub直接commentとして受理された承認（C-06の
    `accept_user_decision`の結果からPR-4が組み立てる）。chain recordを伴わない承認経路
    （D-021）でも同じ判定を通すために受け取る。

    `MERGE_FAILED`からの再開が`SAME_HEAD_VALIDATED`になるのは、**現headへbindされた
    merge承認とreview承認がGitHub上で確認できる場合だけ**。local checkpointの
    `approved_sha`だけでmerge gateへ復帰させない。
    """
    change = _classify(observation, heads)
    statuses = _evaluate_approvals(
        (*collect_approvals(records), *external_approvals), observation.head_sha
    )
    voided = [status for status in statuses if status.state is ApprovalState.VOIDED]
    if voided:
        return HeadReconciliation(
            verdict=ResumeVerdict.FALLBACK_REQUIRED,
            change=change,
            observation=observation,
            approvals=statuses,
            detail=f"headが変わり承認{len(voided)}件が失効した（{change.value}）",
        )
    if change in (HeadChange.CODER_PUSH, HeadChange.EXTERNAL_UPDATE):
        return HeadReconciliation(
            verdict=ResumeVerdict.FALLBACK_REQUIRED,
            change=change,
            observation=observation,
            approvals=statuses,
            detail=f"最後に観測したheadから変化している（{change.value}）",
        )
    if checkpoint_state is State.MERGE_FAILED:
        confirmed = {status.evidence.kind for status in statuses if status.valid}
        missing = sorted(kind.value for kind in _MERGE_GATE_APPROVALS - confirmed)
        if not missing:
            return HeadReconciliation(
                verdict=ResumeVerdict.SAME_HEAD_VALIDATED,
                change=change,
                observation=observation,
                approvals=statuses,
                detail="merge失敗後の同一headで、必要な承認をGitHub上で確認できた",
            )
        return HeadReconciliation(
            verdict=ResumeVerdict.VALIDATED,
            change=change,
            observation=observation,
            approvals=statuses,
            detail=f"現headの承認をGitHub上で確認できない（{','.join(missing)}）ため再確認しない",
        )
    return HeadReconciliation(
        verdict=ResumeVerdict.VALIDATED,
        change=change,
        observation=observation,
        approvals=statuses,
        detail=f"headは承認のbind先と一致している（{change.value}）",
    )
