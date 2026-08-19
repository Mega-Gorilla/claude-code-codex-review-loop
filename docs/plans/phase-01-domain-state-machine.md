<!-- SPDX-License-Identifier: Apache-2.0 -->

# Phase 1計画: C-01 domain state machine

| Field | Value |
| --- | --- |
| Status | **Accepted**（本計画PRのユーザー承認とmergeにより確定） |
| 正本関係 | [implementation plan](implementation-plan.md)のC-01節の詳細設計。target behaviorは[target experience](target-experience.md)に従い、本書は変更しない |
| 対応Issue | #6（本書は計画。Issue #6のcloseはC-01実装PRで行う） |
| 受入条件 | AC-C01-01〜07 |

## 1. 目的と正本の役割分担

target experienceの「State model」節が定義する17 stateと、「User intervention」「Failure, cancellation, and resume experience」節の挙動を、実装可能な粒度で確定する。ユーザー向け簡略図が省略している遷移（失敗系からのresume、cancel可否、GitHub投稿・確認失敗、preflight失敗）をすべて定義する。

正本の役割は次のとおり分担し、二重の正本を作らない。

| 資料 | 役割 |
| --- | --- |
| 本計画文書 | **normativeな期待挙動**。実装が満たすべき遷移・規則・不変条件 |
| 実装のcode registry | **実行可能な単一source**。全ruleをdataとして保持する |
| 生成された遷移表・遷移図 | code registryから導出し、本書の表とのsnapshot照合をtestで行う（AC-C01-07） |

**registryの一意性不変条件**: 各`(state, event, guard)`に一致するruleは**0件または1件**である。共通規則（cancel / failure等）はregistry内で個別ruleへ展開され、重複ruleの存在はtestでfailさせる。優先順位による解決は行わない。

## 2. MachineState

可視の17 stateとは別に、immutableな`MachineState`を導入する。

```text
MachineState（frozen）
  state: State                       # 可視の17 stateのいずれか
  return_to: State | None            # AWAITING_TOOL_PERMISSIONからの復帰先
  recovery_to: State | None          # BLOCKED / FAILEDからの安全な再開地点
  awaiting: Awaiting | None          # 依頼済みで応答待ちの相手（CODEX_REVIEW / HOST_ACTION等、kind付き）
  pending_record: PendingRecord | None  # 永続化の確認待ちrecord

PendingRecord（frozen）
  kind: RecordKind                   # 期待するrecordの種別
  binding: OpaqueBinding             # logical turnへのopaqueなbinding値
  source_state: State                # PRODUCEDが発生したstate

RecordEvidence（frozen）
  kind: RecordKind
  binding: OpaqueBinding
  ref: RecordRef                     # 検証済みrecordへのopaque参照
```

- `return_to` / `recovery_to`は**registry内の遷移ruleだけが設定**し、eventから注入できない
- `binding`はopaqueな値であり、C-01は**等価比較のみ**を行い意味を解釈しない。値の採番はC-08、真正性の検証はC-06の責務
- round数・clarification turn数などの**数量判定はC-01の外**で行い、判定結果を専用eventとして入力する。guardはMachineStateとeventの内容だけで決まり、registryは有限で全数検査できる

## 3. Event体系

### 3.1 PRODUCED / VERIFIEDのbinding規約（canonical record gate）

agent / userの発言を伴うすべての遷移は、次の対で構成される。

1. `EV_*_PRODUCED(kind, binding)`: 発言が生成された。**registryに列挙された許可source stateでのみ受理**される。状態は変えず、`pending_record = (kind, binding, source_state)`を設定し、`CMD_PERSIST_RECORD(kind, binding)`を返す
2. `EV_*_VERIFIED(evidence)`: 後続層が投稿・read-after-write・record検証を完了した。**`pending_record`とevidenceの`kind`および`binding`が一致する場合だけ**受理され、`pending_record`を消費して状態を進め、次のcommandを返す

この構造により次が成立する。

- 対応する`PRODUCED`を経ていない`VERIFIED`、過去turnのevidenceの再利用、同種別だが別turnのevidenceは、binding不一致として**構造化errorで拒否**される（AC-C01-03）
- `pending_record`が設定されている間は、`EV_CANCEL_REQUESTED`・`EV_RUN_FAILED`・対応する`VERIFIED`以外のsemantic eventを拒否する
- 投稿後・確認前に中断したpartial turnは、`pending_record`がMachineStateに残ることでcheckpoint（C-07）から同一turnとして再開できる
- 投稿・確認のbounded retry後失敗は`EV_RUN_FAILED`として入力され、次agentを起動せず`FAILED`へ遷移する（`MERGING`を除く。Section 5）

### 3.2 Record kind registry

`PRODUCED`が許可されるsource stateと、対応する`VERIFIED`を列挙する（遷移先はSection 5の表）。

| RecordKind | PRODUCED許可state | VERIFIED event |
| --- | --- | --- |
| `REVIEW_RESULT` | `RUNNING_REVIEW` | `EV_REVIEW_BLOCKING_VERIFIED` / `EV_REVIEW_APPROVED_VERIFIED` |
| `FIX_RESULT` | `APPLYING_FIXES` | `EV_FIX_RESULT_VERIFIED` |
| `CLARIFICATION_QUESTION` | `CHANGES_REQUESTED` | `EV_CLARIFICATION_QUESTION_VERIFIED` |
| `CLARIFICATION_ANSWER` | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_{CONFIRMED,REVISED,WITHDRAWN,ESCALATED}_VERIFIED` |
| `DECISION_REQUEST` | `APPLYING_FIXES`、`REVIEWING_DECISION_REQUEST`（draft / revise） | `EV_DECISION_REQUEST_VERIFIED` |
| `DECISION_VERDICT` | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_{ASK_USER,PROCEED,RESUBMIT}_VERIFIED` |
| `DECISION_BRIEF` | `REVIEWING_DECISION_REQUEST` | `EV_DECISION_BRIEF_VERIFIED` |
| `DECISION_RECORD` | `REVIEWING_DECISION_REQUEST` | `EV_DECISION_RECORD_VERIFIED` |
| `USER_DECISION` | `AWAITING_USER_DECISION` | `EV_USER_DECISION_VERIFIED` |
| `PERMISSION_BLOCK` | `RUNNING_REVIEW`、`APPLYING_FIXES` | `EV_TOOL_PERMISSION_BLOCKED` |
| `CI_TIMEOUT` | `WAITING_CI` | `EV_CI_TIMEOUT_RECORDED` |
| `CI_CODE_FAILURE` | `WAITING_CI` | `EV_CI_CODE_FAILURE_VERIFIED` |
| `FINAL_REPORT` | `GENERATING_REPORT` | `EV_REPORT_VERIFIED` |
| `GATE_ANSWER` | `READY_FOR_HUMAN_MERGE` | `EV_GATE_ANSWER_VERIFIED` |
| `GATE_CHANGES` | `READY_FOR_HUMAN_MERGE` | `EV_GATE_CHANGES_VERIFIED` |
| `MERGE_APPROVAL` | `READY_FOR_HUMAN_MERGE` | `EV_MERGE_APPROVAL_VERIFIED` |

### 3.3 record以外のevent

| Event | 意味 | 発生元 |
| --- | --- | --- |
| `EV_PREFLIGHT_OK` / `EV_PREFLIGHT_NG` | 対象・policy・head・lockの検証結果（`initialize`専用） | C-07 / C-08 |
| `EV_FIX_STARTED` | hostがfinding対応へ着手 | C-08 |
| `EV_CLARIFICATION_STALLED` | no-progressまたは5 turn上限（判定は外部） | C-11 |
| `EV_DECISION_UNRESOLVED` | 判断フローが進行不能（判定は外部） | C-11 |
| `EV_PERMISSION_RESUME_VALIDATED` | Permission IDとheadの再検証を伴う明示resume | C-08 |
| `EV_CI_SUCCEEDED` / `EV_CI_INFRA_FAILURE` | 対象headのCI結果 | C-12 |
| `EV_CI_RESUME_REQUESTED` | `WAITING_CI`からの明示resume | C-08 |
| `EV_REPORT_FAILED` | report生成失敗 | C-12 |
| `EV_REPORTER_RETRY_REQUESTED` | reporterのみ再実行の明示指示 | C-08 |
| `EV_MERGE_PRECONDITIONS_OK` | merge直前の全条件再検証が承認recordと完全一致 | C-13 |
| `EV_MERGE_PRECONDITION_MISMATCH` | 直前再検証の不一致（head以外） | C-13 |
| `EV_MERGE_CONFIRMED` | GitHub上のmerge完了とmerged SHAを確認 | C-13 |
| `EV_MERGE_NOT_EXECUTED_CONFIRMED` | GitHub照会でmerge未実行を確認 | C-13 |
| `EV_MERGE_OUTCOME_UNKNOWN` | 照会してもmerge結果を確定できない | C-13 |
| `EV_HEAD_CHANGED_EXTERNALLY` | 外部からのhead更新を検出 | C-07 |
| `EV_ROUND_LIMIT_REACHED` / `EV_NO_PROGRESS` | 上限・膠着（判定は外部） | C-10 / C-11 |
| `EV_CANCEL_REQUESTED` | ユーザーのcancel入力 | C-08 |
| `EV_RUN_FAILED` | bounded retry後の失敗（投稿・確認失敗を含む） | 各層 |
| `EV_RESUME_VALIDATED` | resume preflightと状態再構築の成功（復帰先は`recovery_to`） | C-07 |
| `EV_RESUME_FALLBACK_REQUIRED` | head変更・checkpoint不整合等で`recovery_to`を安全に証明できない | C-07 |
| `EV_RESUME_SAME_HEAD_VALIDATED` | merge失敗後、同一head・全条件有効の再確認 | C-07 / C-13 |

### 3.4 Command一覧

| Command | 意味 | 実行component |
| --- | --- | --- |
| `CMD_PERSIST_RECORD(kind, binding)` | canonical recordの投稿と検証を依頼 | C-05 / C-06 |
| `CMD_REQUEST_CODEX_REVIEW(purpose)` | fresh reviewerの起動。`purpose ∈ {CODE_REVIEW, CLARIFICATION, DECISION_VERDICT}`でprompt・schema・入力contextを区別し、実行基盤はC-09へ集約 | C-09 |
| `CMD_REQUEST_HOST_ACTION(kind)` | active hostへの作業依頼（`APPLY_FINDINGS` / `DRAFT_DECISION_REQUEST` / `DRAFT_DECISION_BRIEF` / `RECORD_DECISION` / `REVISE_DECISION_REQUEST`等） | C-08 |
| `CMD_CHECK_CI` | 対象headのCI確認 | C-12 |
| `CMD_GENERATE_REPORT` | final reporterの起動 | C-12 |
| `CMD_VERIFY_MERGE_PRECONDITIONS` | merge直前の全条件再検証 | C-13 |
| `CMD_EXECUTE_MERGE` | **`EV_MERGE_PRECONDITIONS_OK`の後にのみ発行される**merge実行 | C-13 |
| `CMD_QUERY_MERGE_OUTCOME` | merge結果のGitHub照会 | C-13 |
| `CMD_INVALIDATE_APPROVALS` | review / merge承認の失効 | C-07 |

commandは記述のみであり、C-01は実行しない。1遷移が返すcommand列の順序は決定論的とする。**command列に条件分岐の意味は無い**。条件で結果が分かれる処理は、必ず結果eventを受けて次のruleが判断する（merge実行が典型。Section 5）。

## 4. 分類とresume registry

| 分類 | State |
| --- | --- |
| terminal | `MERGED`、`CANCELLED`（全event拒否） |
| resumable | `WAITING_CI`、`AWAITING_USER_DECISION`、`AWAITING_TOOL_PERMISSION`、`READY_FOR_HUMAN_MERGE`、`BLOCKED`、`FAILED`、`REPORT_FAILED`、`MERGE_FAILED` |
| active | 残りの7 state |

resume registry（明示resume時に許可されるevent。GitHubからの状態再構築はC-07の責務）:

| From | Event | Guard | To | Commands |
| --- | --- | --- | --- | --- |
| `FAILED` | `EV_RESUME_VALIDATED` | `recovery_to`あり | `recovery_to` | 復帰先stateの駆動command（下注） |
| `FAILED` | `EV_RESUME_FALLBACK_REQUIRED` | — | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)` |
| `BLOCKED` | `EV_RESUME_VALIDATED` | `recovery_to`あり | `recovery_to` | 同上 |
| `BLOCKED` | `EV_RESUME_FALLBACK_REQUIRED` | — | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)` |
| `REPORT_FAILED` | `EV_REPORTER_RETRY_REQUESTED` | — | `GENERATING_REPORT` | `CMD_GENERATE_REPORT` |
| `MERGE_FAILED` | `EV_RESUME_SAME_HEAD_VALIDATED` | — | `READY_FOR_HUMAN_MERGE` | —（新しい明示承認を待つ） |
| `MERGE_FAILED` | `EV_HEAD_CHANGED_EXTERNALLY` | — | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)` |
| `WAITING_CI` | `EV_CI_RESUME_REQUESTED` | — | `WAITING_CI` | `CMD_CHECK_CI` |
| `AWAITING_TOOL_PERMISSION` | `EV_PERMISSION_RESUME_VALIDATED` | `return_to`あり | `return_to` | 復帰先stateの駆動command（下注） |
| `AWAITING_USER_DECISION` | 通常event（`EV_USER_DECISION_PRODUCED`〜） | — | Section 5 | 通常eventがresumeを兼ねる |
| `READY_FOR_HUMAN_MERGE` | 通常event（gate系） | — | Section 5 | 通常eventがresumeを兼ねる |

注: 復帰先stateの駆動commandは`RUNNING_REVIEW -> CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`、`APPLYING_FIXES -> CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`のように、復帰先ごとにregistryが定義する。

`BLOCKED` / `FAILED`への遷移ruleは、進入元に応じた安全な`recovery_to`を設定する（例: `CLARIFYING_REVIEW`からの`BLOCKED`は`recovery_to = CHANGES_REQUESTED`）。**同一head・同一原因での無条件fresh reviewはno-progress loopを再開させ得るため採らず**、`recovery_to`を安全に証明できない場合（head変更・checkpoint不整合）のみfallbackとしてfresh reviewへ入る。

## 5. 完全遷移表

registryの期待挙動。`VERIFIED`系eventのGuard列には`pending_record`一致（kind + binding）が暗黙に含まれる。`PRODUCED`系はSection 3.2の許可stateでのみ受理され、状態を変えず`CMD_PERSIST_RECORD`を返す（表からは省略）。

| # | From | Event | Guard | To | Commands |
| --- | --- | --- | --- | --- | --- |
| 1 | （`initialize` API） | `EV_PREFLIGHT_OK` | — | `RUNNING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)` |
| 2 | （`initialize` API） | `EV_PREFLIGHT_NG` | — | `FAILED` | —（`recovery_to`なし。resumeはfallback経路） |
| 3 | `RUNNING_REVIEW` | `EV_REVIEW_BLOCKING_VERIFIED` | evidence一致 | `CHANGES_REQUESTED` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` |
| 4 | `RUNNING_REVIEW` | `EV_REVIEW_APPROVED_VERIFIED` | evidence一致 | `WAITING_CI` | `CMD_CHECK_CI` |
| 5 | `RUNNING_REVIEW` | `EV_TOOL_PERMISSION_BLOCKED` | evidence一致 | `AWAITING_TOOL_PERMISSION` | —（`return_to = RUNNING_REVIEW`） |
| 6 | `RUNNING_REVIEW` | `EV_ROUND_LIMIT_REACHED` / `EV_NO_PROGRESS` | — | `BLOCKED` | —（`recovery_to = RUNNING_REVIEW`） |
| 7 | `CHANGES_REQUESTED` | `EV_FIX_STARTED` | — | `APPLYING_FIXES` | — |
| 8 | `CHANGES_REQUESTED` | `EV_CLARIFICATION_QUESTION_VERIFIED` | evidence一致 | `CLARIFYING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CLARIFICATION)` |
| 9 | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_CONFIRMED_VERIFIED` / `EV_CLARIFICATION_REVISED_VERIFIED` | evidence一致 | `CHANGES_REQUESTED` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` |
| 10 | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_WITHDRAWN_VERIFIED` | evidence一致 | `RUNNING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)` |
| 11 | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_ESCALATED_VERIFIED` | evidence一致 | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_HOST_ACTION(DRAFT_DECISION_REQUEST)` |
| 12 | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_STALLED` | — | `BLOCKED` | —（`recovery_to = CHANGES_REQUESTED`） |
| 13 | `APPLYING_FIXES` | `EV_FIX_RESULT_VERIFIED` | evidence一致 | `RUNNING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)` |
| 14 | `APPLYING_FIXES` | `EV_DECISION_REQUEST_VERIFIED` | evidence一致 | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_CODEX_REVIEW(DECISION_VERDICT)` |
| 15 | `APPLYING_FIXES` | `EV_TOOL_PERMISSION_BLOCKED` | evidence一致 | `AWAITING_TOOL_PERMISSION` | —（`return_to = APPLYING_FIXES`） |
| 16 | `APPLYING_FIXES` | `EV_NO_PROGRESS` | — | `BLOCKED` | —（`recovery_to = APPLYING_FIXES`） |
| 17 | `REVIEWING_DECISION_REQUEST` | `EV_DECISION_REQUEST_VERIFIED` | evidence一致（draft / revised） | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_CODEX_REVIEW(DECISION_VERDICT)` |
| 18 | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_ASK_USER_VERIFIED` | evidence一致 | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_HOST_ACTION(DRAFT_DECISION_BRIEF)` |
| 19 | `REVIEWING_DECISION_REQUEST` | `EV_DECISION_BRIEF_VERIFIED` | evidence一致 | `AWAITING_USER_DECISION` | — |
| 20 | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_PROCEED_VERIFIED` | evidence一致 | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_HOST_ACTION(RECORD_DECISION)` |
| 21 | `REVIEWING_DECISION_REQUEST` | `EV_DECISION_RECORD_VERIFIED` | evidence一致 | `APPLYING_FIXES` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` |
| 22 | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_RESUBMIT_VERIFIED` | evidence一致 | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_HOST_ACTION(REVISE_DECISION_REQUEST)` |
| 23 | `REVIEWING_DECISION_REQUEST` | `EV_DECISION_UNRESOLVED` | — | `BLOCKED` | —（`recovery_to = REVIEWING_DECISION_REQUEST`） |
| 24 | `AWAITING_USER_DECISION` | `EV_USER_DECISION_VERIFIED` | evidence一致 | `APPLYING_FIXES` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` |
| 25 | `AWAITING_TOOL_PERMISSION` | `EV_PERMISSION_RESUME_VALIDATED` | `return_to`あり | `return_to` | 復帰先の駆動command（Section 4注） |
| 26 | `WAITING_CI` | `EV_CI_SUCCEEDED` | — | `GENERATING_REPORT` | `CMD_GENERATE_REPORT` |
| 27 | `WAITING_CI` | `EV_CI_CODE_FAILURE_VERIFIED` | evidence一致 | `CHANGES_REQUESTED` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` |
| 28 | `WAITING_CI` | `EV_CI_INFRA_FAILURE` | — | `WAITING_CI` | `CMD_CHECK_CI` |
| 29 | `WAITING_CI` | `EV_CI_TIMEOUT_RECORDED` | evidence一致 | `WAITING_CI` | —（runはcheckpointで終了） |
| 30 | `WAITING_CI` | `EV_HEAD_CHANGED_EXTERNALLY` | — | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)` |
| 31 | `GENERATING_REPORT` | `EV_REPORT_VERIFIED` | evidence一致 | `READY_FOR_HUMAN_MERGE` | — |
| 32 | `GENERATING_REPORT` | `EV_REPORT_FAILED` | — | `REPORT_FAILED` | — |
| 33 | `READY_FOR_HUMAN_MERGE` | `EV_GATE_ANSWER_VERIFIED` | evidence一致 | `READY_FOR_HUMAN_MERGE` | — |
| 34 | `READY_FOR_HUMAN_MERGE` | `EV_GATE_CHANGES_VERIFIED` | evidence一致 | `CHANGES_REQUESTED` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` |
| 35 | `READY_FOR_HUMAN_MERGE` | `EV_MERGE_APPROVAL_VERIFIED` | evidence一致 | `MERGING` | **`CMD_VERIFY_MERGE_PRECONDITIONS`のみ** |
| 36 | `READY_FOR_HUMAN_MERGE` | `EV_HEAD_CHANGED_EXTERNALLY` | — | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)` |
| 37 | `MERGING` | `EV_MERGE_PRECONDITIONS_OK` | — | `MERGING` | **`CMD_EXECUTE_MERGE`（この経路でのみ発行）** |
| 38 | `MERGING` | `EV_MERGE_PRECONDITION_MISMATCH` | — | `MERGE_FAILED` | — |
| 39 | `MERGING` | `EV_HEAD_CHANGED_EXTERNALLY` | — | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)` |
| 40 | `MERGING` | `EV_MERGE_CONFIRMED` | — | `MERGED` | — |
| 41 | `MERGING` | `EV_MERGE_NOT_EXECUTED_CONFIRMED` | `awaiting` = 照会（cancel起点） | `CANCELLED` | — |
| 42 | `MERGING` | `EV_MERGE_NOT_EXECUTED_CONFIRMED` | `awaiting` = 照会（failure起点） | `MERGE_FAILED` | —（`EV_RESUME_SAME_HEAD_VALIDATED`で復帰可能） |
| 43 | `MERGING` | `EV_MERGE_OUTCOME_UNKNOWN` | — | `MERGE_FAILED` | — |
| 44 | `MERGING` | `EV_CANCEL_REQUESTED` | — | `MERGING` | `CMD_QUERY_MERGE_OUTCOME`（**即CANCELLEDにしない**。照会起点を`awaiting`へ記録し、#40 / #41 / #43で解決） |
| 45 | `MERGING` | `EV_RUN_FAILED` | — | `MERGING` | `CMD_QUERY_MERGE_OUTCOME`（merge成否不明を通常失敗へ落とさない。照会起点を`awaiting`へ記録し、#40 / #42 / #43で解決） |

**共通規則**（registry内で個別ruleへ展開され、一意性checkの対象になる）:

- `EV_CANCEL_REQUESTED`: terminalと`MERGING`（#44）を除く全stateから`CANCELLED`へ
- `EV_RUN_FAILED`: terminalと`MERGING`（#45）を除く全stateから`FAILED`へ（`recovery_to` = 進入元state。ただし進入元がpending_record確認待ちなら、そのpartial turnをcheckpointが引き継ぐ）
- terminal（`MERGED` / `CANCELLED`）は全eventを構造化errorで拒否
- 上記いずれにも一致しない`(state, event)`は未定義遷移として構造化errorで拒否（AC-C01-02）

**decision flowのGitHub会話順序**（#11 / #14 / #17〜#22 / #24）: target experienceの合意どおり、(a) Claude draft投稿確認（#11はdraft作成の依頼から、#14は投稿確認から）、(b) Codex verdict投稿確認、(c) Claudeの最終brief / decision record / revised draftの投稿確認、(d) 次のCodexまたはユーザー、の順序でGitHub上に両agentの発言が個別に現れる。省略・結合はしない。

## 6. C-01のscope境界（Phase 1）

**実装する**: `domain/states.py`、`domain/events.py`、`domain/commands.py`、`domain/machine.py`（registry・`initialize(preflight_event)`・`transition(machine_state, event)`）、最小のvalue object（`MachineState` / `PendingRecord` / `RecordEvidence` / `OpaqueBinding`）。

**実装しない（out of scope）**: GitHub APIアクセス（C-05）/ actor認証・record chain検証（C-06。evidenceの真正性はC-06の責務）/ checkpoint永続化・状態再構築（C-07）/ subprocess起動（C-03 / C-09）/ advance-submit engine（C-08）/ finding ledger本実装（Phase 10）とid採番（binding値の生成はC-08）/ CLI / Skill / wrapper。空moduleも作らない。

## 7. Test計画

- registryをdataとして全state × event組合せをtable-drivenで検査（未定義は構造化error）
- **一意性**: 展開後のregistryで同一`(state, event, guard)`のruleが2件以上あればfail
- 17 stateの到達可能性、到達不能stateの検出、遷移表・遷移図のsnapshot照合（本書Section 5と）
- 純粋性: 同一入力の再適用で同一結果、入力非変更、I/O・時刻・乱数・環境変数への非依存、command列順序の決定性
- terminalからの全event拒否。resumableはresume registryのeventのみ受理
- `return_to` / `recovery_to`が進入元ごとに正しく、eventから注入できないこと
- **binding**: 対応するPRODUCEDなしのVERIFIED、binding不一致のevidence、過去evidenceの再利用、pending中の他semantic eventがすべて拒否されること。partial turn（pending_recordあり）のMachineStateが再開入力として機能すること
- **merge安全性**: `CMD_EXECUTE_MERGE`が#37以外の経路で決して発行されないこと。`MERGING` + cancel / failureが照会を経由し、`EV_MERGE_NOT_EXECUTED_CONFIRMED`でのみ`CANCELLED`になること
- decision flowで両agentのrecordが順番に要求されること（#11→#17→#18→#19等の系列test）

## 8. 設計レビューで確定した判断

1. **Codex起動command**: `CMD_REQUEST_CODEX_REVIEW(purpose)`のtyped purpose方式を採用（`CODE_REVIEW` / `CLARIFICATION` / `DECISION_VERDICT`）。実行基盤はC-09へ集約
2. **CI code failure**: `CHANGES_REQUESTED`へ遷移し、`CMD_INVALIDATE_APPROVALS`で既存承認を失効させる。infra failure / timeoutとは区別を維持
3. **BLOCKED / FAILEDのresume**: 一律fresh reviewは採らない。進入時にregistryが`recovery_to`を設定し、`EV_RESUME_VALIDATED`で復帰する。head変更・checkpoint不整合等で安全を証明できない場合のみ`EV_RESUME_FALLBACK_REQUIRED`でfresh reviewへfallback
