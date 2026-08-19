<!-- SPDX-License-Identifier: Apache-2.0 -->

# Phase 1計画: C-01 domain state machine

| Field | Value |
| --- | --- |
| Status | **Draft**（本計画PRのユーザー承認とmergeをもって実装の正本とする） |
| 正本関係 | [implementation plan](implementation-plan.md)のC-01節の詳細設計。target behaviorは[target experience](target-experience.md)に従い、本書は変更しない |
| 対応Issue | #6（本書は計画。Issue #6のcloseはC-01実装PRで行う） |
| 受入条件 | AC-C01-01〜07 |

## 1. 目的

target experienceの「State model」節が定義する17 stateと、同「User intervention」「Failure, cancellation, and resume experience」節の挙動を、**単一の遷移registry**として実装可能な粒度で確定する。ユーザー向け簡略図が省略している遷移（失敗系からのresume、cancel可否、GitHub投稿・確認失敗、preflight失敗）をすべて定義する。

## 2. MachineState

可視の17 stateとは別に、immutableな`MachineState`を導入する。

```text
MachineState（frozen）
  state: State                 # 可視の17 stateのいずれか
  return_to: State | None      # 待機状態からの復帰先。AWAITING_TOOL_PERMISSION進入時のみ設定
  gate: RecordEvidence | None  # 直近の検証済みcanonical recordへの型付き参照
```

- `return_to`は`AWAITING_TOOL_PERMISSION`へ入る遷移がregistry内で設定し、復帰eventが参照する。**eventの引数として外部から復帰先を渡すことはできない**（不正な状態jumpの防止）
- `gate`は後続層（C-06）が発行する検証済みevidenceの型付き参照であり、boolean flagではない。Phase 1では`RecordEvidence`をopaqueなvalue object（record種別 + 参照ID）として定義し、実際の検証はC-06が担う
- round数・clarification turn数などの**数量判定はC-01の外**で行う。上限到達などの判定結果は専用event（`EV_ROUND_LIMIT_REACHED`等）としてC-01へ入力される。これによりguardはMachineStateとeventの内容だけで決まり、registryが有限で全数検査できる

## 3. Event体系

### 3.1 二段階遷移の規約（canonical record gate）

agent / userの発言を伴うすべての遷移は、次の対で構成される。

1. `EV_*_PRODUCED`: 発言が生成された。**状態を変えず**、`CMD_PERSIST_RECORD(kind)`だけを返す
2. `EV_*_VERIFIED`: 後続層がGitHubへ投稿しread-after-writeとrecord検証を完了した（`RecordEvidence`を運ぶ）。**このeventだけが状態を進め、次agentを起動するcommandを返せる**

この規約により、AC-C01-03（evidenceなしで次agent commandを生成できない）が構造的に成立する。以下の遷移表は`VERIFIED`側のみを列挙し、`PRODUCED`側は全stateで共通の規則（状態不変 + persist command）とする。投稿・確認の失敗は`EV_RUN_FAILED`（bounded retry後）として入力され、次agentを起動せず`FAILED`へ遷移する。

### 3.2 Event一覧

| Event | 意味 | 発生元component |
| --- | --- | --- |
| `EV_PREFLIGHT_OK` / `EV_PREFLIGHT_NG` | 対象・policy・head・lockの検証結果 | C-07 / C-08 |
| `EV_REVIEW_BLOCKING_VERIFIED` | blocking findingを含むCodex reviewが検証済み | C-06経由 |
| `EV_REVIEW_APPROVED_VERIFIED` | 現在headへのreview承認が検証済み | C-06経由 |
| `EV_FIX_STARTED` | hostがfinding対応へ着手 | C-08 |
| `EV_FIX_RESULT_VERIFIED` | 修正summaryと新headの記録が検証済み | C-06経由 |
| `EV_CLARIFICATION_QUESTION_VERIFIED` | Claudeの質問が検証済み | C-06経由 |
| `EV_CLARIFICATION_CONFIRMED_VERIFIED` / `EV_CLARIFICATION_REVISED_VERIFIED` | Codex回答（維持 / 修正）が検証済み | C-06経由 |
| `EV_CLARIFICATION_WITHDRAWN_VERIFIED` | finding撤回が検証済み | C-06経由 |
| `EV_CLARIFICATION_ESCALATED_VERIFIED` | `USER_DECISION_REQUIRED`回答が検証済み | C-06経由 |
| `EV_CLARIFICATION_STALLED` | no-progressまたは5 turn上限（判定は外部） | C-11 |
| `EV_DECISION_REQUEST_VERIFIED` | Claudeのdraft decision requestが検証済み | C-06経由 |
| `EV_VERDICT_ASK_USER_BRIEF_VERIFIED` | `ASK_USER` verdictと最終briefが検証済み | C-06経由 |
| `EV_VERDICT_PROCEED_VERIFIED` | `PROCEED_WITH_RECORD`と記録が検証済み | C-06経由 |
| `EV_VERDICT_RESUBMIT_VERIFIED` | `REVISE_AND_RESUBMIT`の再提出が検証済み | C-06経由 |
| `EV_DECISION_UNRESOLVED` | 判断フローが進行不能（判定は外部） | C-11 |
| `EV_USER_DECISION_VERIFIED` | ユーザー決定のcanonical recordが検証済み | C-06経由 |
| `EV_TOOL_PERMISSION_BLOCKED` | 例外permission blockの記録が検証済み | C-06経由 |
| `EV_PERMISSION_RESUME_VALIDATED` | Permission IDとheadの再検証を伴う明示resume | C-08 |
| `EV_CI_SUCCEEDED` / `EV_CI_INFRA_FAILURE` | 対象headのCI結果 | C-12 |
| `EV_CI_CODE_FAILURE_VERIFIED` | code起因のCI失敗と対応要否の記録が検証済み | C-06経由 |
| `EV_CI_TIMEOUT_RECORDED` | bounded wait上限とGitHubへの記録が検証済み | C-06経由 |
| `EV_CI_RESUME_REQUESTED` | `WAITING_CI`からの明示resume | C-08 |
| `EV_REPORT_VERIFIED` / `EV_REPORT_FAILED` | final reportの投稿検証 / 生成失敗 | C-06経由 / C-12 |
| `EV_REPORTER_RETRY_REQUESTED` | reporterのみ再実行の明示指示 | C-08 |
| `EV_GATE_ANSWER_VERIFIED` | gateでの質問への回答が検証済み | C-06経由 |
| `EV_GATE_CHANGES_VERIFIED` | gateでの修正依頼が検証済み | C-06経由 |
| `EV_MERGE_APPROVAL_VERIFIED` | bind済みmerge承認recordが検証済み | C-06経由 |
| `EV_MERGE_CONFIRMED` | GitHub上のmerge完了とmerged SHAを確認 | C-13 |
| `EV_MERGE_PRECONDITION_MISMATCH` | 直前再検証の不一致 | C-13 |
| `EV_MERGE_OUTCOME_UNKNOWN` | merge結果を照会しても確定できない | C-13 |
| `EV_HEAD_CHANGED_EXTERNALLY` | 外部からのhead更新を検出 | C-07 |
| `EV_ROUND_LIMIT_REACHED` | review / fix round上限（判定は外部） | C-10 |
| `EV_NO_PROGRESS` | 同一findingの膠着等（判定は外部） | C-10 / C-11 |
| `EV_CANCEL_REQUESTED` | ユーザーのcancel入力 | C-08 |
| `EV_RUN_FAILED` | auth / network / schema / GitHub操作等のbounded retry後失敗 | 各層 |
| `EV_RESUME_PREFLIGHT_OK` | resume時のpreflightと状態再構築の成功 | C-07 |
| `EV_RESUME_SAME_HEAD_VALIDATED` | merge失敗後、同一head・全条件有効の再確認 | C-07 / C-13 |

### 3.3 Command一覧

| Command | 意味 | 実行するcomponent |
| --- | --- | --- |
| `CMD_PERSIST_RECORD(kind)` | canonical recordの投稿と検証を依頼 | C-05 / C-06 |
| `CMD_REQUEST_CODEX_REVIEW` | fresh reviewerの起動を依頼 | C-09 |
| `CMD_REQUEST_HOST_ACTION(kind)` | active hostへの作業依頼（`APPLY_FINDINGS`等） | C-08 |
| `CMD_CHECK_CI` | 対象headのCI確認を依頼 | C-12 |
| `CMD_GENERATE_REPORT` | final reporterの起動を依頼 | C-12 |
| `CMD_VERIFY_MERGE_PRECONDITIONS` | merge直前の全条件再検証を依頼 | C-13 |
| `CMD_EXECUTE_MERGE` | 検証一致時のmerge実行を依頼 | C-13 |
| `CMD_QUERY_MERGE_OUTCOME` | merge結果のGitHub照会を依頼 | C-13 |
| `CMD_INVALIDATE_APPROVALS` | review / merge承認の失効を依頼 | C-07 |

commandは記述のみであり、C-01は実行しない。1遷移が返すcommand列の順序は決定論的とする（AC-C01-04）。

## 4. Terminal / resumable分類

| 分類 | State | 復帰 |
| --- | --- | --- |
| terminal | `MERGED`、`CANCELLED` | 不可。いかなるeventも受理しない（構造化errorで拒否） |
| resumable（checkpoint終了） | `WAITING_CI`、`AWAITING_USER_DECISION`、`AWAITING_TOOL_PERMISSION`、`READY_FOR_HUMAN_MERGE`、`BLOCKED`、`FAILED`、`REPORT_FAILED`、`MERGE_FAILED` | 定義されたresume eventのみ（下表） |
| active | 上記以外の7 state | run内で遷移 |

resume経路（明示resume時。GitHubからの状態再構築はC-07の責務）:

| From | Event | To | 備考 |
| --- | --- | --- | --- |
| `FAILED` | `EV_RESUME_PREFLIGHT_OK` | `RUNNING_REVIEW` | 現在headのfresh reviewから安全に再開 |
| `BLOCKED` | `EV_RESUME_PREFLIGHT_OK` | `RUNNING_REVIEW` | 手動修正・上限変更後の再開もfresh reviewを経由 |
| `REPORT_FAILED` | `EV_REPORTER_RETRY_REQUESTED` | `GENERATING_REPORT` | review承認は保持（reporterのみ再実行） |
| `MERGE_FAILED` | `EV_RESUME_SAME_HEAD_VALIDATED` | `READY_FOR_HUMAN_MERGE` | 新しい明示承認を要求。head変化時は`EV_HEAD_CHANGED_EXTERNALLY`で`RUNNING_REVIEW` |
| `WAITING_CI` | `EV_CI_RESUME_REQUESTED` | `WAITING_CI` | 再入後にCI結果eventで解決 |

## 5. 完全遷移表

registryの正本。実装はこの表をdataとして持ち、遷移表・遷移図をここから導出する（AC-C01-01 / AC-C01-07）。`EV_CANCEL_REQUESTED`と`EV_RUN_FAILED`は共通規則として最後にまとめる。

| # | From | Event | To | Commands |
| --- | --- | --- | --- | --- |
| 1 | （開始） | `EV_PREFLIGHT_OK` | `RUNNING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW` |
| 2 | （開始） | `EV_PREFLIGHT_NG` | `FAILED` | — |
| 3 | `RUNNING_REVIEW` | `EV_REVIEW_BLOCKING_VERIFIED` | `CHANGES_REQUESTED` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` |
| 4 | `RUNNING_REVIEW` | `EV_REVIEW_APPROVED_VERIFIED` | `WAITING_CI` | `CMD_CHECK_CI` |
| 5 | `RUNNING_REVIEW` | `EV_TOOL_PERMISSION_BLOCKED` | `AWAITING_TOOL_PERMISSION` | —（`return_to = RUNNING_REVIEW`） |
| 6 | `RUNNING_REVIEW` | `EV_ROUND_LIMIT_REACHED` / `EV_NO_PROGRESS` | `BLOCKED` | — |
| 7 | `CHANGES_REQUESTED` | `EV_FIX_STARTED` | `APPLYING_FIXES` | — |
| 8 | `CHANGES_REQUESTED` | `EV_CLARIFICATION_QUESTION_VERIFIED` | `CLARIFYING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW`（clarification回答用のfresh session） |
| 9 | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_CONFIRMED_VERIFIED` / `EV_CLARIFICATION_REVISED_VERIFIED` | `CHANGES_REQUESTED` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` |
| 10 | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_WITHDRAWN_VERIFIED` | `RUNNING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW` |
| 11 | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_ESCALATED_VERIFIED` | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_CODEX_REVIEW`（verdict評価） |
| 12 | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_STALLED` | `BLOCKED` | — |
| 13 | `APPLYING_FIXES` | `EV_FIX_RESULT_VERIFIED` | `RUNNING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW` |
| 14 | `APPLYING_FIXES` | `EV_DECISION_REQUEST_VERIFIED` | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_CODEX_REVIEW`（verdict評価） |
| 15 | `APPLYING_FIXES` | `EV_TOOL_PERMISSION_BLOCKED` | `AWAITING_TOOL_PERMISSION` | —（`return_to = APPLYING_FIXES`） |
| 16 | `APPLYING_FIXES` | `EV_NO_PROGRESS` | `BLOCKED` | — |
| 17 | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_ASK_USER_BRIEF_VERIFIED` | `AWAITING_USER_DECISION` | — |
| 18 | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_PROCEED_VERIFIED` | `APPLYING_FIXES` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` |
| 19 | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_RESUBMIT_VERIFIED` | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_CODEX_REVIEW`（再評価） |
| 20 | `REVIEWING_DECISION_REQUEST` | `EV_DECISION_UNRESOLVED` | `BLOCKED` | — |
| 21 | `AWAITING_USER_DECISION` | `EV_USER_DECISION_VERIFIED` | `APPLYING_FIXES` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` |
| 22 | `AWAITING_TOOL_PERMISSION` | `EV_PERMISSION_RESUME_VALIDATED` | `return_to`（`RUNNING_REVIEW`なら`CMD_REQUEST_CODEX_REVIEW`、`APPLYING_FIXES`なら`CMD_REQUEST_HOST_ACTION`） | 復帰先はMachineStateから解決 |
| 23 | `WAITING_CI` | `EV_CI_SUCCEEDED` | `GENERATING_REPORT` | `CMD_GENERATE_REPORT` |
| 24 | `WAITING_CI` | `EV_CI_CODE_FAILURE_VERIFIED` | `CHANGES_REQUESTED` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` |
| 25 | `WAITING_CI` | `EV_CI_INFRA_FAILURE` | `WAITING_CI` | `CMD_CHECK_CI`（bounded retryは外部管理） |
| 26 | `WAITING_CI` | `EV_CI_TIMEOUT_RECORDED` | `WAITING_CI` | —（runはcheckpointで終了。resumeは#4.の表） |
| 27 | `WAITING_CI` | `EV_HEAD_CHANGED_EXTERNALLY` | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW` |
| 28 | `GENERATING_REPORT` | `EV_REPORT_VERIFIED` | `READY_FOR_HUMAN_MERGE` | — |
| 29 | `GENERATING_REPORT` | `EV_REPORT_FAILED` | `REPORT_FAILED` | — |
| 30 | `READY_FOR_HUMAN_MERGE` | `EV_GATE_ANSWER_VERIFIED` | `READY_FOR_HUMAN_MERGE` | — |
| 31 | `READY_FOR_HUMAN_MERGE` | `EV_GATE_CHANGES_VERIFIED` | `CHANGES_REQUESTED` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` |
| 32 | `READY_FOR_HUMAN_MERGE` | `EV_MERGE_APPROVAL_VERIFIED` | `MERGING` | `CMD_VERIFY_MERGE_PRECONDITIONS`、`CMD_EXECUTE_MERGE` |
| 33 | `READY_FOR_HUMAN_MERGE` | `EV_HEAD_CHANGED_EXTERNALLY` | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW` |
| 34 | `MERGING` | `EV_MERGE_CONFIRMED` | `MERGED` | — |
| 35 | `MERGING` | `EV_MERGE_PRECONDITION_MISMATCH` | `MERGE_FAILED` | — |
| 36 | `MERGING` | `EV_MERGE_OUTCOME_UNKNOWN` | `MERGE_FAILED` | — |
| 37 | `MERGING` | `EV_HEAD_CHANGED_EXTERNALLY` | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW` |
| 38 | `MERGING` | `EV_CANCEL_REQUESTED` | `MERGING` | `CMD_QUERY_MERGE_OUTCOME`（結果eventで#34 / #36が解決。**即CANCELLEDにしない**） |
| 39〜43 | resume経路 | （#4.の表のとおり） | | |

**共通規則**:

- `EV_CANCEL_REQUESTED`: active状態およびresumable状態（`MERGING`と`MERGED` / `CANCELLED`を除く）から`CANCELLED`へ。`MERGING`は#38、terminalは拒否
- `EV_RUN_FAILED`: terminal以外の全stateから`FAILED`へ（GitHub投稿・確認のbounded retry後失敗を含む。次agentは起動しない）
- 上記に列挙のないstate × event組合せはすべて**未定義遷移**であり、silent no-opにせず構造化errorとして拒否する（AC-C01-02）

## 6. C-01のscope境界（Phase 1）

**実装する**: `domain/states.py`（17 state + terminal / resumable分類）、`domain/events.py`、`domain/commands.py`、`domain/machine.py`（registryと遷移関数）、最小のvalue object（`RecordEvidence`）。

**実装しない（out of scope）**:

- GitHub APIアクセス（C-05）
- actor認証・record chain検証（C-06）。`RecordEvidence`の中身の真正性はC-06の責務
- checkpoint永続化・状態再構築（C-07）
- subprocess起動（C-03 / C-09）
- advance / submit step engine（C-08）
- finding ledgerの本実装（Phase 10）とid生成（必要とするPhaseで追加）。空moduleも作らない
- CLI / Skill / wrapper

## 7. Test計画

- registryをdataとして全state × event組合せをtable-drivenで検査（未定義は構造化error）
- 17 stateの到達可能性と、到達不能stateの検出
- 純粋性: 同一入力の再適用で同一結果、入力MachineState / eventの非変更、I/O・時刻・乱数・環境変数への非依存、command列順序の決定性
- terminalからの全event拒否。resumableは定義済みresume eventのみ受理
- `AWAITING_TOOL_PERMISSION`の復帰先が進入元ごとに正しいこと、eventから注入できないこと
- `PRODUCED`系eventが状態を変えず、`VERIFIED`系evidenceなしで次agent commandが出ないこと
- `MERGING` + cancelが`CMD_QUERY_MERGE_OUTCOME`を経由し、`EV_MERGE_OUTCOME_UNKNOWN`で`MERGE_FAILED`となること
- 遷移表・遷移図の導出結果がregistryおよびtarget experienceの簡略図と矛盾しないこと

## 8. 本計画のreviewで確認したい点

1. 遷移表#8 / #11 / #14 / #19でのfresh Codex session起動の位置づけ（clarification / verdict評価をreview commandの変種とするか、独立commandにするか）
2. `EV_CI_CODE_FAILURE_VERIFIED`の遷移先を`CHANGES_REQUESTED`とした判断（target experienceは「code failureはClaude」とのみ記載）
3. `BLOCKED` / `FAILED`からのresumeを一律fresh review経由とした判断（安全側だが、停止地点への直接復帰より1 round増える）
