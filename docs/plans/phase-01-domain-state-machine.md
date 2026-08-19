<!-- SPDX-License-Identifier: Apache-2.0 -->

# Phase 1計画: C-01 domain state machine

| Field | Value |
| --- | --- |
| Status | **Accepted**（本計画PRのユーザー承認とmergeにより確定） |
| 正本関係 | [implementation plan](implementation-plan.md)のC-01節の詳細設計。target behaviorは[target experience](target-experience.md)に従い、本書は変更しない |
| 対応Issue | #6（本書は計画。Issue #6のcloseはC-01実装PRで行う） |
| 受入条件 | AC-C01-01〜11 |

## 1. 目的と正本の役割分担

target experienceの「State model」節が定義する17 stateと、「User intervention」「Failure, cancellation, and resume experience」節の挙動を、実装可能な粒度で確定する。ユーザー向け簡略図が省略している遷移（失敗系からのresume、cancel可否、GitHub投稿・確認失敗、preflight失敗）をすべて定義する。

正本の役割は次のとおり分担し、二重の正本を作らない。

| 資料 | 役割 |
| --- | --- |
| 本計画文書 | **normativeな期待挙動**。実装が満たすべき遷移・規則・不変条件 |
| 実装のcode registry | **実行可能な単一source**。全ruleをdataとして保持する |
| 生成された遷移表・遷移図 | code registryから導出し、本書の表とのsnapshot照合をtestで行う（AC-C01-01） |

**registryの一意性不変条件**: guardは自由なpredicateではなく、**有限のtyped discriminator**（Section 2の`awaiting`値、`pending_record`のkind / binding一致、`progress`値、`blocked_continuation`のreason、`cancelling` / `return_to` / `recovery_to`の有無）に限定する。到達可能なMachineState付随値の各組合せ × 各eventに対し、一致するruleは**0件または1件**である。共通規則（cancel / failure / progress等）はregistry内で個別ruleへ展開され、重複・overlapはdiscriminator全値の展開により機械的に検査してfailさせる。優先順位による解決は行わない（AC-C01-08）。

## 2. MachineState

可視の17 stateとは別に、immutableな`MachineState`を導入する。

```text
MachineState（frozen）
  state: State                       # 可視の17 stateのいずれか
  return_to: State | None            # AWAITING_TOOL_PERMISSIONからの復帰先
  recovery_to: State | None          # EV_RUN_FAILEDで入ったFAILEDの安全な再開地点
  blocked_continuation: BlockedContinuation | None  # 上限到達でBLOCKEDへ入ったときの、本来の継続と停止理由
  cancelling: CancelAttempt | None   # 進行中のcancel attempt（停止・checkpoint完了待ち）
  awaiting: Awaiting | None          # 発行済みcommandに対応する「次に受理してよい応答」の期待値
  pending_record: PendingRecord | None  # 永続化の確認待ちrecord

BlockedContinuation（frozen）
  resume_state: State                # 継続処理を行うstate（CONTINUE時の遷移先）
  commands: tuple[Command, ...]      # CONTINUE時に発行するはずだったcommand列（付随actionを含む）
  awaiting: Awaiting | None          # CONTINUE時に設定するはずだったawaiting
  reason: LIMIT_REACHED | NO_PROGRESS  # 停止理由
  budget: Budget                     # 対象budget（REVIEW_ROUND / CLARIFICATION_TURN）
  counter_snapshot: OpaqueSnapshot   # 停止時のcounter / limit（C-10 / C-11の監査値。C-01は解釈しない）
  fingerprint: OpaqueFingerprint     # 対象topic / loopの識別（等価比較のみ）

CancelAttempt（frozen）
  binding: OpaqueBinding             # cancel attemptへのopaqueなbinding値

PendingRecord（frozen）
  kind: RecordKind
  binding: OpaqueBinding             # logical turnへのopaqueなbinding値
  source_state: State                # PRODUCEDが発生したstate

RecordEvidence（frozen）
  kind: RecordKind
  binding: OpaqueBinding
  ref: RecordRef                     # 検証済みrecordへのopaque参照
```

- `return_to` / `recovery_to` / `blocked_continuation` / `cancelling` / `awaiting` / `pending_record`は**registry内の遷移ruleだけが設定**し、eventから注入できない。`BlockedContinuation`のcommand列・awaitingはregistryのCONTINUE行から導出される有限集合であり、任意のcommand列を持ち込めない（`counter_snapshot` / `fingerprint` / bindingはevent payload由来の監査値で、C-01は等価比較のみを行う）
- `binding`はopaqueな値であり、C-01は**等価比較のみ**を行い意味を解釈しない。**採番の所有者は経路で分かれる**: 内部recordはC-08、外部evidenceはC-06がcomment参照から導出、cancel attemptはC-08。いずれも真正性の検証はC-06 / C-07の責務
- 数量判定（round数・clarification turn数・膠着）はC-01の外で行い、判定結果を`progress` discriminator（Section 3.4）として入力する。counterの管理・更新はC-10 / C-11が行い、C-01は算術を行わない

### 2.1 Awaiting（有限のtyped discriminator、19値）

`awaiting`は「どのcommandを発行済みで、次にどの応答だけを受理するか」を表す。値は次の19値に限る。

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
| `HALT_FOR_CANCEL` | `CMD_HALT_RUN` | `EV_CANCELLATION_COMPLETED`（`cancelling`とのbinding一致） |
| `USER_INPUT(DECISION)` | —（`AWAITING_USER_DECISION`進入ruleが設定） | user-input record `USER_DECISION`（両経路。Section 3.3） |
| `USER_INPUT(GATE)` | —（`READY_FOR_HUMAN_MERGE`進入ruleが設定） | user-input record `GATE_QUESTION` / `GATE_CHANGES` / `MERGE_APPROVAL`（両経路） |
| `USER_INPUT(PERMISSION)` | —（`AWAITING_TOOL_PERMISSION`進入ruleが設定） | `EV_PERMISSION_RESUME_VALIDATED` |

**lifecycle規則**（AC-C01-08の核）:

1. 応答を要するcommandを発行する遷移ruleは、対応する`awaiting`値を**同一ruleで設定**する
2. 応答event（結果event、PRODUCED）は、`awaiting`が当該応答を受理する値である場合**のみ**受理され、受理時に`awaiting`を**消費**（`None`化）または次の期待値へ**更新**する（例: `EV_MERGE_PRECONDITIONS_OK`は`MERGE_PRECONDITIONS`を消費し`MERGE_OUTCOME(EXECUTE)`へ更新）
3. `awaiting`不一致の応答、消費済み応答の再入力（例: 2度目の`EV_MERGE_PRECONDITIONS_OK`）、順序を飛ばした応答（例: 実行command発行前の`EV_MERGE_CONFIRMED`、verdict前の`DECISION_BRIEF`）は**構造化errorで拒否**される
4. 例外として`awaiting`に関わらず受理されるのは、`EV_RUN_FAILED`（共通規則）、`USER_CANCEL`（Section 3.3。PRODUCEDは`awaiting`を消費せず維持する）、`EV_CANCELLATION_COMPLETED`（Section 3.5のbinding guardに従う）、resume系のみ
5. **`cancelling`保持中**は、binding一致の`EV_CANCELLATION_COMPLETED`と`EV_RUN_FAILED`以外の全semantic eventを拒否する（cancel開始後にrunを再進行させない）

## 3. Record体系

canonical recordは生成主体で2系統に分かれ、規約が異なる。

### 3.1 内部record（Controller / agentが生成し、Controllerが投稿する）

次の対で構成される。

1. `EV_*_PRODUCED(kind, binding)`: 発言が生成された。**許可source state（Section 3.2）かつ`awaiting`一致**の場合のみ受理。`awaiting`を消費し、`pending_record = (kind, binding, source_state)`を設定して`CMD_PERSIST_RECORD(kind, binding)`を返す。状態は変えない
2. `EV_*_VERIFIED(evidence)`: 後続層が投稿・read-after-write・record検証を完了した。**`pending_record`とevidenceの`kind`および`binding`が一致する場合だけ**受理され、`pending_record`を消費して状態を進め、次のcommand（と次の`awaiting`）を設定する

この構造により、対応する`PRODUCED`を経ない`VERIFIED`、過去turnのevidence再利用、別turnへの流用はbinding不一致として拒否される（AC-C01-03）。`pending_record`保持中は、対応する`VERIFIED`・`EV_RUN_FAILED`・外部経路の`EV_USER_CANCEL_VERIFIED`以外のsemantic eventを拒否する（内部経路の`USER_CANCEL` PRODUCEDはpending slotが空くまでC-08が保留する）。投稿後・確認前に中断したpartial turnは、`pending_record`がMachineStateに残ることでcheckpoint（C-07)から同一turnとして再開できる。

`CMD_PERSIST_RECORD`は**冪等**であることをC-05へ要求する: 同一bindingのrecordが既に投稿済みならば再投稿せず、read-after-write確認から再開する（resume時の二重投稿防止）。

### 3.2 内部record kind registry（agent / Controller生成の13種）

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

### 3.3 user-input record（5種、2経路）

ユーザー入力recordは**入力経路が2つ**あり、target experienceはその両方を要求する。

**経路1 — PowerShell / Skill入力（主経路）**: ユーザーはactive Claude Code session（PowerShell）で入力する。C-08がintentへ構造化し、**内部recordと同じ`PRODUCED -> CMD_PERSIST_RECORD -> VERIFIED`**でGitHubへ転記・確認する。bindingはC-08が採番し、evidenceはactor・input route（PowerShell）・対象head・intentを保持する。PRODUCEDの受理guardは下表の`awaiting`（`USER_CANCEL`のみ`awaiting`不問だが`pending_record`が空であることを要求し、`awaiting`を消費せず維持する）。

**経路2 — GitHub直接comment**: ユーザーがGitHubへ直接記入したcommentは既に永続化済みであり、`CMD_PERSIST_RECORD`を通さない（再投稿すると二重投稿になる）。C-05が既存commentを**観測**（取得のみ） -> C-06がcomment ID・body hash・actor（GitHub login allowlistとの完全一致、D-031、fail closed）・対象headを検証してtyped external evidenceを生成 -> C-01は`VERIFIED` eventとして直接受理する。bindingはC-06がcomment参照から導出し、**消費済みcomment IDの再提示はC-06 / C-07が拒否**する。

**両経路は同一の`EV_*_VERIFIED` semantic eventへ合流**し、以降の遷移は共通である。evidenceはactor / input route / head / intentのbindingを保持し、C-01はevidenceの構造のみを見る。

| RecordKind | 受理state | awaiting guard | VERIFIED event |
| --- | --- | --- | --- |
| `USER_DECISION` | `AWAITING_USER_DECISION` | `USER_INPUT(DECISION)` | `EV_USER_DECISION_VERIFIED` |
| `GATE_QUESTION` | `READY_FOR_HUMAN_MERGE` | `USER_INPUT(GATE)` | `EV_GATE_QUESTION_VERIFIED` |
| `GATE_CHANGES` | `READY_FOR_HUMAN_MERGE` | `USER_INPUT(GATE)` | `EV_GATE_CHANGES_VERIFIED` |
| `MERGE_APPROVAL` | `READY_FOR_HUMAN_MERGE` | `USER_INPUT(GATE)` | `EV_MERGE_APPROVAL_VERIFIED` |
| `USER_CANCEL` | terminal以外の全state | 不問（Section 3.1の規則に従う） | `EV_USER_CANCEL_VERIFIED` |

### 3.4 progress discriminatorとbudget（bounded-progress判定）

上限・膠着の判定はC-10 / C-11が行い、**同じbounded loopをもう1回継続する遷移だけ**に`progress ∈ {CONTINUE, LIMIT_REACHED, NO_PROGRESS}`を付与して入力する（event組立はC-08）。**loopを終了する結果は上限turnで得られたものでも常に処理する**。counterの管理はC-10 / C-11の責務であり、C-01は判定結果のみを受ける。**counterの消費点と判定点はregistryのdataとして明示**し、二重計上を防ぐ。

| Budget | counterを消費する遷移（新しいloop単位の開始） | 判定のみ（消費しない） | 境界のsemantics |
| --- | --- | --- | --- |
| `REVIEW_ROUND` | `EV_REVIEW_BLOCKING_VERIFIED`（新しいfix roundの開始で1回だけ増加）、`EV_CI_CODE_FAILURE_VERIFIED`（CI失敗によるfix roundの再開） | `EV_FIX_RESULT_VERIFIED`（同一roundの完了。**二重計上しない**。`NO_PROGRESS`判定のみ） | 1 round = review -> fix -> re-reviewの一巡。既定3 roundの開始から停止までを通す系列testで境界を固定する |
| `CLARIFICATION_TURN` | `EV_CLARIFICATION_QUESTION_VERIFIED`（新しいturnの開始）、`EV_VERDICT_RESUBMIT_VERIFIED`（**`REVISE_AND_RESUBMIT`は同一topicのclarification turnとして共通counterを消費**） | — | 1 turn = 質問／再提出とCodex回答の一往復。**5回目のturn開始は許可し、6回目の開始を`LIMIT_REACHED`とする**（off-by-one禁止）。同一fingerprintの質問と再提出は共通の5-turn counterを消費する |

`NO_PROGRESS`は上記の各遷移に対する膠着判定であり、event -> budgetのregistry対応によってどのloopへの判定かがtypedに特定される。

**progress共通規則**: `progress = CONTINUE`の場合のみSection 5の表の遷移とcommand発行を行う。`progress ∈ {LIMIT_REACHED, NO_PROGRESS}`の場合、`pending_record`の消費は行うが、表の遷移の代わりに**`BLOCKED`へ遷移し、commandを一切発行しない**（AC-C01-09）。このとき**`blocked_continuation` := CONTINUE行の（遷移先state、command列、awaiting）+ 停止理由（reason / budget / counter snapshot / fingerprint）**をruleが保存する。command列には`CMD_INVALIDATE_APPROVALS`等の付随actionも含まれる。保存した継続の再現は**停止理由の解消を検証した後**（Section 4.1）に限る。旧`EV_ROUND_LIMIT_REACHED` / `EV_NO_PROGRESS` / `EV_CLARIFICATION_STALLED` / `EV_DECISION_UNRESOLVED`は定義しない。

### 3.5 cancelの2系統（いずれも停止完了後にのみCANCELLEDへ入る）

`CANCELLED`への遷移は、**active processの停止とcheckpoint保存の完了をC-01が確認した後**に限る（canonical recordの検証はユーザー意図の真正性を保証するが、実行中のClaude / Codex / test processの停止は保証しないため）。

- **対話cancel**: user-input record `USER_CANCEL`（Section 3.3の両経路）のcanonical検証後、`EV_USER_CANCEL_VERIFIED`は**同一stateに留まり、`cancelling := CancelAttempt(binding)`（bindingはevidence由来でC-08採番）を設定して`CMD_HALT_RUN(binding)`だけを発行**し、`awaiting := HALT_FOR_CANCEL`とする。新agentは起動されない。**binding一致の`EV_CANCELLATION_COMPLETED`を受けて初めて`CANCELLED`へ遷移**する。実行中のprocessが無い場合、C-08は同じ完了eventを即時返す。`MERGING`のみ結果照会を優先する（Section 5）
- **緊急停止（Ctrl+C等）**: signal受信とprocess tree停止はC-03 / C-08の責務。停止とcheckpoint保存の完了後、C-08が`EV_CANCELLATION_COMPLETED`を直接入力する。このevidenceは**現在のrunとcheckpointへのbindingを持ち、C-07 / C-08が検証してから入力**する（過去runの遅延・重複完了eventは到達しない）

**cancel中の安全規則**:

- `cancelling`保持中は、binding一致の完了eventと`EV_RUN_FAILED`以外の全semantic eventを拒否する（lifecycle規則5）
- 既存の`pending_record`は**監査用に保持するが、cancel完了前にsemantic継続へ使わない**（対応するVERIFIEDも`cancelling`中は拒否される）。`CANCELLED`到達時に破棄する
- `CMD_HALT_RUN`が失敗して`FAILED`へ入った場合も`cancelling`を引き継ぎ、resumeでは**停止・checkpoint完了が最優先**になる（Section 4.1の優先順位0）
- binding不一致の完了event（過去attemptの遅延・重複）は構造化errorで拒否する

`CANCELLED`は現在runのterminalであり、提示するresume commandは直前の安全なcheckpointから**新しいrunとして**開始する。`MERGING`中の割込みは完了eventを入力せず、MachineState（`awaiting = MERGE_*`を含む）をそのままcheckpointし、新runのresumeで照会を再開する（`MERGING`のcancel経路はagent processを伴わないC-13の照会で閉じるため、`cancelling`は使わない）。

### 3.6 record以外のevent

| Event | 意味 | 発生元 |
| --- | --- | --- |
| `EV_PREFLIGHT_OK` / `EV_PREFLIGHT_NG` | 対象・policy・head・lockの検証結果（`initialize`専用） | C-07 / C-08 |
| `EV_FIX_STARTED` | hostがfinding対応へ着手 | C-08 |
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
| `EV_CANCELLATION_COMPLETED` | process tree停止とcheckpoint保存の完了（対話cancel / 緊急停止共通。attempt / run / checkpointへのbindingを持つ） | C-08 |
| `EV_BLOCK_RESOLVED_LIMIT_RAISED` | limit設定が停止時のcounter snapshotを超えて引き上げられたことをC-10 / C-11が検証した | C-10 / C-11 |
| `EV_BLOCK_RESOLVED_INTERVENTION` | no-progressを解消する追加evidenceまたはユーザー判断がcanonical recordとして確認された | C-06 / C-11 |
| `EV_RUN_FAILED` | bounded retry後の失敗（投稿・確認失敗を含む） | 各層 |
| `EV_RESUME_VALIDATED` | resume preflightと状態再構築の成功 | C-07 |
| `EV_RESUME_FALLBACK_REQUIRED` | head変更・checkpoint不整合等で安全な継続を証明できない | C-07 |
| `EV_RESUME_SAME_HEAD_VALIDATED` | merge失敗後、同一head・全条件有効の再確認 | C-07 / C-13 |

### 3.7 Command一覧

| Command | 意味 | 実行component |
| --- | --- | --- |
| `CMD_PERSIST_RECORD(kind, binding)` | canonical recordの投稿と検証（冪等。既投稿なら確認のみ） | C-05 / C-06 |
| `CMD_REQUEST_CODEX_REVIEW(purpose)` | fresh reviewerの起動。`purpose ∈ {CODE_REVIEW, CLARIFICATION, DECISION_VERDICT}` | C-09 |
| `CMD_REQUEST_HOST_ACTION(kind)` | active hostへの作業依頼（`APPLY_FINDINGS` / `DRAFT_DECISION_REQUEST` / `DRAFT_DECISION_BRIEF` / `RECORD_DECISION` / `REVISE_DECISION_REQUEST` / `ANSWER_GATE_QUESTION`等） | C-08 |
| `CMD_CHECK_CI` | 対象headのCI確認 | C-12 |
| `CMD_GENERATE_REPORT` | final reporterの起動 | C-12 |
| `CMD_HALT_RUN(binding)` | active process treeの停止とcheckpoint保存（cancel attemptへbindされる） | C-03 / C-08 |
| `CMD_VERIFY_MERGE_PRECONDITIONS` | merge直前の全条件再検証 | C-13 |
| `CMD_EXECUTE_MERGE` | **`awaiting = MERGE_PRECONDITIONS`の消費を伴うSection 5の#34でのみ発行される**merge実行 | C-13 |
| `CMD_QUERY_MERGE_OUTCOME` | merge結果のGitHub照会 | C-13 |
| `CMD_INVALIDATE_APPROVALS` | review / merge承認の失効 | C-07 |

commandは記述のみであり、C-01は実行しない。1遷移が返すcommand列の順序は決定論的とする。**command列に条件分岐の意味は無い** — 条件で結果が分かれる処理は、必ず結果eventを受けて次のruleが判断する。

## 4. 分類とresume registry

| 分類 | State |
| --- | --- |
| terminal | `MERGED`、`CANCELLED`（全event拒否） |
| resumable | `WAITING_CI`、`AWAITING_USER_DECISION`、`AWAITING_TOOL_PERMISSION`、`READY_FOR_HUMAN_MERGE`、`BLOCKED`、`FAILED`、`REPORT_FAILED`、`MERGE_FAILED` |
| active | 残りの7 state |

### 4.1 resume protocol

**`FAILED`のresume**（`EV_RESUME_VALIDATED`受理時。優先順位で決まる）:

0. **`cancelling`がある**: `CMD_HALT_RUN(binding)`の再発行のみを返す（停止・checkpoint完了が最優先。pending / awaitingの再開はしない）
1. **`pending_record`がある**: 復帰先は`pending_record.source_state`とし、同一bindingの`CMD_PERSIST_RECORD(kind, binding)`を再発行する（冪等なので、投稿済みなら確認のみが走る）。次agentは起動しない
2. **`awaiting`がある**: 復帰先へ戻り、`awaiting`に対応するcommandを再発行する（`CODEX(p) -> CMD_REQUEST_CODEX_REVIEW(p)`（fresh起動なので再発行safe）、`HOST(k) -> CMD_REQUEST_HOST_ACTION(k)`、`CI_RESULT -> CMD_CHECK_CI`、`REPORT -> CMD_GENERATE_REPORT`、`HALT_FOR_CANCEL -> CMD_HALT_RUN`、`MERGE_PRECONDITIONS -> CMD_VERIFY_MERGE_PRECONDITIONS`、`MERGE_OUTCOME(*) -> CMD_QUERY_MERGE_OUTCOME`、`USER_INPUT(*) -> なし`）
3. **いずれも無い**: `recovery_to`の駆動commandを発行する（下表で全数定義）

| recovery_to | 駆動command | 設定するawaiting |
| --- | --- | --- |
| `RUNNING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)` | `CODEX(CODE_REVIEW)` |
| `CHANGES_REQUESTED` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` | `HOST(APPLY_FINDINGS)` |
| `APPLYING_FIXES` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` | `HOST(APPLY_FINDINGS)` |
| `CLARIFYING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CLARIFICATION)` | `CODEX(CLARIFICATION)` |
| `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_CODEX_REVIEW(DECISION_VERDICT)`（直近のdecision request recordに対するfresh verdict） | `CODEX(DECISION_VERDICT)` |
| `GENERATING_REPORT` | `CMD_GENERATE_REPORT` | `REPORT` |

**`BLOCKED`のresume（停止理由の解消gate）**: `BLOCKED`は常に`blocked_continuation`（停止理由付き）を保持する。**同一条件での単純resume（`EV_RESUME_VALIDATED`）は継続を再現せず、`BLOCKED`に留まりcommandを発行しない**（停止理由と解消経路を提示する構造化resultを返す）。継続の再現は、停止理由の解消を検証した次のtyped eventだけが行う。

| Event | Guard | 挙動 |
| --- | --- | --- |
| `EV_BLOCK_RESOLVED_LIMIT_RAISED` | `blocked_continuation.reason = LIMIT_REACHED` | limit設定がcounter snapshot超に引き上げられたことをC-10 / C-11が検証済み。`blocked_continuation`の（state・command列・awaiting）を**そのまま1回だけ再現**して消費する |
| `EV_BLOCK_RESOLVED_INTERVENTION` | `blocked_continuation.reason = NO_PROGRESS` | 膠着を解消する追加evidence / ユーザー判断のcanonical recordを確認済み。同上 |
| `EV_RESUME_VALIDATED` | — | `BLOCKED`維持。**commandを発行しない** |
| `EV_RESUME_FALLBACK_REQUIRED` | — | 手動修正によるhead変更・checkpoint不整合等。旧`blocked_continuation`を**破棄**し、`RUNNING_REVIEW` + `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`へ（fresh review） |

reason不一致の解消event、消費済み継続への再解消eventは構造化errorで拒否する。`recovery_to`は`EV_RUN_FAILED`で`FAILED`へ入るruleだけが設定し（値は進入元のactive state）、`blocked_continuation`は上限到達の`BLOCKED`だけが設定する。**両者は排他**である（testで検査）。

### 4.2 resume registry

| From | Event | Guard | To | Commands / awaiting更新 |
| --- | --- | --- | --- | --- |
| `FAILED` | `EV_RESUME_VALIDATED` | — | Section 4.1の優先順位（0は現state維持、1は`source_state`、2 / 3は`recovery_to`） | Section 4.1の優先順位 |
| `FAILED` | `EV_RESUME_FALLBACK_REQUIRED` | — | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)`（`pending_record` / 旧`awaiting`は破棄。`cancelling`保持中はfallbackせず優先順位0） |
| `BLOCKED` | `EV_BLOCK_RESOLVED_LIMIT_RAISED` / `EV_BLOCK_RESOLVED_INTERVENTION` | reason一致 | `blocked_continuation.resume_state` | 保存されたcommand列とawaitingを1回だけ再現し消費（Section 4.1） |
| `BLOCKED` | `EV_RESUME_VALIDATED` | — | `BLOCKED` | —（停止理由と解消経路の提示のみ） |
| `BLOCKED` | `EV_RESUME_FALLBACK_REQUIRED` | — | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)`（`blocked_continuation`は破棄） |
| `REPORT_FAILED` | `EV_REPORTER_RETRY_REQUESTED` | — | `GENERATING_REPORT` | `CMD_GENERATE_REPORT`; **awaiting := `REPORT`** |
| `MERGE_FAILED` | `EV_RESUME_SAME_HEAD_VALIDATED` | — | `READY_FOR_HUMAN_MERGE` | —; awaiting := `USER_INPUT(GATE)`（新しい明示承認を待つ） |
| `MERGE_FAILED` | `EV_HEAD_CHANGED_EXTERNALLY` | — | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| `WAITING_CI` | `EV_CI_RESUME_REQUESTED` | — | `WAITING_CI` | `CMD_CHECK_CI`; awaiting := `CI_RESULT` |
| `AWAITING_TOOL_PERMISSION` | `EV_PERMISSION_RESUME_VALIDATED` | awaiting = `USER_INPUT(PERMISSION)`、`return_to`あり | `return_to` | `awaiting`を消費し、同一ruleで`return_to`対応の駆動commandと次のawaitingを設定（`RUNNING_REVIEW` / `APPLYING_FIXES`の2値。`pending_record`が残る場合は優先順位1が先行） |
| `AWAITING_USER_DECISION` | user-input record（`EV_USER_DECISION_VERIFIED`） | `USER_INPUT(DECISION)` | Section 5 | 通常eventがresumeを兼ねる |
| `READY_FOR_HUMAN_MERGE` | user-input record（gate系） | `USER_INPUT(GATE)` | Section 5 | 通常eventがresumeを兼ねる |

**resumable stateの保全**: 共通`EV_RUN_FAILED`はresumable state（上表の8 state）には適用しない。resumable stateでの失敗（resume試行の失敗を含む）は**同一stateに留まり、既存の`recovery_to` / `blocked_continuation` / `cancelling` / `return_to` / `pending_record` / `awaiting`を保持**する明示ruleとする。`recovery_to`が`BLOCKED` / `FAILED`自身を指すことはregistry上あり得ない。

`FAILED`への遷移rule（`EV_RUN_FAILED`）は`recovery_to` := 進入元stateを設定し、**`pending_record` / `awaiting` / `cancelling`を変更せずに引き継ぐ**。

## 5. 完全遷移表

registryの期待挙動。`VERIFIED`（内部record・user-input record）のGuard列には`pending_record`一致または外部evidence検証済みが暗黙に含まれる。内部recordの`PRODUCED`はSection 3.2 / 3.3の許可state + awaiting guardでのみ受理され、状態を変えず`awaiting`を消費して`pending_record`設定と`CMD_PERSIST_RECORD`発行を行う（表からは省略）。Guard列の「CONTINUE（budget名）」は`progress = CONTINUE`を意味し、`LIMIT_REACHED` / `NO_PROGRESS`の場合はprogress共通規則（Section 3.4）により`BLOCKED`へ入り、`blocked_continuation` := 当該行の（To、Commands、awaiting）+ 停止理由を保存してcommandを発行しない。

| # | From | Event | Guard | To | Commands / awaiting更新 |
| --- | --- | --- | --- | --- | --- |
| 1 | （`initialize` API） | `EV_PREFLIGHT_OK` | — | `RUNNING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| 2 | （`initialize` API） | `EV_PREFLIGHT_NG` | — | `FAILED` | —（`recovery_to`なし。resumeはfallback経路のみ） |
| 3 | `RUNNING_REVIEW` | `EV_REVIEW_BLOCKING_VERIFIED` | evidence一致、CONTINUE（REVIEW_ROUND消費） | `CHANGES_REQUESTED` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 4 | `RUNNING_REVIEW` | `EV_REVIEW_APPROVED_VERIFIED` | evidence一致 | `WAITING_CI` | `CMD_CHECK_CI`; awaiting := `CI_RESULT` |
| 5 | `RUNNING_REVIEW` | `EV_TOOL_PERMISSION_BLOCKED` | evidence一致 | `AWAITING_TOOL_PERMISSION` | —（`return_to := RUNNING_REVIEW`; awaiting := `USER_INPUT(PERMISSION)`） |
| 6 | `CHANGES_REQUESTED` | `EV_FIX_STARTED` | awaiting = `HOST(APPLY_FINDINGS)` | `APPLYING_FIXES` | —（awaiting維持） |
| 7 | `CHANGES_REQUESTED` | `EV_CLARIFICATION_QUESTION_VERIFIED` | evidence一致、CONTINUE（CLARIFICATION_TURN消費） | `CLARIFYING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CLARIFICATION)`; awaiting := `CODEX(CLARIFICATION)` |
| 8 | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_CONFIRMED_VERIFIED` / `EV_CLARIFICATION_REVISED_VERIFIED` | evidence一致 | `CHANGES_REQUESTED` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)`（loop終了結果。上限turnでも処理される） |
| 9 | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_WITHDRAWN_VERIFIED` | evidence一致 | `RUNNING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)`（loop終了結果） |
| 10 | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_ESCALATED_VERIFIED` | evidence一致 | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_HOST_ACTION(DRAFT_DECISION_REQUEST)`; awaiting := `HOST(DRAFT_DECISION_REQUEST)`（loop終了結果） |
| 11 | `APPLYING_FIXES` | `EV_FIX_RESULT_VERIFIED` | evidence一致、CONTINUE（REVIEW_ROUND判定のみ） | `RUNNING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| 12 | `APPLYING_FIXES` | `EV_DECISION_REQUEST_VERIFIED` | evidence一致 | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_CODEX_REVIEW(DECISION_VERDICT)`; awaiting := `CODEX(DECISION_VERDICT)` |
| 13 | `APPLYING_FIXES` | `EV_TOOL_PERMISSION_BLOCKED` | evidence一致 | `AWAITING_TOOL_PERMISSION` | —（`return_to := APPLYING_FIXES`; awaiting := `USER_INPUT(PERMISSION)`） |
| 14 | `REVIEWING_DECISION_REQUEST` | `EV_DECISION_REQUEST_VERIFIED` | evidence一致（draft / revised） | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_CODEX_REVIEW(DECISION_VERDICT)`; awaiting := `CODEX(DECISION_VERDICT)` |
| 15 | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_ASK_USER_VERIFIED` | evidence一致 | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_HOST_ACTION(DRAFT_DECISION_BRIEF)`; awaiting := `HOST(DRAFT_DECISION_BRIEF)`（loop終了結果。上限時でも処理される） |
| 16 | `REVIEWING_DECISION_REQUEST` | `EV_DECISION_BRIEF_VERIFIED` | evidence一致 | `AWAITING_USER_DECISION` | —; awaiting := `USER_INPUT(DECISION)` |
| 17 | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_PROCEED_VERIFIED` | evidence一致 | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_HOST_ACTION(RECORD_DECISION)`; awaiting := `HOST(RECORD_DECISION)`（loop終了結果） |
| 18 | `REVIEWING_DECISION_REQUEST` | `EV_DECISION_RECORD_VERIFIED` | evidence一致 | `APPLYING_FIXES` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 19 | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_RESUBMIT_VERIFIED` | evidence一致、CONTINUE（CLARIFICATION_TURN消費。同一fingerprintで共通counter） | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_HOST_ACTION(REVISE_DECISION_REQUEST)`; awaiting := `HOST(REVISE_DECISION_REQUEST)` |
| 20 | `AWAITING_USER_DECISION` | `EV_USER_DECISION_VERIFIED` | awaiting = `USER_INPUT(DECISION)` | `APPLYING_FIXES` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 21 | `AWAITING_TOOL_PERMISSION` | `EV_PERMISSION_RESUME_VALIDATED` | awaiting = `USER_INPUT(PERMISSION)`、`return_to`あり | `return_to` | Section 4.2の同名rule（awaiting消費 + `return_to`対応の駆動command + 次のawaiting設定） |
| 22 | `WAITING_CI` | `EV_CI_SUCCEEDED` | awaiting = `CI_RESULT` | `GENERATING_REPORT` | `CMD_GENERATE_REPORT`; awaiting := `REPORT` |
| 23 | `WAITING_CI` | `EV_CI_CODE_FAILURE_VERIFIED` | evidence一致、CONTINUE（REVIEW_ROUND消費） | `CHANGES_REQUESTED` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 24 | `WAITING_CI` | `EV_CI_INFRA_FAILURE` | awaiting = `CI_RESULT` | `WAITING_CI` | `CMD_CHECK_CI`（awaiting維持。bounded retryの判定は外部） |
| 25 | `WAITING_CI` | `EV_CI_TIMEOUT_RECORDED` | evidence一致 | `WAITING_CI` | —; awaiting := なし（runはcheckpointで終了。resumeは`EV_CI_RESUME_REQUESTED`） |
| 26 | `WAITING_CI` | `EV_HEAD_CHANGED_EXTERNALLY` | `pending_record`なし | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| 27 | `GENERATING_REPORT` | `EV_REPORT_VERIFIED` | evidence一致 | `READY_FOR_HUMAN_MERGE` | —; awaiting := `USER_INPUT(GATE)` |
| 28 | `GENERATING_REPORT` | `EV_REPORT_FAILED` | awaiting = `REPORT` | `REPORT_FAILED` | — |
| 29 | `READY_FOR_HUMAN_MERGE` | `EV_GATE_QUESTION_VERIFIED` | awaiting = `USER_INPUT(GATE)` | `READY_FOR_HUMAN_MERGE` | `CMD_REQUEST_HOST_ACTION(ANSWER_GATE_QUESTION)`; awaiting := `HOST(ANSWER_GATE_QUESTION)` |
| 30 | `READY_FOR_HUMAN_MERGE` | `EV_GATE_ANSWER_VERIFIED` | evidence一致 | `READY_FOR_HUMAN_MERGE` | —; awaiting := `USER_INPUT(GATE)`（質問と回答をPRへ記録しgate維持） |
| 31 | `READY_FOR_HUMAN_MERGE` | `EV_GATE_CHANGES_VERIFIED` | awaiting = `USER_INPUT(GATE)` | `CHANGES_REQUESTED` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 32 | `READY_FOR_HUMAN_MERGE` | `EV_MERGE_APPROVAL_VERIFIED` | awaiting = `USER_INPUT(GATE)` | `MERGING` | **`CMD_VERIFY_MERGE_PRECONDITIONS`のみ**; awaiting := `MERGE_PRECONDITIONS` |
| 33 | `READY_FOR_HUMAN_MERGE` | `EV_HEAD_CHANGED_EXTERNALLY` | `pending_record`なし | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| 34 | `MERGING` | `EV_MERGE_PRECONDITIONS_OK` | awaiting = `MERGE_PRECONDITIONS` | `MERGING` | **`CMD_EXECUTE_MERGE`（この経路でのみ発行）**; awaiting := `MERGE_OUTCOME(EXECUTE)`（**再入力はguard不一致で構造化error**） |
| 35 | `MERGING` | `EV_MERGE_PRECONDITION_MISMATCH` | awaiting = `MERGE_PRECONDITIONS` | `MERGE_FAILED` | — |
| 36 | `MERGING` | `EV_HEAD_CHANGED_EXTERNALLY` | awaiting = `MERGE_PRECONDITIONS` | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| 37 | `MERGING` | `EV_MERGE_CONFIRMED` | awaiting = `MERGE_OUTCOME(*)` | `MERGED` | —（awaiting消費） |
| 38 | `MERGING` | `EV_MERGE_NOT_EXECUTED_CONFIRMED` | awaiting = `MERGE_OUTCOME(CANCEL)` | `CANCELLED` | —（merge未実行の確認後。`MERGING`のcancel経路はagent processを伴わない） |
| 39 | `MERGING` | `EV_MERGE_NOT_EXECUTED_CONFIRMED` | awaiting = `MERGE_OUTCOME(FAILURE)` | `MERGE_FAILED` | —（`EV_RESUME_SAME_HEAD_VALIDATED`で復帰可能） |
| 40 | `MERGING` | `EV_MERGE_OUTCOME_UNKNOWN` | awaiting = `MERGE_OUTCOME(*)` | `MERGE_FAILED` | — |
| 41 | `MERGING` | `EV_USER_CANCEL_VERIFIED` | evidence検証済み | `MERGING` | `CMD_QUERY_MERGE_OUTCOME`; awaiting := `MERGE_OUTCOME(CANCEL)`（**即CANCELLEDにしない**。#37 / #38 / #40で解決） |
| 42 | `MERGING` | `EV_RUN_FAILED` | — | `MERGING` | `CMD_QUERY_MERGE_OUTCOME`; awaiting := `MERGE_OUTCOME(FAILURE)`（merge成否不明を通常失敗へ落とさない。#37 / #39 / #40で解決） |

**共通規則**（registry内で個別ruleへ展開され、一意性checkの対象になる）:

- **progress共通規則**（Section 3.4）: budget表の5 eventで`progress ∈ {LIMIT_REACHED, NO_PROGRESS}`の場合、`BLOCKED`へ遷移し（`blocked_continuation` := 当該行のTo / Commands / awaiting + 停止理由）、**commandを一切発行しない**
- `EV_USER_CANCEL_VERIFIED`: terminalと`MERGING`（#41）を除く全stateで**同一stateに留まり、`cancelling`を設定して`CMD_HALT_RUN(binding)`のみを発行**し、awaiting := `HALT_FOR_CANCEL`とする（新agentを起動しない。Section 3.5）
- `EV_CANCELLATION_COMPLETED`: terminalと`MERGING`を除く全stateから`CANCELLED`へ。**guard: `cancelling`保持中はbinding一致（不一致は構造化error）。`cancelling`なしの場合は緊急停止経路のevidence（run / checkpointへのbindをC-07 / C-08が検証済み）のみ**。`awaiting` / `pending_record` / `blocked_continuation` / `cancelling`は破棄
- `EV_RUN_FAILED`: terminal・`MERGING`（#42）・resumable stateを除くactive stateから`FAILED`へ（`recovery_to` := 進入元state。`pending_record` / `awaiting` / `cancelling`は変更せず引き継ぐ）
- resumable state（8 state）+ `EV_RUN_FAILED`: 同一stateに留まり、既存の付随値をすべて保持する（Section 4.2「resumable stateの保全」）
- terminal（`MERGED` / `CANCELLED`）は全eventを構造化errorで拒否
- 上記いずれにも一致しない`(state, event, guard値)`は未定義遷移として構造化errorで拒否（AC-C01-02）

**decision flowのGitHub会話順序**（#10 / #12 / #14〜#20）: target experienceの合意どおり、(a) Claude draft投稿確認、(b) Codex verdict投稿確認、(c) Claudeの最終brief / decision record / revised draftの投稿確認、(d) 次のCodexまたはユーザー、の順にGitHub上へ両agentの発言が個別に現れる。`awaiting`のlifecycle規則により、verdict確認前のbrief / decision record投稿は構造化errorで拒否される。

## 6. C-01のscope境界（Phase 1）

**実装する**: `domain/states.py`、`domain/events.py`、`domain/commands.py`、`domain/machine.py`（registry・`initialize(preflight_event)`・`transition(machine_state, event)`）、最小のvalue object（`MachineState` / `BlockedContinuation` / `CancelAttempt` / `PendingRecord` / `RecordEvidence` / `OpaqueBinding` / `Awaiting` / `Progress` / `Budget`）。

**実装しない（out of scope）**: GitHub APIアクセス・comment観測（C-05）/ actor認証・record chain・外部evidence検証・comment再利用拒否（C-06）/ checkpoint永続化・状態再構築・消費済みrecord管理（C-07）/ subprocess起動・signal処理・process停止の実行（C-03 / C-09）/ advance-submit engine・intent構造化・counter管理とprogress判定の実行（C-08 / C-10 / C-11）/ finding ledger本実装（Phase 10）とid・binding採番（C-08）/ CLI / Skill / wrapper。空moduleも作らない。

## 7. Test計画

- registryをdataとして全state × event × guard discriminator値の組合せをtable-drivenで検査（未定義は構造化error）
- **一意性とoverlap**: guard discriminator（awaiting 19値 + progress 3値 + blocked_continuation reason 2値 + pending / cancelling / return_to / recovery_toの有無）の全値を展開し、到達可能なMachineState付随値の各組合せ × 各eventについて一致rule数が常に0または1であることを検査。`recovery_to`と`blocked_continuation`の排他もここで検査
- 17 stateの到達可能性、到達不能stateの検出、遷移表・遷移図のsnapshot照合（本書Section 5と）
- 純粋性: 同一入力の再適用で同一結果、入力非変更、I/O・時刻・乱数・環境変数への非依存、command列順序の決定性
- terminalからの全event拒否。resumableはresume registryのeventのみ受理
- 付随値（`return_to` / `recovery_to` / `blocked_continuation` / `cancelling` / `awaiting` / `pending_record`）が遷移ruleだけで設定され、eventから遷移先・command列を注入できないこと
- **binding**: 対応するPRODUCEDなしのVERIFIED、binding不一致のevidence、過去evidenceの再利用、pending中の他semantic eventがすべて拒否されること。partial turnのMachineStateが再開入力として機能すること
- **awaiting順序**: `CMD_EXECUTE_MERGE`が#34以外の経路で決して発行されないこと。実行command発行前の`EV_MERGE_CONFIRMED`、`EV_MERGE_PRECONDITIONS_OK`の重複入力、verdict確認前の`DECISION_BRIEF` / `DECISION_RECORD`のPRODUCED、awaiting不一致の応答eventの拒否
- **bounded-progress**（AC-C01-09）: progress対象集合とbudget対応（消費 / 判定のみの区別を含む）がregistryから導出され、本書budget表と一致すること。**既定3 review roundを開始から停止まで通す系列test**で、roundが二重計上されないこと。**clarificationは5回目のturn開始が許可され、5回目の回答処理後の6回目の開始が`BLOCKED`になること**（off-by-one検査）。**同一fingerprintでclarification 5 turn消費後の`VERDICT_RESUBMIT`が共通counterにより`BLOCKED`になること**。5回目の`CONFIRMED` / `REVISED` / `WITHDRAWN` / `ESCALATED`と上限時の`ASK_USER` / `PROCEED`が正常に処理されること
- **block解消gate**（AC-C01-11）: **同一条件での単純resume（`EV_RESUME_VALIDATED`）がcommandを発行せず`BLOCKED`を維持**すること。`EV_BLOCK_RESOLVED_LIMIT_RAISED`（reason = LIMIT_REACHED）/ `EV_BLOCK_RESOLVED_INTERVENTION`（reason = NO_PROGRESS）でのみ保存された本来のcommand列（`CMD_INVALIDATE_APPROVALS`等の付随actionを含む）が1回だけ再現されること。reason不一致・消費済み継続への解消eventが拒否されること。`EV_RESUME_FALLBACK_REQUIRED`が継続を破棄しfresh reviewへ入ること
- **cancel / 緊急停止**（AC-C01-10）: active stateでcancel intentを検証しても完了event前はterminalにならず、新agentを起動しないこと。**`cancelling`中は古い`pending_record`のVERIFIEDを含む全semantic eventが拒否される**こと。`CMD_HALT_RUN`失敗 -> `FAILED` -> resumeで**`CMD_HALT_RUN`再発行だけ**が返ること（stale pendingの再開が先行しない）。binding不一致の完了event（過去attempt）が拒否されること。完了event後にのみ`CANCELLED`へ入ること。`MERGING`のcancel / failureが照会を経由すること
- **user-input recordの2経路同値性**: PowerShell経路とGitHub直接comment経路が同一のsemantic遷移へ合流し、直接comment経路で`CMD_PERSIST_RECORD`が発行されないこと
- **resume系列（end-to-end）**: reporter retry後の`FINAL_REPORT` PRODUCED受理、permission resumeのawaiting消費 + 駆動command発行、pendingありresumeの`CMD_PERSIST_RECORD`再発行のみ、resumable stateでの`EV_RUN_FAILED`の状態・付随値保持
- decision flowで両agentのrecordが順番に要求されること（#10→#14→#15→#16等の系列test）

## 8. 設計レビューで確定した判断

1. **Codex起動command**（round 1）: `CMD_REQUEST_CODEX_REVIEW(purpose)`のtyped purpose方式を採用。実行基盤はC-09へ集約
2. **CI code failure**（round 1）: `CHANGES_REQUESTED`へ遷移し、`CMD_INVALIDATE_APPROVALS`で既存承認を失効させる
3. **awaiting lifecycle**（round 2）: 応答を要する全commandが`awaiting`を設定し、応答eventはguard一致でのみ受理・消費される
4. **user-input recordの2経路**（round 3）: PowerShell / Skill入力はC-08構造化 + 内部record規約でGitHubへ転記し、GitHub直接commentはC-06検証済みexternal evidenceとして直接受理する
5. **budget型bounded-progress**（round 3〜5で改訂）: 同じbounded loopを継続する遷移だけがbudgetを消費して判定を受け、loop終了結果は常に処理される。counterの消費点と判定点をregistryで区別し二重計上を防ぐ。clarificationとdecision resubmitは同一fingerprintで共通の5-turn counterを消費し、5回目の開始を許可・6回目の開始を停止する
6. **blocked_continuationと解消gate**（round 4〜5で改訂）: 上限到達の`BLOCKED`はCONTINUE行の完全な継続と停止理由（reason / budget / counter snapshot / fingerprint）を保存する。**単純resumeでは再現せず**、limit引き上げ検証・膠着解消のcanonical record確認・head変更によるfallbackのいずれかを経てのみ継続または破棄する
7. **cancelの2系統・停止完了gate・attempt binding**（round 2〜5で改訂）: 対話cancelはintent検証後に`cancelling`を設定して`CMD_HALT_RUN(binding)`を発行し、binding一致の`EV_CANCELLATION_COMPLETED`後にのみ`CANCELLED`へ遷移する。cancel中はstale pendingを含む全semantic継続を拒否し、resumeでも停止完了が最優先になる
