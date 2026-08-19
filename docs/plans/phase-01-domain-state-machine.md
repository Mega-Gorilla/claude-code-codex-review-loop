<!-- SPDX-License-Identifier: Apache-2.0 -->

# Phase 1計画: C-01 domain state machine

| Field | Value |
| --- | --- |
| Status | **Accepted**（本計画PRのユーザー承認とmergeにより確定） |
| 正本関係 | [implementation plan](implementation-plan.md)のC-01節の詳細設計。target behaviorは[target experience](target-experience.md)に従い、本書は変更しない |
| 対応Issue | #6（本書は計画。Issue #6のcloseはC-01実装PRで行う） |
| 受入条件 | AC-C01-01〜08 |

## 1. 目的と正本の役割分担

target experienceの「State model」節が定義する17 stateと、「User intervention」「Failure, cancellation, and resume experience」節の挙動を、実装可能な粒度で確定する。ユーザー向け簡略図が省略している遷移（失敗系からのresume、cancel可否、GitHub投稿・確認失敗、preflight失敗）をすべて定義する。

正本の役割は次のとおり分担し、二重の正本を作らない。

| 資料 | 役割 |
| --- | --- |
| 本計画文書 | **normativeな期待挙動**。実装が満たすべき遷移・規則・不変条件 |
| 実装のcode registry | **実行可能な単一source**。全ruleをdataとして保持する |
| 生成された遷移表・遷移図 | code registryから導出し、本書の表とのsnapshot照合をtestで行う（AC-C01-01） |

**registryの一意性不変条件**: guardは自由なpredicateではなく、**有限のtyped discriminator**（Section 2の`awaiting`値、`pending_record`のkind / binding一致、`return_to` / `recovery_to`の有無）に限定する。到達可能なMachineState付随値の各組合せ × 各eventに対し、一致するruleは**0件または1件**である。共通規則（cancel / failure等）はregistry内で個別ruleへ展開され、重複・overlapはdiscriminator全値の展開により機械的に検査してfailさせる。優先順位による解決は行わない（AC-C01-08）。

## 2. MachineState

可視の17 stateとは別に、immutableな`MachineState`を導入する。

```text
MachineState（frozen）
  state: State                       # 可視の17 stateのいずれか
  return_to: State | None            # AWAITING_TOOL_PERMISSIONからの復帰先
  recovery_to: State | None          # BLOCKED / FAILEDからの安全な再開地点
  awaiting: Awaiting | None          # 発行済みcommandに対応する「次に受理してよい応答」の期待値
  pending_record: PendingRecord | None  # 永続化の確認待ちrecord

PendingRecord（frozen）
  kind: RecordKind
  binding: OpaqueBinding             # logical turnへのopaqueなbinding値
  source_state: State                # PRODUCEDが発生したstate

RecordEvidence（frozen）
  kind: RecordKind
  binding: OpaqueBinding             # 外部recordではcomment参照から導出（Section 3.3）
  ref: RecordRef                     # 検証済みrecordへのopaque参照
```

- `return_to` / `recovery_to` / `awaiting` / `pending_record`は**registry内の遷移ruleだけが設定**し、eventから注入できない
- `binding`はopaqueな値であり、C-01は**等価比較のみ**を行い意味を解釈しない。値の採番はC-08、真正性の検証はC-06の責務
- 数量判定（round数・clarification turn数等）はC-01の外で行い、判定結果を専用eventとして入力する

### 2.1 Awaiting（有限のtyped discriminator）

`awaiting`は「どのcommandを発行済みで、次にどの応答だけを受理するか」を表す。値は次の有限集合に限る。

| Awaiting値 | 設定するcommand | 受理する応答 |
| --- | --- | --- |
| `CODEX(CODE_REVIEW)` | `CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)` | `REVIEW_RESULT` / `PERMISSION_BLOCK`のPRODUCED |
| `CODEX(CLARIFICATION)` | `CMD_REQUEST_CODEX_REVIEW(CLARIFICATION)` | `CLARIFICATION_ANSWER`のPRODUCED |
| `CODEX(DECISION_VERDICT)` | `CMD_REQUEST_CODEX_REVIEW(DECISION_VERDICT)` | `DECISION_VERDICT`のPRODUCED |
| `HOST(APPLY_FINDINGS)` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` | `EV_FIX_STARTED`、`FIX_RESULT` / `CLARIFICATION_QUESTION` / `DECISION_REQUEST` / `PERMISSION_BLOCK`のPRODUCED |
| `HOST(DRAFT_DECISION_REQUEST)` | 同名host action | `DECISION_REQUEST`のPRODUCED |
| `HOST(DRAFT_DECISION_BRIEF)` | 同名host action | `DECISION_BRIEF`のPRODUCED |
| `HOST(RECORD_DECISION)` | 同名host action | `DECISION_RECORD`のPRODUCED |
| `HOST(REVISE_DECISION_REQUEST)` | 同名host action | `DECISION_REQUEST`のPRODUCED |
| `HOST(ANSWER_GATE_QUESTION)` | 同名host action | `GATE_ANSWER`のPRODUCED |
| `CI_RESULT` | `CMD_CHECK_CI` | `EV_CI_SUCCEEDED` / `EV_CI_INFRA_FAILURE`、`CI_CODE_FAILURE` / `CI_TIMEOUT`のPRODUCED |
| `REPORT` | `CMD_GENERATE_REPORT` | `FINAL_REPORT`のPRODUCED、`EV_REPORT_FAILED` |
| `MERGE_PRECONDITIONS` | `CMD_VERIFY_MERGE_PRECONDITIONS` | `EV_MERGE_PRECONDITIONS_OK` / `EV_MERGE_PRECONDITION_MISMATCH` / `EV_HEAD_CHANGED_EXTERNALLY` |
| `MERGE_OUTCOME(EXECUTE)` | `CMD_EXECUTE_MERGE` | `EV_MERGE_CONFIRMED` / `EV_MERGE_OUTCOME_UNKNOWN` |
| `MERGE_OUTCOME(CANCEL)` | `CMD_QUERY_MERGE_OUTCOME`（cancel起点） | `EV_MERGE_CONFIRMED` / `EV_MERGE_NOT_EXECUTED_CONFIRMED` / `EV_MERGE_OUTCOME_UNKNOWN` |
| `MERGE_OUTCOME(FAILURE)` | `CMD_QUERY_MERGE_OUTCOME`（failure起点） | 同上 |
| `USER_INPUT(DECISION)` | —（`AWAITING_USER_DECISION`進入ruleが設定） | 外部record `USER_DECISION` |
| `USER_INPUT(GATE)` | —（`READY_FOR_HUMAN_MERGE`進入ruleが設定） | 外部record `GATE_QUESTION` / `GATE_CHANGES` / `MERGE_APPROVAL` |
| `USER_INPUT(PERMISSION)` | —（`AWAITING_TOOL_PERMISSION`進入ruleが設定） | `EV_PERMISSION_RESUME_VALIDATED` |

**lifecycle規則**（AC-C01-08の核）:

1. 応答を要するcommandを発行する遷移ruleは、対応する`awaiting`値を**同一ruleで設定**する
2. 応答event（結果event、PRODUCED）は、`awaiting`が当該応答を受理する値である場合**のみ**受理され、受理時に`awaiting`を**消費**（`None`化）または次の期待値へ**更新**する（例: `EV_MERGE_PRECONDITIONS_OK`は`MERGE_PRECONDITIONS`を消費し`MERGE_OUTCOME(EXECUTE)`へ更新）
3. `awaiting`不一致の応答、消費済み応答の再入力（例: 2度目の`EV_MERGE_PRECONDITIONS_OK`）、順序を飛ばした応答（例: 実行command発行前の`EV_MERGE_CONFIRMED`、verdict前の`DECISION_BRIEF`）は**構造化errorで拒否**される
4. 例外として`awaiting`に関わらず受理されるのは、`EV_RUN_FAILED`（共通規則）、外部recordの`USER_CANCEL`（Section 3.3）、resume系のみ

## 3. Record体系

canonical recordは生成主体で2系統に分かれ、規約が異なる。

### 3.1 内部record（Controller / agentが生成し、Controllerが投稿する）

次の対で構成される。

1. `EV_*_PRODUCED(kind, binding)`: 発言が生成された。**許可source state（Section 3.2）かつ`awaiting`一致**の場合のみ受理。`awaiting`を消費し、`pending_record = (kind, binding, source_state)`を設定して`CMD_PERSIST_RECORD(kind, binding)`を返す。状態は変えない
2. `EV_*_VERIFIED(evidence)`: 後続層が投稿・read-after-write・record検証を完了した。**`pending_record`とevidenceの`kind`および`binding`が一致する場合だけ**受理され、`pending_record`を消費して状態を進め、次のcommand（と次の`awaiting`）を設定する

この構造により、対応する`PRODUCED`を経ない`VERIFIED`、過去turnのevidence再利用、別turnへの流用はbinding不一致として拒否される（AC-C01-03）。`pending_record`保持中は、対応する`VERIFIED`・`EV_RUN_FAILED`・`USER_CANCEL`以外のsemantic eventを拒否する。投稿後・確認前に中断したpartial turnは、`pending_record`がMachineStateに残ることでcheckpoint（C-07）から同一turnとして再開できる。

`CMD_PERSIST_RECORD`は**冪等**であることをC-05へ要求する: 同一bindingのrecordが既に投稿済みならば再投稿せず、read-after-write確認から再開する（resume時の二重投稿防止）。

### 3.2 内部record kind registry

| RecordKind | PRODUCED許可state | PRODUCED時のawaiting guard | VERIFIED event |
| --- | --- | --- | --- |
| `REVIEW_RESULT` | `RUNNING_REVIEW` | `CODEX(CODE_REVIEW)` | `EV_REVIEW_BLOCKING_VERIFIED` / `EV_REVIEW_APPROVED_VERIFIED` |
| `FIX_RESULT` | `APPLYING_FIXES` | `HOST(APPLY_FINDINGS)` | `EV_FIX_RESULT_VERIFIED` |
| `CLARIFICATION_QUESTION` | `CHANGES_REQUESTED` | `HOST(APPLY_FINDINGS)` | `EV_CLARIFICATION_QUESTION_VERIFIED` |
| `CLARIFICATION_ANSWER` | `CLARIFYING_REVIEW` | `CODEX(CLARIFICATION)` | `EV_CLARIFICATION_{CONFIRMED,REVISED,WITHDRAWN,ESCALATED}_VERIFIED` |
| `DECISION_REQUEST` | `APPLYING_FIXES` / `REVIEWING_DECISION_REQUEST` | `HOST(APPLY_FINDINGS)` / `HOST(DRAFT_DECISION_REQUEST)` / `HOST(REVISE_DECISION_REQUEST)` | `EV_DECISION_REQUEST_VERIFIED` |
| `DECISION_VERDICT` | `REVIEWING_DECISION_REQUEST` | `CODEX(DECISION_VERDICT)` | `EV_VERDICT_{ASK_USER,PROCEED,RESUBMIT}_VERIFIED` |
| `DECISION_BRIEF` | `REVIEWING_DECISION_REQUEST` | `HOST(DRAFT_DECISION_BRIEF)` | `EV_DECISION_BRIEF_VERIFIED` |
| `DECISION_RECORD` | `REVIEWING_DECISION_REQUEST` | `HOST(RECORD_DECISION)` | `EV_DECISION_RECORD_VERIFIED` |
| `PERMISSION_BLOCK` | `RUNNING_REVIEW` / `APPLYING_FIXES` | `CODEX(CODE_REVIEW)` / `HOST(APPLY_FINDINGS)` | `EV_TOOL_PERMISSION_BLOCKED` |
| `CI_TIMEOUT` | `WAITING_CI` | `CI_RESULT` | `EV_CI_TIMEOUT_RECORDED` |
| `CI_CODE_FAILURE` | `WAITING_CI` | `CI_RESULT` | `EV_CI_CODE_FAILURE_VERIFIED` |
| `FINAL_REPORT` | `GENERATING_REPORT` | `REPORT` | `EV_REPORT_VERIFIED` |
| `GATE_ANSWER` | `READY_FOR_HUMAN_MERGE` | `HOST(ANSWER_GATE_QUESTION)` | `EV_GATE_ANSWER_VERIFIED` |

### 3.3 外部record（ユーザーがGitHubへ直接記入する）

target experienceの「User intervention」節どおり、ユーザーは判断・gate回答・cancelをGitHub commentへ直接記入でき、Controllerはそれを取得・検証して受理する。これらは**既にGitHubへ永続化済み**であるため、`PRODUCED -> CMD_PERSIST_RECORD -> VERIFIED`を通さない（再投稿すると二重投稿になる）。

| RecordKind | 受理state | awaiting guard | VERIFIED event |
| --- | --- | --- | --- |
| `USER_DECISION` | `AWAITING_USER_DECISION` | `USER_INPUT(DECISION)` | `EV_USER_DECISION_VERIFIED` |
| `GATE_QUESTION` | `READY_FOR_HUMAN_MERGE` | `USER_INPUT(GATE)` | `EV_GATE_QUESTION_VERIFIED` |
| `GATE_CHANGES` | `READY_FOR_HUMAN_MERGE` | `USER_INPUT(GATE)` | `EV_GATE_CHANGES_VERIFIED` |
| `MERGE_APPROVAL` | `READY_FOR_HUMAN_MERGE` | `USER_INPUT(GATE)` | `EV_MERGE_APPROVAL_VERIFIED` |
| `USER_CANCEL` | terminal以外の全state | **不問**（awaiting / pending_recordを破棄して受理） | `EV_USER_CANCEL_VERIFIED` |

処理経路: C-05が既存commentを**観測**（取得のみ、投稿しない） -> C-06がcomment ID・body hash・actor（GitHub login allowlistとの完全一致、D-031、fail closed）・対象headを検証してtyped external evidenceを生成 -> C-01は`awaiting`と受理stateの一致でのみ受理する。外部recordのbindingはC-06がcomment参照から導出し、**同一commentの再利用（消費済みcomment IDの再提示）はC-06 / C-07が拒否**する。C-01はevidenceの構造のみを見る。

**cancelの2系統**: 通常の対話cancelは外部record `USER_CANCEL`としてcanonical記録の検証後に遷移する（`MERGING`では照会経由。Section 5）。**Ctrl+C等の緊急停止はC-01のeventではない** — C-08がrunを停止し、C-07が現在のMachineState（`pending_record` / `awaiting`を含む）をそのままcheckpointする。再開は通常のresume規約に従う。

### 3.4 record以外のevent

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
| `EV_RUN_FAILED` | bounded retry後の失敗（投稿・確認失敗を含む） | 各層 |
| `EV_RESUME_VALIDATED` | resume preflightと状態再構築の成功 | C-07 |
| `EV_RESUME_FALLBACK_REQUIRED` | head変更・checkpoint不整合等で`recovery_to`を安全に証明できない | C-07 |
| `EV_RESUME_SAME_HEAD_VALIDATED` | merge失敗後、同一head・全条件有効の再確認 | C-07 / C-13 |

### 3.5 Command一覧

| Command | 意味 | 実行component |
| --- | --- | --- |
| `CMD_PERSIST_RECORD(kind, binding)` | canonical recordの投稿と検証（冪等。既投稿なら確認のみ） | C-05 / C-06 |
| `CMD_REQUEST_CODEX_REVIEW(purpose)` | fresh reviewerの起動。`purpose ∈ {CODE_REVIEW, CLARIFICATION, DECISION_VERDICT}` | C-09 |
| `CMD_REQUEST_HOST_ACTION(kind)` | active hostへの作業依頼（`APPLY_FINDINGS` / `DRAFT_DECISION_REQUEST` / `DRAFT_DECISION_BRIEF` / `RECORD_DECISION` / `REVISE_DECISION_REQUEST` / `ANSWER_GATE_QUESTION`等） | C-08 |
| `CMD_CHECK_CI` | 対象headのCI確認 | C-12 |
| `CMD_GENERATE_REPORT` | final reporterの起動 | C-12 |
| `CMD_VERIFY_MERGE_PRECONDITIONS` | merge直前の全条件再検証 | C-13 |
| `CMD_EXECUTE_MERGE` | **`awaiting = MERGE_PRECONDITIONS`の消費を伴う#38でのみ発行される**merge実行 | C-13 |
| `CMD_QUERY_MERGE_OUTCOME` | merge結果のGitHub照会 | C-13 |
| `CMD_INVALIDATE_APPROVALS` | review / merge承認の失効 | C-07 |

commandは記述のみであり、C-01は実行しない。1遷移が返すcommand列の順序は決定論的とする。**command列に条件分岐の意味は無い** — 条件で結果が分かれる処理は、必ず結果eventを受けて次のruleが判断する。

## 4. 分類とresume registry

| 分類 | State |
| --- | --- |
| terminal | `MERGED`、`CANCELLED`（全event拒否） |
| resumable | `WAITING_CI`、`AWAITING_USER_DECISION`、`AWAITING_TOOL_PERMISSION`、`READY_FOR_HUMAN_MERGE`、`BLOCKED`、`FAILED`、`REPORT_FAILED`、`MERGE_FAILED` |
| active | 残りの7 state |

### 4.1 resume時のaction優先順位

resume actionは可視stateだけでは決めない。`EV_RESUME_VALIDATED`受理時のcommandは次の優先順位で決まる。

1. **`pending_record`がある**: 復帰先は`pending_record.source_state`とし、同一bindingの`CMD_PERSIST_RECORD(kind, binding)`を再発行する（冪等なので、投稿済みなら確認のみが走る）。次agentは起動しない
2. **`awaiting`がある**: 復帰先へ戻り、`awaiting`に対応するcommandを再発行する（`CODEX(p) -> CMD_REQUEST_CODEX_REVIEW(p)`（reviewerはfresh起動なので再発行safe）、`HOST(k) -> CMD_REQUEST_HOST_ACTION(k)`、`CI_RESULT -> CMD_CHECK_CI`、`REPORT -> CMD_GENERATE_REPORT`、`MERGE_PRECONDITIONS -> CMD_VERIFY_MERGE_PRECONDITIONS`、`MERGE_OUTCOME(*) -> CMD_QUERY_MERGE_OUTCOME`、`USER_INPUT(*) -> なし`（入力待ち再掲のみ））
3. **どちらも無い**: `recovery_to`の駆動command（`RUNNING_REVIEW -> CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`等、復帰先ごとにregistryが定義）を発行する

### 4.2 resume registry

| From | Event | Guard | To | Commands |
| --- | --- | --- | --- | --- |
| `FAILED` / `BLOCKED` | `EV_RESUME_VALIDATED` | `recovery_to`あり | Section 4.1の優先順位（1は`source_state`、2 / 3は`recovery_to`） | Section 4.1の優先順位 |
| `FAILED` / `BLOCKED` | `EV_RESUME_FALLBACK_REQUIRED` | — | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`（`pending_record` / `awaiting`は破棄） |
| `REPORT_FAILED` | `EV_REPORTER_RETRY_REQUESTED` | — | `GENERATING_REPORT` | `CMD_GENERATE_REPORT` |
| `MERGE_FAILED` | `EV_RESUME_SAME_HEAD_VALIDATED` | — | `READY_FOR_HUMAN_MERGE` | —（`awaiting = USER_INPUT(GATE)`。新しい明示承認を待つ） |
| `MERGE_FAILED` | `EV_HEAD_CHANGED_EXTERNALLY` | — | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)` |
| `WAITING_CI` | `EV_CI_RESUME_REQUESTED` | — | `WAITING_CI` | `CMD_CHECK_CI`（`awaiting := CI_RESULT`） |
| `AWAITING_TOOL_PERMISSION` | `EV_PERMISSION_RESUME_VALIDATED` | `return_to`あり | `return_to` | Section 4.1の優先順位に従う（pending / awaitingが無ければ復帰先の駆動command） |
| `AWAITING_USER_DECISION` | 外部record（`EV_USER_DECISION_VERIFIED`） | `USER_INPUT(DECISION)` | Section 5 | 通常eventがresumeを兼ねる |
| `READY_FOR_HUMAN_MERGE` | 外部record（gate系） | `USER_INPUT(GATE)` | Section 5 | 通常eventがresumeを兼ねる |

**自己参照の禁止**: `BLOCKED` / `FAILED`滞在中の`EV_RUN_FAILED`（resume試行の失敗等）は**同一stateに留まり、既存の`recovery_to` / `pending_record` / `awaiting`を保持**する。`recovery_to`が`BLOCKED` / `FAILED`自身を指すことはregistry上あり得ない（進入ruleは常に進入元のactive / waiting stateを設定する）。

`BLOCKED` / `FAILED`への遷移ruleは、進入元に応じた`recovery_to`を設定し、**`pending_record`と`awaiting`を変更せずに引き継ぐ**。同一head・同一原因での無条件fresh reviewはno-progress loopを再開させ得るため採らず、`recovery_to`を安全に証明できない場合のみfallbackとしてfresh reviewへ入る。

## 5. 完全遷移表

registryの期待挙動。`VERIFIED`（内部record）のGuard列には`pending_record`一致（kind + binding）が暗黙に含まれる。内部recordの`PRODUCED`はSection 3.2の許可state + awaiting guardでのみ受理され、状態を変えず`awaiting`を消費して`pending_record`設定と`CMD_PERSIST_RECORD`発行を行う（表からは省略）。「awaiting := X」はそのruleが設定する新しい期待値。

| # | From | Event | Guard | To | Commands / awaiting更新 |
| --- | --- | --- | --- | --- | --- |
| 1 | （`initialize` API） | `EV_PREFLIGHT_OK` | — | `RUNNING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| 2 | （`initialize` API） | `EV_PREFLIGHT_NG` | — | `FAILED` | —（`recovery_to`なし。resumeはfallback経路のみ） |
| 3 | `RUNNING_REVIEW` | `EV_REVIEW_BLOCKING_VERIFIED` | evidence一致 | `CHANGES_REQUESTED` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 4 | `RUNNING_REVIEW` | `EV_REVIEW_APPROVED_VERIFIED` | evidence一致 | `WAITING_CI` | `CMD_CHECK_CI`; awaiting := `CI_RESULT` |
| 5 | `RUNNING_REVIEW` | `EV_TOOL_PERMISSION_BLOCKED` | evidence一致 | `AWAITING_TOOL_PERMISSION` | —（`return_to := RUNNING_REVIEW`; awaiting := `USER_INPUT(PERMISSION)`） |
| 6 | `RUNNING_REVIEW` | `EV_ROUND_LIMIT_REACHED` / `EV_NO_PROGRESS` | — | `BLOCKED` | —（`recovery_to := RUNNING_REVIEW`; pending / awaiting引継） |
| 7 | `CHANGES_REQUESTED` | `EV_FIX_STARTED` | awaiting = `HOST(APPLY_FINDINGS)` | `APPLYING_FIXES` | —（awaiting維持） |
| 8 | `CHANGES_REQUESTED` | `EV_CLARIFICATION_QUESTION_VERIFIED` | evidence一致 | `CLARIFYING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CLARIFICATION)`; awaiting := `CODEX(CLARIFICATION)` |
| 9 | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_CONFIRMED_VERIFIED` / `EV_CLARIFICATION_REVISED_VERIFIED` | evidence一致 | `CHANGES_REQUESTED` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 10 | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_WITHDRAWN_VERIFIED` | evidence一致 | `RUNNING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| 11 | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_ESCALATED_VERIFIED` | evidence一致 | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_HOST_ACTION(DRAFT_DECISION_REQUEST)`; awaiting := `HOST(DRAFT_DECISION_REQUEST)` |
| 12 | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_STALLED` | — | `BLOCKED` | —（`recovery_to := CHANGES_REQUESTED`; pending / awaiting引継） |
| 13 | `APPLYING_FIXES` | `EV_FIX_RESULT_VERIFIED` | evidence一致 | `RUNNING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| 14 | `APPLYING_FIXES` | `EV_DECISION_REQUEST_VERIFIED` | evidence一致 | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_CODEX_REVIEW(DECISION_VERDICT)`; awaiting := `CODEX(DECISION_VERDICT)` |
| 15 | `APPLYING_FIXES` | `EV_TOOL_PERMISSION_BLOCKED` | evidence一致 | `AWAITING_TOOL_PERMISSION` | —（`return_to := APPLYING_FIXES`; awaiting := `USER_INPUT(PERMISSION)`） |
| 16 | `APPLYING_FIXES` | `EV_NO_PROGRESS` | — | `BLOCKED` | —（`recovery_to := APPLYING_FIXES`; pending / awaiting引継） |
| 17 | `REVIEWING_DECISION_REQUEST` | `EV_DECISION_REQUEST_VERIFIED` | evidence一致（draft / revised） | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_CODEX_REVIEW(DECISION_VERDICT)`; awaiting := `CODEX(DECISION_VERDICT)` |
| 18 | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_ASK_USER_VERIFIED` | evidence一致 | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_HOST_ACTION(DRAFT_DECISION_BRIEF)`; awaiting := `HOST(DRAFT_DECISION_BRIEF)` |
| 19 | `REVIEWING_DECISION_REQUEST` | `EV_DECISION_BRIEF_VERIFIED` | evidence一致 | `AWAITING_USER_DECISION` | —; awaiting := `USER_INPUT(DECISION)` |
| 20 | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_PROCEED_VERIFIED` | evidence一致 | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_HOST_ACTION(RECORD_DECISION)`; awaiting := `HOST(RECORD_DECISION)` |
| 21 | `REVIEWING_DECISION_REQUEST` | `EV_DECISION_RECORD_VERIFIED` | evidence一致 | `APPLYING_FIXES` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 22 | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_RESUBMIT_VERIFIED` | evidence一致 | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_HOST_ACTION(REVISE_DECISION_REQUEST)`; awaiting := `HOST(REVISE_DECISION_REQUEST)` |
| 23 | `REVIEWING_DECISION_REQUEST` | `EV_DECISION_UNRESOLVED` | — | `BLOCKED` | —（`recovery_to := REVIEWING_DECISION_REQUEST`; pending / awaiting引継） |
| 24 | `AWAITING_USER_DECISION` | `EV_USER_DECISION_VERIFIED` | awaiting = `USER_INPUT(DECISION)`、external evidence | `APPLYING_FIXES` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 25 | `AWAITING_TOOL_PERMISSION` | `EV_PERMISSION_RESUME_VALIDATED` | `return_to`あり | `return_to` | Section 4.1の優先順位 |
| 26 | `WAITING_CI` | `EV_CI_SUCCEEDED` | awaiting = `CI_RESULT` | `GENERATING_REPORT` | `CMD_GENERATE_REPORT`; awaiting := `REPORT` |
| 27 | `WAITING_CI` | `EV_CI_CODE_FAILURE_VERIFIED` | evidence一致 | `CHANGES_REQUESTED` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 28 | `WAITING_CI` | `EV_CI_INFRA_FAILURE` | awaiting = `CI_RESULT` | `WAITING_CI` | `CMD_CHECK_CI`（awaiting維持。bounded retryの判定は外部） |
| 29 | `WAITING_CI` | `EV_CI_TIMEOUT_RECORDED` | evidence一致 | `WAITING_CI` | —; awaiting := なし（runはcheckpointで終了。resumeは`EV_CI_RESUME_REQUESTED`） |
| 30 | `WAITING_CI` | `EV_HEAD_CHANGED_EXTERNALLY` | `pending_record`なし | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| 31 | `GENERATING_REPORT` | `EV_REPORT_VERIFIED` | evidence一致 | `READY_FOR_HUMAN_MERGE` | —; awaiting := `USER_INPUT(GATE)` |
| 32 | `GENERATING_REPORT` | `EV_REPORT_FAILED` | awaiting = `REPORT` | `REPORT_FAILED` | — |
| 33 | `READY_FOR_HUMAN_MERGE` | `EV_GATE_QUESTION_VERIFIED` | awaiting = `USER_INPUT(GATE)`、external evidence | `READY_FOR_HUMAN_MERGE` | `CMD_REQUEST_HOST_ACTION(ANSWER_GATE_QUESTION)`; awaiting := `HOST(ANSWER_GATE_QUESTION)` |
| 34 | `READY_FOR_HUMAN_MERGE` | `EV_GATE_ANSWER_VERIFIED` | evidence一致 | `READY_FOR_HUMAN_MERGE` | —; awaiting := `USER_INPUT(GATE)`（質問と回答をPRへ記録しgate維持） |
| 35 | `READY_FOR_HUMAN_MERGE` | `EV_GATE_CHANGES_VERIFIED` | awaiting = `USER_INPUT(GATE)`、external evidence | `CHANGES_REQUESTED` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 36 | `READY_FOR_HUMAN_MERGE` | `EV_MERGE_APPROVAL_VERIFIED` | awaiting = `USER_INPUT(GATE)`、external evidence | `MERGING` | **`CMD_VERIFY_MERGE_PRECONDITIONS`のみ**; awaiting := `MERGE_PRECONDITIONS` |
| 37 | `READY_FOR_HUMAN_MERGE` | `EV_HEAD_CHANGED_EXTERNALLY` | `pending_record`なし | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| 38 | `MERGING` | `EV_MERGE_PRECONDITIONS_OK` | awaiting = `MERGE_PRECONDITIONS` | `MERGING` | **`CMD_EXECUTE_MERGE`（この経路でのみ発行）**; awaiting := `MERGE_OUTCOME(EXECUTE)`（**再入力はguard不一致で構造化error**） |
| 39 | `MERGING` | `EV_MERGE_PRECONDITION_MISMATCH` | awaiting = `MERGE_PRECONDITIONS` | `MERGE_FAILED` | — |
| 40 | `MERGING` | `EV_HEAD_CHANGED_EXTERNALLY` | awaiting = `MERGE_PRECONDITIONS` | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| 41 | `MERGING` | `EV_MERGE_CONFIRMED` | awaiting = `MERGE_OUTCOME(*)` | `MERGED` | —（awaiting消費） |
| 42 | `MERGING` | `EV_MERGE_NOT_EXECUTED_CONFIRMED` | awaiting = `MERGE_OUTCOME(CANCEL)` | `CANCELLED` | — |
| 43 | `MERGING` | `EV_MERGE_NOT_EXECUTED_CONFIRMED` | awaiting = `MERGE_OUTCOME(FAILURE)` | `MERGE_FAILED` | —（`EV_RESUME_SAME_HEAD_VALIDATED`で復帰可能） |
| 44 | `MERGING` | `EV_MERGE_OUTCOME_UNKNOWN` | awaiting = `MERGE_OUTCOME(*)` | `MERGE_FAILED` | — |
| 45 | `MERGING` | `EV_USER_CANCEL_VERIFIED` | external evidence | `MERGING` | `CMD_QUERY_MERGE_OUTCOME`; awaiting := `MERGE_OUTCOME(CANCEL)`（**即CANCELLEDにしない**。#41 / #42 / #44で解決） |
| 46 | `MERGING` | `EV_RUN_FAILED` | — | `MERGING` | `CMD_QUERY_MERGE_OUTCOME`; awaiting := `MERGE_OUTCOME(FAILURE)`（merge成否不明を通常失敗へ落とさない。#41 / #43 / #44で解決） |

**共通規則**（registry内で個別ruleへ展開され、一意性checkの対象になる）:

- `EV_USER_CANCEL_VERIFIED`: terminalと`MERGING`（#45）を除く全stateから`CANCELLED`へ。外部record（Section 3.3）であり、canonical記録の検証後にのみ発火する。`awaiting` / `pending_record`は破棄する
- `EV_RUN_FAILED`: terminal・`MERGING`（#46）・`BLOCKED`・`FAILED`を除く全stateから`FAILED`へ（`recovery_to` := 進入元state。`pending_record` / `awaiting`は変更せず引き継ぐ）
- `BLOCKED` / `FAILED` + `EV_RUN_FAILED`: 同一stateに留まり、既存の`recovery_to` / `pending_record` / `awaiting`を保持する（自己参照上書きの禁止）
- 緊急停止（Ctrl+C等）はC-01のeventではない（Section 3.3）
- terminal（`MERGED` / `CANCELLED`）は全eventを構造化errorで拒否
- 上記いずれにも一致しない`(state, event, guard値)`は未定義遷移として構造化errorで拒否（AC-C01-02）

**decision flowのGitHub会話順序**（#11 / #14 / #17〜#22 / #24）: target experienceの合意どおり、(a) Claude draft投稿確認、(b) Codex verdict投稿確認、(c) Claudeの最終brief / decision record / revised draftの投稿確認、(d) 次のCodexまたはユーザー、の順にGitHub上へ両agentの発言が個別に現れる。`awaiting`のlifecycle規則により、verdict確認前のbrief / decision record投稿は構造化errorで拒否される。

## 6. C-01のscope境界（Phase 1）

**実装する**: `domain/states.py`、`domain/events.py`、`domain/commands.py`、`domain/machine.py`（registry・`initialize(preflight_event)`・`transition(machine_state, event)`）、最小のvalue object（`MachineState` / `PendingRecord` / `RecordEvidence` / `OpaqueBinding` / `Awaiting`）。

**実装しない（out of scope）**: GitHub APIアクセス・comment観測（C-05）/ actor認証・record chain・外部evidence検証・comment再利用拒否（C-06）/ checkpoint永続化・状態再構築・消費済みrecord管理（C-07）/ subprocess起動（C-03 / C-09）/ advance-submit engine（C-08）/ finding ledger本実装（Phase 10)とid・binding採番（C-08）/ CLI / Skill / wrapper。空moduleも作らない。

## 7. Test計画

- registryをdataとして全state × event × guard discriminator値の組合せをtable-drivenで検査（未定義は構造化error）
- **一意性とoverlap**: guard discriminatorの全値を展開し、到達可能なMachineState付随値の各組合せ × 各eventについて一致rule数が常に0または1であることを検査。2件以上はfail
- 17 stateの到達可能性、到達不能stateの検出、遷移表・遷移図のsnapshot照合（本書Section 5と）
- 純粋性: 同一入力の再適用で同一結果、入力非変更、I/O・時刻・乱数・環境変数への非依存、command列順序の決定性
- terminalからの全event拒否。resumableはresume registryのeventのみ受理
- `return_to` / `recovery_to` / `awaiting` / `pending_record`が遷移ruleだけで設定され、eventから注入できないこと
- **binding**: 対応するPRODUCEDなしのVERIFIED、binding不一致のevidence、過去evidenceの再利用、pending中の他semantic eventがすべて拒否されること。partial turn（pending_recordあり）のMachineStateが再開入力として機能すること
- **awaiting順序**（negative系列を中心に）: `CMD_EXECUTE_MERGE`が#38以外の経路で決して発行されないこと。**実行command発行前の`EV_MERGE_CONFIRMED`の拒否**、**`EV_MERGE_PRECONDITIONS_OK`の重複入力の拒否**、**verdict確認前の`DECISION_BRIEF` / `DECISION_RECORD`のPRODUCED拒否**、awaiting不一致の応答event拒否、消費済みawaitingへの再応答拒否
- **merge安全性**: `MERGING`のcancel / failureが照会を経由し、`EV_MERGE_NOT_EXECUTED_CONFIRMED`かつcancel起点でのみ`CANCELLED`になること
- **resume優先順位**: pending_recordありのresumeが`CMD_PERSIST_RECORD`再発行のみを返すこと（次agentを起動しない）、awaitingありのresumeが対応する再発行commandを返すこと、`BLOCKED` / `FAILED`中の`EV_RUN_FAILED`が既存recovery情報を上書きしないこと
- **外部record**: `USER_DECISION` / gate系 / `USER_CANCEL`が`CMD_PERSIST_RECORD`を発行しないこと、awaiting不一致（例: verdict待ち中のgate approval）が拒否されること
- decision flowで両agentのrecordが順番に要求されること（#11→#17→#18→#19等の系列test）

## 8. 設計レビューで確定した判断

1. **Codex起動command**（round 1）: `CMD_REQUEST_CODEX_REVIEW(purpose)`のtyped purpose方式を採用。実行基盤はC-09へ集約
2. **CI code failure**（round 1）: `CHANGES_REQUESTED`へ遷移し、`CMD_INVALIDATE_APPROVALS`で既存承認を失効させる
3. **BLOCKED / FAILEDのresume**（round 1）: 一律fresh reviewは採らない。`recovery_to`と resume優先順位（Section 4.1）で復帰し、安全を証明できない場合のみfallback
4. **awaiting lifecycle**（round 2）: 応答を要する全commandが`awaiting`を設定し、応答eventはguard一致でのみ受理・消費される。command -> expected resultがMachineState上で一意になる
5. **内部 / 外部recordの分離**（round 2）: ユーザーがGitHubへ直接記入したrecordは再投稿せず、C-06検証済みexternal evidenceとして`awaiting = USER_INPUT(*)` guardで直接受理する
6. **cancelの2系統**（round 2）: 対話cancelは外部record `USER_CANCEL`のcanonical検証後に遷移。緊急停止はC-01外（checkpointのみ）
