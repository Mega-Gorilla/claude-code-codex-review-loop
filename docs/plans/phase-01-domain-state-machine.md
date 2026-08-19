<!-- SPDX-License-Identifier: Apache-2.0 -->

# Phase 1計画: C-01 domain state machine

| Field | Value |
| --- | --- |
| Status | **Accepted**（本計画PRのユーザー承認とmergeにより確定） |
| 正本関係 | [implementation plan](implementation-plan.md)のC-01節の詳細設計。target behaviorは[target experience](target-experience.md)に従い、本書は変更しない |
| 対応Issue | #6（本書は計画。Issue #6のcloseはC-01実装PRで行う） |
| 受入条件 | AC-C01-01〜12 |

## 1. 目的と正本の役割分担

target experienceの「State model」節が定義する17 stateと、「User intervention」「Failure, cancellation, and resume experience」節の挙動を、実装可能な粒度で確定する。ユーザー向け簡略図が省略している遷移（失敗系からのresume、cancel可否、GitHub投稿・確認失敗、preflight失敗、record整合性違反）をすべて定義する。

正本の役割は次のとおり分担し、二重の正本を作らない。

| 資料 | 役割 |
| --- | --- |
| 本計画文書 | **normativeな期待挙動**。実装が満たすべき遷移・規則・不変条件 |
| 実装のcode registry | **実行可能な単一source**。全ruleをdataとして保持する |
| 生成された遷移表・遷移図 | code registryから導出し、本書の表とのsnapshot照合をtestで行う（AC-C01-01） |

**registryの一意性不変条件**: guardは自由なpredicateではなく、**有限のtyped discriminator**（Section 2の`awaiting`値、`pending_record`のkind / binding一致、`progress`値、`block`のkind / reason、`cancelling` / `return_to` / `recovery_to`の有無）に限定する。到達可能なMachineState付随値の各組合せ × 各eventに対し、一致するruleは**0件または1件**である。共通規則（cancel / failure / progress / integrity等）はregistry内で個別ruleへ展開され、重複・overlapはdiscriminator全値の展開により機械的に検査してfailさせる。優先順位による解決は行わない（AC-C01-08）。

## 2. MachineState

可視の17 stateとは別に、immutableな`MachineState`を導入する。

```text
MachineState（frozen）
  state: State                       # 可視の17 stateのいずれか
  return_to: State | None            # AWAITING_TOOL_PERMISSIONからの復帰先
  recovery_to: State | None          # EV_RUN_FAILEDで入ったFAILEDの安全な再開地点
  block: BlockContext | None         # BLOCKEDの停止理由・解消policy・（あれば）本来の継続
  cancelling: CancelAttempt | None   # 進行中のcancel attempt（停止・checkpoint完了待ち）
  awaiting: Awaiting | None          # 発行済みcommandに対応する「次に受理してよい応答」の期待値
  pending_record: PendingRecord | None  # 永続化の確認待ちrecord

BlockContext（frozen）
  kind: PROGRESS | EXTERNAL_DEPENDENCY | RECORD_INTEGRITY
  binding: OpaqueBinding             # block attemptのbinding。進入eventのevidence bindingを再利用
  head: OpaqueRef                    # 停止時の対象head（C-07由来の監査値。等価比較のみ）
  continuation: BlockedContinuation | None  # PROGRESS / EXTERNAL_DEPENDENCYは保持、RECORD_INTEGRITYはNone
  reason: LIMIT_REACHED | NO_PROGRESS | None       # PROGRESSのみ
  budget: Budget | None                            # PROGRESSのみ
  counter_snapshot: OpaqueSnapshot | None          # PROGRESSのみ（C-10 / C-11の監査値）
  fingerprint: OpaqueFingerprint | None            # PROGRESSのみ（対象topic / loop。等価比較のみ）
  evidence_ref: RecordRef | None                   # EXTERNAL_DEPENDENCY / RECORD_INTEGRITYの検出evidence

BlockedContinuation（frozen）
  resume_state: State                # 継続処理を行うstate
  commands: tuple[Command, ...]      # 本来発行するはずだったcommand列（付随actionを含む）
  awaiting: Awaiting | None          # 本来設定するはずだったawaiting

CancelAttempt（frozen）
  binding: OpaqueBinding             # cancel attemptのbinding

PendingRecord（frozen）
  kind: RecordKind
  binding: OpaqueBinding             # logical turnへのopaqueなbinding値
  source_state: State                # PRODUCEDが発生したstate

RecordEvidence（frozen）
  kind: RecordKind
  binding: OpaqueBinding
  ref: RecordRef                     # 検証済みrecordへのopaque参照
```

- `return_to` / `recovery_to` / `block` / `cancelling` / `awaiting` / `pending_record`は**registry内の遷移ruleだけが設定**し、eventから注入できない。`BlockedContinuation`のcommand列・awaitingはregistryの該当行から導出される有限集合であり、任意のcommand列を持ち込めない。binding / head / snapshot / fingerprintはevent evidence由来の監査値で、C-01は**等価比較のみ**を行う
- **bindingの所有者**: 内部recordはC-08採番、外部evidenceはC-06がcomment参照から導出する。**cancel attemptとblock attemptのbindingは、それぞれの進入eventのrecord / evidence bindingを再利用**し、C-01は新たな採番をしない（純粋性の維持）。緊急停止の完了evidenceのみrun / checkpointへのbindをC-07 / C-08が構成・検証する
- 数量判定（round数・clarification turn数・膠着）はC-01の外で行い、判定結果を`progress` discriminator（Section 3.4）として入力する。counterの管理・更新はC-10 / C-11が行い、C-01は算術を行わない

### 2.1 Awaiting（有限のtyped discriminator、19値）

`awaiting`は「どのcommandを発行済みで、次にどの応答だけを受理するか」を表す。値は次の19値に限る。

| Awaiting値 | 設定するcommand | 受理する応答 |
| --- | --- | --- |
| `CODEX(CODE_REVIEW)` | `CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)` | `REVIEW_RESULT` / `PERMISSION_BLOCK`のPRODUCED |
| `CODEX(CLARIFICATION)` | `CMD_REQUEST_CODEX_REVIEW(CLARIFICATION)` | `CLARIFICATION_ANSWER`のPRODUCED |
| `CODEX(DECISION_VERDICT)` | `CMD_REQUEST_CODEX_REVIEW(DECISION_VERDICT)` | `DECISION_VERDICT`のPRODUCED |
| `HOST(APPLY_FINDINGS)` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` | `EV_FIX_STARTED`、`FIX_RESULT` / `CLARIFICATION_QUESTION` / `DECISION_REQUEST` / `EXTERNAL_DEPENDENCY` / `PERMISSION_BLOCK`のPRODUCED |
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
| `HALT_FOR_CANCEL` | `CMD_HALT_RUN(binding)` | `EV_CANCELLATION_COMPLETED`（`cancelling`とのbinding一致） |
| `USER_INPUT(DECISION)` | —（`AWAITING_USER_DECISION`進入ruleが設定） | user-input record `USER_DECISION`（両経路。Section 3.3） |
| `USER_INPUT(GATE)` | —（`READY_FOR_HUMAN_MERGE`進入ruleが設定） | user-input record `GATE_QUESTION` / `GATE_CHANGES` / `MERGE_APPROVAL`（両経路） |
| `USER_INPUT(PERMISSION)` | —（`AWAITING_TOOL_PERMISSION`進入ruleが設定） | `EV_PERMISSION_RESUME_VALIDATED` |

**lifecycle規則**（AC-C01-08の核）:

1. 応答を要するcommandを発行する遷移ruleは、対応する`awaiting`値を**同一ruleで設定**する
2. 応答event（結果event、PRODUCED）は、`awaiting`が当該応答を受理する値である場合**のみ**受理され、受理時に`awaiting`を**消費**（`None`化）または次の期待値へ**更新**する
3. `awaiting`不一致の応答、消費済み応答の再入力、順序を飛ばした応答は**構造化errorで拒否**される
4. 例外として`awaiting`に関わらず受理されるのは、`EV_RUN_FAILED`（共通規則）、`USER_CANCEL`（Section 3.3）、`EV_CANCELLATION_COMPLETED`（binding guardに従う）、`EV_RECORD_INTEGRITY_VIOLATION_DETECTED`（Section 3.5）、resume系のみ
5. **`cancelling`保持中**は、binding一致の`EV_CANCELLATION_COMPLETED`と`EV_RUN_FAILED`以外の全semantic eventを拒否する。**resume系eventも含め、`cancelling`保持中の明示resumeは`CMD_HALT_RUN(binding)`の再発行だけを返す**（Section 4.1の横断規則）

## 3. Record体系

canonical recordは生成主体で2系統に分かれ、規約が異なる。

### 3.1 内部record（Controller / agentが生成し、Controllerが投稿する）

次の対で構成される。

1. `EV_*_PRODUCED(kind, binding)`: 発言が生成された。**許可source state（Section 3.2）かつ`awaiting`一致**の場合のみ受理。`awaiting`を消費し、`pending_record = (kind, binding, source_state)`を設定して`CMD_PERSIST_RECORD(kind, binding)`を返す。状態は変えない
2. `EV_*_VERIFIED(evidence)`: 後続層が投稿・read-after-write・record検証を完了した。**`pending_record`とevidenceの`kind`および`binding`が一致する場合だけ**受理され、`pending_record`を消費して状態を進め、次のcommand（と次の`awaiting`）を設定する

この構造により、対応する`PRODUCED`を経ない`VERIFIED`、過去turnのevidence再利用、別turnへの流用はbinding不一致として拒否される（AC-C01-03）。`pending_record`保持中は、対応する`VERIFIED`・`EV_RUN_FAILED`・外部経路の`EV_USER_CANCEL_VERIFIED`・`EV_RECORD_INTEGRITY_VIOLATION_DETECTED`以外のsemantic eventを拒否する（内部経路の`USER_CANCEL` / `BLOCK_INTERVENTION`のPRODUCEDはpending slotが空くまでC-08が保留する）。投稿後・確認前に中断したpartial turnは、`pending_record`がMachineStateに残ることでcheckpoint（C-07）から同一turnとして再開できる。

`CMD_PERSIST_RECORD`は**冪等**であることをC-05へ要求する: 同一bindingのrecordが既に投稿済みならば再投稿せず、read-after-write確認から再開する（resume時の二重投稿防止）。

### 3.2 内部record kind registry（agent / Controller生成の14種）

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
| `EXTERNAL_DEPENDENCY` | `APPLYING_FIXES` | `HOST(APPLY_FINDINGS)` | `EV_EXTERNAL_DEPENDENCY_VERIFIED`（外部依存により進行不能である旨のhost報告） |
| `PERMISSION_BLOCK` | `RUNNING_REVIEW` / `APPLYING_FIXES` | `CODEX(CODE_REVIEW)` / `HOST(APPLY_FINDINGS)` | `EV_TOOL_PERMISSION_BLOCKED` |
| `CI_TIMEOUT` | `WAITING_CI` | `CI_RESULT` | `EV_CI_TIMEOUT_RECORDED` |
| `CI_CODE_FAILURE` | `WAITING_CI` | `CI_RESULT` | `EV_CI_CODE_FAILURE_VERIFIED` |
| `FINAL_REPORT` | `GENERATING_REPORT` | `REPORT` | `EV_REPORT_VERIFIED` |
| `GATE_ANSWER` | `READY_FOR_HUMAN_MERGE` | `HOST(ANSWER_GATE_QUESTION)` | `EV_GATE_ANSWER_VERIFIED` |

### 3.3 user-input record（6種、2経路）

ユーザー入力recordは**入力経路が2つ**あり、target experienceはその両方を要求する。

**経路1 — PowerShell / Skill入力（主経路）**: ユーザーはactive Claude Code session（PowerShell）で入力する。C-08がintentへ構造化し、**内部recordと同じ`PRODUCED -> CMD_PERSIST_RECORD -> VERIFIED`**でGitHubへ転記・確認する。bindingはC-08が採番し、evidenceはactor・input route（PowerShell）・対象head・intentを保持する。PRODUCEDの受理guardは下表のとおり（`USER_CANCEL` / `BLOCK_INTERVENTION`は`awaiting`不問だが`pending_record`が空であることを要求し、`awaiting`を消費せず維持する）。

**経路2 — GitHub直接comment**: ユーザーがGitHubへ直接記入したcommentは既に永続化済みであり、`CMD_PERSIST_RECORD`を通さない（再投稿すると二重投稿になる）。C-05が既存commentを**観測**（取得のみ） -> C-06がcomment ID・body hash・actor（GitHub login allowlistとの完全一致、D-031、fail closed）・対象headを検証してtyped external evidenceを生成 -> C-01は`VERIFIED` eventとして直接受理する。bindingはC-06がcomment参照から導出し、**消費済みcomment IDの再提示はC-06 / C-07が拒否**する。

**両経路は同一の`EV_*_VERIFIED` semantic eventへ合流**し、以降の遷移は共通である。evidenceはactor / input route / head / intentのbindingを保持し、C-01はevidenceの構造のみを見る。

| RecordKind | 受理state | 受理guard | VERIFIED event |
| --- | --- | --- | --- |
| `USER_DECISION` | `AWAITING_USER_DECISION` | awaiting = `USER_INPUT(DECISION)` | `EV_USER_DECISION_VERIFIED` |
| `GATE_QUESTION` | `READY_FOR_HUMAN_MERGE` | awaiting = `USER_INPUT(GATE)` | `EV_GATE_QUESTION_VERIFIED` |
| `GATE_CHANGES` | `READY_FOR_HUMAN_MERGE` | awaiting = `USER_INPUT(GATE)` | `EV_GATE_CHANGES_VERIFIED` |
| `MERGE_APPROVAL` | `READY_FOR_HUMAN_MERGE` | awaiting = `USER_INPUT(GATE)` | `EV_MERGE_APPROVAL_VERIFIED` |
| `BLOCK_INTERVENTION` | `BLOCKED` | `block.kind`が解消を許可（Section 3.5の解消matrix） | `EV_BLOCK_RESOLVED_INTERVENTION` |
| `USER_CANCEL` | terminal以外の全state | 不問（Section 3.1の規則に従う） | `EV_USER_CANCEL_VERIFIED` |

### 3.4 progress discriminatorとbudget（bounded-progress判定）

上限・膠着の判定はC-10 / C-11が行い、**同じbounded loopをもう1回継続する遷移だけ**に`progress ∈ {CONTINUE, LIMIT_REACHED, NO_PROGRESS}`を付与して入力する（event組立はC-08）。**loopを終了する結果は上限turnで得られたものでも常に処理する**。counterの管理はC-10 / C-11の責務であり、C-01は判定結果のみを受ける。**counterの消費点と判定点はregistryのdataとして明示**し、二重計上を防ぐ。

| Budget | counterを消費する遷移（新しいloop単位の開始） | 判定のみ（消費しない） | 境界のsemantics |
| --- | --- | --- | --- |
| `REVIEW_ROUND` | `EV_REVIEW_BLOCKING_VERIFIED`（新しいfix roundの開始で1回だけ増加）、`EV_CI_CODE_FAILURE_VERIFIED`（CI失敗によるfix roundの再開） | `EV_FIX_RESULT_VERIFIED`（同一roundの完了。**二重計上しない**。`NO_PROGRESS`判定のみ） | 1 round = review -> fix -> re-reviewの一巡。既定3 roundの開始から停止までを通す系列testで境界を固定する |
| `CLARIFICATION_TURN` | `EV_CLARIFICATION_QUESTION_VERIFIED`（新しいturnの開始）、`EV_VERDICT_RESUBMIT_VERIFIED`（**`REVISE_AND_RESUBMIT`は同一topicのclarification turnとして共通counterを消費**） | — | 1 turn = 質問／再提出とCodex回答の一往復。**5回目のturn開始は許可し、6回目の開始を`LIMIT_REACHED`とする**（off-by-one禁止）。同一fingerprintの質問と再提出は共通の5-turn counterを消費する |

`NO_PROGRESS`は上記の各遷移に対する膠着判定であり、event -> budgetのregistry対応によってどのloopへの判定かがtypedに特定される。

**progress共通規則**: `progress = CONTINUE`の場合のみSection 5の表の遷移とcommand発行を行う。`progress ∈ {LIMIT_REACHED, NO_PROGRESS}`の場合、`pending_record`の消費は行うが、表の遷移の代わりに**`BLOCKED`へ遷移し、commandを一切発行しない**（AC-C01-09）。このとき`block := BlockContext(kind = PROGRESS, binding = 当該VERIFIED evidenceのbinding, head, continuation = 当該行のTo / Commands / awaiting, reason / budget / counter_snapshot / fingerprint)`をruleが保存する。継続の再現はSection 3.5の解消matrixに従う。旧`EV_ROUND_LIMIT_REACHED` / `EV_NO_PROGRESS` / `EV_CLARIFICATION_STALLED` / `EV_DECISION_UNRESOLVED`は定義しない。

### 3.5 Block体系（BLOCKEDの3種と解消matrix）

`BLOCKED`は常に`block: BlockContext`を保持し、kindごとに進入経路と解消policyが異なる。**いずれのkindでも、同一条件での単純resume（`EV_RESUME_VALIDATED`）は継続を再現せず、`BLOCKED`を維持してcommandを発行しない**。

| kind | 進入 | continuation | 解消 |
| --- | --- | --- | --- |
| `PROGRESS` | progress共通規則（Section 3.4） | あり | `EV_BLOCK_RESOLVED_LIMIT_RAISED`（reason = LIMIT_REACHED）/ `EV_BLOCK_RESOLVED_INTERVENTION`（reason = NO_PROGRESS）/ fallback |
| `EXTERNAL_DEPENDENCY` | `EV_EXTERNAL_DEPENDENCY_VERIFIED`（内部record。hostが外部依存による進行不能を報告し永続化確認済み） | あり（進入元の駆動command） | `EV_BLOCK_RESOLVED_INTERVENTION`（依存解消の確認）/ fallback |
| `RECORD_INTEGRITY` | `EV_RECORD_INTEGRITY_VIOLATION_DETECTED`（C-06がcanonical commentの改変・削除・sequence gapを検出。AC-C06-06〜08） | **なし** | **fallbackのみ**（`EV_BLOCK_RESOLVED_*`は受理しない。改変されたrecord chainの上で継続を再現しない） |

**解消evidenceのbinding**（AC-C01-11）: `EV_BLOCK_RESOLVED_LIMIT_RAISED` / `EV_BLOCK_RESOLVED_INTERVENTION`のevidenceは、**現在のrun・block attempt binding・reason・budget・counter snapshot・fingerprint・対象head**を保持し、C-01は`block`との**完全一致**を有限guardとして要求する（`PROGRESS`以外のkindではreason / budget / snapshot / fingerprintはNoneどうしの一致）。過去または別block向けの解消event、消費済みblockへのreplayはbinding不一致として構造化errorで拒否する。検証の実体は`EV_BLOCK_RESOLVED_LIMIT_RAISED`がC-10 / C-11（limit設定がsnapshot超に引き上げられた）、`EV_BLOCK_RESOLVED_INTERVENTION`がuser-input record `BLOCK_INTERVENTION`（Section 3.3の2経路）のcanonical検証である。

`EV_RECORD_INTEGRITY_VIOLATION_DETECTED`はterminalを除く全stateで受理され（`awaiting` / `pending_record`不問。ただし`cancelling`中はlifecycle規則5が優先）、`BLOCKED`へ遷移して`pending_record` / `awaiting`を**破棄**する（改変されたchain上のturnを信頼しない。検出evidenceは`block.evidence_ref`として保持）。

### 3.6 cancelの2系統（いずれも停止完了後にのみCANCELLEDへ入る）

`CANCELLED`への遷移は、**active processの停止とcheckpoint保存の完了をC-01が確認した後**に限る。

- **対話cancel**: user-input record `USER_CANCEL`（Section 3.3の両経路）のcanonical検証後、`EV_USER_CANCEL_VERIFIED`は**同一stateに留まり、`cancelling := CancelAttempt(binding)`を設定して`CMD_HALT_RUN(binding)`だけを発行**し、`awaiting := HALT_FOR_CANCEL`とする。**attempt bindingは`USER_CANCEL` recordのbindingを再利用する**（経路1はC-08採番、経路2はC-06導出。C-01は等価比較のみ）。新agentは起動されない。binding一致の`EV_CANCELLATION_COMPLETED`を受けて初めて`CANCELLED`へ遷移する。実行中のprocessが無い場合、C-08は同じ完了eventを即時返す。`MERGING`のみ結果照会を優先する（Section 5）
- **緊急停止（Ctrl+C等）**: signal受信とprocess tree停止はC-03 / C-08の責務。停止とcheckpoint保存の完了後、C-08が`EV_CANCELLATION_COMPLETED`を直接入力する。このevidenceは**現在のrunとcheckpointへのbindingを持ち、C-07 / C-08が検証してから入力**する（過去runの遅延・重複完了eventは到達しない）

**cancel中の安全規則**:

- `cancelling`保持中は、binding一致の完了eventと`EV_RUN_FAILED`以外の全semantic eventを拒否する（lifecycle規則5）
- 既存の`pending_record`は**監査用に保持するが、cancel完了前にsemantic継続へ使わない**。`CANCELLED`到達時に破棄する
- `CMD_HALT_RUN`の失敗後も`cancelling`は保持され、**stateの分類に関わらず（terminal / `MERGING`を除く全state）、明示resumeは`CMD_HALT_RUN(binding)`の再発行だけを返す**（Section 4.1の横断規則。`FAILED`経由でもresumable state滞在のままでも同じ）
- binding不一致の完了event（過去attemptの遅延・重複）は構造化errorで拒否する

`CANCELLED`は現在runのterminalであり、提示するresume commandは直前の安全なcheckpointから**新しいrunとして**開始する。`MERGING`中の割込みは完了eventを入力せず、MachineStateをそのままcheckpointし、新runのresumeで照会を再開する（`MERGING`のcancel経路はagent processを伴わないC-13の照会で閉じるため、`cancelling`は使わない）。

### 3.7 record以外のevent

| Event | 意味 | 発生元 |
| --- | --- | --- |
| `EV_PREFLIGHT_OK` / `EV_PREFLIGHT_NG` | 対象・policy・head・lockの検証結果（`initialize`専用） | C-07 / C-08 |
| `EV_FIX_STARTED` | hostがfinding対応へ着手 | C-08 |
| `EV_PERMISSION_RESUME_VALIDATED` | Permission IDとheadの再検証を伴う明示resume | C-08 |
| `EV_CI_SUCCEEDED` / `EV_CI_INFRA_FAILURE` | 対象headのCI結果 | C-12 |
| `EV_CI_RESUME_REQUESTED` | `WAITING_CI`からの明示resume | C-08 |
| `EV_REPORT_FAILED` | report生成失敗 | C-12 |
| `EV_REPORTER_RETRY_REQUESTED` | reporterのみ再実行の明示指示 | C-08 |
| `EV_MERGE_PRECONDITIONS_OK` / `EV_MERGE_PRECONDITION_MISMATCH` | merge直前再検証の結果 | C-13 |
| `EV_MERGE_CONFIRMED` / `EV_MERGE_NOT_EXECUTED_CONFIRMED` / `EV_MERGE_OUTCOME_UNKNOWN` | merge結果照会 | C-13 |
| `EV_HEAD_CHANGED_EXTERNALLY` | 外部からのhead更新を検出 | C-07 |
| `EV_CANCELLATION_COMPLETED` | process tree停止とcheckpoint保存の完了（attempt / run / checkpointへのbindingを持つ） | C-08 |
| `EV_RECORD_INTEGRITY_VIOLATION_DETECTED` | canonical commentの改変・削除・sequence gapの検出（AC-C06-06〜08） | C-06 |
| `EV_BLOCK_RESOLVED_LIMIT_RAISED` | limit設定が停止時のsnapshot超に引き上げられたことの検証（blockへの完全binding付き） | C-10 / C-11 |
| `EV_BLOCK_RESOLVED_INTERVENTION` | user-input record `BLOCK_INTERVENTION`のcanonical検証（blockへの完全binding付き） | C-06 / C-11 |
| `EV_RUN_FAILED` | bounded retry後の失敗（投稿・確認失敗を含む） | 各層 |
| `EV_RESUME_VALIDATED` | resume preflightと状態再構築の成功 | C-07 |
| `EV_RESUME_FALLBACK_REQUIRED` | head変更・checkpoint不整合等で安全な継続を証明できない | C-07 |
| `EV_RESUME_SAME_HEAD_VALIDATED` | merge失敗後、同一head・全条件有効の再確認 | C-07 / C-13 |

### 3.8 Command一覧

| Command | 意味 | 実行component |
| --- | --- | --- |
| `CMD_PERSIST_RECORD(kind, binding)` | canonical recordの投稿と検証（冪等。既投稿なら確認のみ） | C-05 / C-06 |
| `CMD_REQUEST_CODEX_REVIEW(purpose)` | fresh reviewerの起動。`purpose ∈ {CODE_REVIEW, CLARIFICATION, DECISION_VERDICT}` | C-09 |
| `CMD_REQUEST_HOST_ACTION(kind)` | active hostへの作業依頼（`APPLY_FINDINGS` / decision系 / `ANSWER_GATE_QUESTION`等） | C-08 |
| `CMD_CHECK_CI` | 対象headのCI確認 | C-12 |
| `CMD_GENERATE_REPORT` | final reporterの起動 | C-12 |
| `CMD_HALT_RUN(binding)` | active process treeの停止とcheckpoint保存（cancel attemptへbind） | C-03 / C-08 |
| `CMD_VERIFY_MERGE_PRECONDITIONS` | merge直前の全条件再検証 | C-13 |
| `CMD_EXECUTE_MERGE` | **`awaiting = MERGE_PRECONDITIONS`の消費を伴うSection 5の#34でのみ発行される**merge実行 | C-13 |
| `CMD_QUERY_MERGE_OUTCOME` | merge結果のGitHub照会 | C-13 |
| `CMD_INVALIDATE_APPROVALS` | review / merge承認の失効 | C-07 |

commandは記述のみであり、C-01は実行しない。1遷移が返すcommand列の順序は決定論的とする。**command列に条件分岐の意味は無い**。

## 4. 分類とresume registry

| 分類 | State |
| --- | --- |
| terminal | `MERGED`、`CANCELLED`（全event拒否） |
| resumable | `WAITING_CI`、`AWAITING_USER_DECISION`、`AWAITING_TOOL_PERMISSION`、`READY_FOR_HUMAN_MERGE`、`BLOCKED`、`FAILED`、`REPORT_FAILED`、`MERGE_FAILED` |
| active | 残りの7 state |

### 4.1 resume protocol

**横断規則（最優先）**: `cancelling`保持中は、**stateの分類に関わらず**（terminal / `MERGING`を除く）、`EV_RESUME_VALIDATED`および各stateの明示resume event（`EV_CI_RESUME_REQUESTED`等）に対して**`CMD_HALT_RUN(binding)`の再発行だけ**を返し、状態と付随値を維持する。停止・checkpoint完了（binding一致の`EV_CANCELLATION_COMPLETED`）が他のあらゆる再開に先行する。

**`FAILED`のresume**（`EV_RESUME_VALIDATED`受理時。横断規則の次に、優先順位で決まる）:

1. **`pending_record`がある**: 復帰先は`pending_record.source_state`とし、同一bindingの`CMD_PERSIST_RECORD(kind, binding)`を再発行する。次agentは起動しない
2. **`awaiting`がある**: 復帰先へ戻り、`awaiting`に対応するcommandを再発行する（対応表はSection 2.1のcommand列。`USER_INPUT(*)`はcommandなし）
3. **`recovery_to`がある**: その駆動commandを発行する（下表で全数定義）
4. **いずれも無い（preflight NGで作られた未開始の`FAILED`）**: `EV_RESUME_VALIDATED` / `EV_RESUME_FALLBACK_REQUIRED`を**構造化errorで拒否**する。復帰は新しいrunの`initialize(preflight_event)`（preflight再実行）のみ

| recovery_to | 駆動command | 設定するawaiting |
| --- | --- | --- |
| `RUNNING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)` | `CODEX(CODE_REVIEW)` |
| `CHANGES_REQUESTED` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` | `HOST(APPLY_FINDINGS)` |
| `APPLYING_FIXES` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)` | `HOST(APPLY_FINDINGS)` |
| `CLARIFYING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CLARIFICATION)` | `CODEX(CLARIFICATION)` |
| `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_CODEX_REVIEW(DECISION_VERDICT)` | `CODEX(DECISION_VERDICT)` |
| `GENERATING_REPORT` | `CMD_GENERATE_REPORT` | `REPORT` |

**`BLOCKED`のresume（解消gate）**: Section 3.5の解消matrixに従う。単純resume（`EV_RESUME_VALIDATED`）は`BLOCKED`維持・command発行なし。解消eventは`block`との**完全binding一致**（binding / reason / budget / snapshot / fingerprint / head）で受理され、`continuation`があればそれを**1回だけ再現**して消費する。`RECORD_INTEGRITY`は解消eventを受理せず、`EV_RESUME_FALLBACK_REQUIRED`（継続破棄 + `CMD_INVALIDATE_APPROVALS` + fresh review）のみ許可する。

`recovery_to`は`EV_RUN_FAILED`で`FAILED`へ入るruleだけが設定し、`block`は`BLOCKED`へ入るruleだけが設定する。**両者は排他**である（testで検査）。

### 4.2 resume registry

| From | Event | Guard | To | Commands / awaiting更新 |
| --- | --- | --- | --- | --- |
| terminal / `MERGING`以外の全state | `EV_RESUME_VALIDATED`等の明示resume | **`cancelling`あり** | 同一state | `CMD_HALT_RUN(binding)`再発行のみ（横断規則） |
| `FAILED` | `EV_RESUME_VALIDATED` | `cancelling`なし、優先順位1〜3のいずれかが該当 | Section 4.1 | Section 4.1 |
| `FAILED` | `EV_RESUME_VALIDATED` / `EV_RESUME_FALLBACK_REQUIRED` | `cancelling`なし、付随値なし（preflight NG） | —（拒否） | 構造化error。復帰は新runの`initialize`のみ |
| `FAILED` | `EV_RESUME_FALLBACK_REQUIRED` | `cancelling`なし、付随値あり | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)`（pending / 旧awaitingは破棄） |
| `BLOCKED` | `EV_BLOCK_RESOLVED_LIMIT_RAISED` | kind = PROGRESS、reason = LIMIT_REACHED、**完全binding一致** | `block.continuation.resume_state` | 保存されたcommand列とawaitingを1回だけ再現し`block`を消費 |
| `BLOCKED` | `EV_BLOCK_RESOLVED_INTERVENTION` | （kind = PROGRESS かつ reason = NO_PROGRESS）または kind = EXTERNAL_DEPENDENCY、**完全binding一致** | 同上 | 同上 |
| `BLOCKED` | `EV_RESUME_VALIDATED` | `cancelling`なし | `BLOCKED` | —（停止理由と解消経路の提示のみ） |
| `BLOCKED` | `EV_RESUME_FALLBACK_REQUIRED` | `cancelling`なし | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)`（`block`は破棄。`RECORD_INTEGRITY`の唯一の出口） |
| `REPORT_FAILED` | `EV_REPORTER_RETRY_REQUESTED` | `cancelling`なし | `GENERATING_REPORT` | `CMD_GENERATE_REPORT`; awaiting := `REPORT` |
| `MERGE_FAILED` | `EV_RESUME_SAME_HEAD_VALIDATED` | `cancelling`なし | `READY_FOR_HUMAN_MERGE` | —; awaiting := `USER_INPUT(GATE)` |
| `MERGE_FAILED` | `EV_HEAD_CHANGED_EXTERNALLY` | — | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| `WAITING_CI` | `EV_CI_RESUME_REQUESTED` | `cancelling`なし | `WAITING_CI` | `CMD_CHECK_CI`; awaiting := `CI_RESULT` |
| `AWAITING_TOOL_PERMISSION` | `EV_PERMISSION_RESUME_VALIDATED` | `cancelling`なし、awaiting = `USER_INPUT(PERMISSION)`、`return_to`あり | `return_to` | awaiting消費 + `return_to`対応の駆動command + 次のawaiting（`RUNNING_REVIEW` / `APPLYING_FIXES`の2値。pendingが残る場合は優先順位1が先行） |
| `AWAITING_USER_DECISION` / `READY_FOR_HUMAN_MERGE` | user-input record | 各guard | Section 5 | 通常eventがresumeを兼ねる |

**resumable stateの保全**: 共通`EV_RUN_FAILED`はresumable state（8 state）には適用しない。resumable stateでの失敗は**同一stateに留まり、既存の付随値（`recovery_to` / `block` / `cancelling` / `return_to` / `pending_record` / `awaiting`）をすべて保持**する明示ruleとする。`cancelling`保持中でも横断規則により停止再開の経路が常に存在する。

`FAILED`への遷移rule（`EV_RUN_FAILED`）は`recovery_to` := 進入元stateを設定し、`pending_record` / `awaiting` / `cancelling`を変更せずに引き継ぐ。

## 5. 完全遷移表

registryの期待挙動。`VERIFIED`のGuard列には`pending_record`一致または外部evidence検証済みが暗黙に含まれる。内部recordの`PRODUCED`はSection 3.2 / 3.3の許可state + guardでのみ受理され、状態を変えず`awaiting`を消費して`pending_record`設定と`CMD_PERSIST_RECORD`発行を行う（表からは省略）。Guard列の「CONTINUE（budget名）」は`progress = CONTINUE`を意味し、`LIMIT_REACHED` / `NO_PROGRESS`はprogress共通規則で`BLOCKED`へ入る。

| # | From | Event | Guard | To | Commands / awaiting更新 |
| --- | --- | --- | --- | --- | --- |
| 1 | （`initialize` API） | `EV_PREFLIGHT_OK` | — | `RUNNING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| 2 | （`initialize` API） | `EV_PREFLIGHT_NG` | — | `FAILED` | —（付随値なし。resume系は拒否。復帰は新runの`initialize`のみ） |
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
| 15 | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_ASK_USER_VERIFIED` | evidence一致 | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_HOST_ACTION(DRAFT_DECISION_BRIEF)`; awaiting := `HOST(DRAFT_DECISION_BRIEF)`（loop終了結果） |
| 16 | `REVIEWING_DECISION_REQUEST` | `EV_DECISION_BRIEF_VERIFIED` | evidence一致 | `AWAITING_USER_DECISION` | —; awaiting := `USER_INPUT(DECISION)` |
| 17 | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_PROCEED_VERIFIED` | evidence一致 | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_HOST_ACTION(RECORD_DECISION)`; awaiting := `HOST(RECORD_DECISION)`（loop終了結果） |
| 18 | `REVIEWING_DECISION_REQUEST` | `EV_DECISION_RECORD_VERIFIED` | evidence一致 | `APPLYING_FIXES` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 19 | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_RESUBMIT_VERIFIED` | evidence一致、CONTINUE（CLARIFICATION_TURN消費。同一fingerprintで共通counter） | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_HOST_ACTION(REVISE_DECISION_REQUEST)`; awaiting := `HOST(REVISE_DECISION_REQUEST)` |
| 20 | `AWAITING_USER_DECISION` | `EV_USER_DECISION_VERIFIED` | awaiting = `USER_INPUT(DECISION)` | `APPLYING_FIXES` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 21 | `AWAITING_TOOL_PERMISSION` | `EV_PERMISSION_RESUME_VALIDATED` | awaiting = `USER_INPUT(PERMISSION)`、`return_to`あり | `return_to` | Section 4.2の同名rule |
| 22 | `WAITING_CI` | `EV_CI_SUCCEEDED` | awaiting = `CI_RESULT` | `GENERATING_REPORT` | `CMD_GENERATE_REPORT`; awaiting := `REPORT` |
| 23 | `WAITING_CI` | `EV_CI_CODE_FAILURE_VERIFIED` | evidence一致、CONTINUE（REVIEW_ROUND消費） | `CHANGES_REQUESTED` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 24 | `WAITING_CI` | `EV_CI_INFRA_FAILURE` | awaiting = `CI_RESULT` | `WAITING_CI` | `CMD_CHECK_CI`（awaiting維持） |
| 25 | `WAITING_CI` | `EV_CI_TIMEOUT_RECORDED` | evidence一致 | `WAITING_CI` | —; awaiting := なし（runはcheckpointで終了） |
| 26 | `WAITING_CI` | `EV_HEAD_CHANGED_EXTERNALLY` | `pending_record`なし | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| 27 | `GENERATING_REPORT` | `EV_REPORT_VERIFIED` | evidence一致 | `READY_FOR_HUMAN_MERGE` | —; awaiting := `USER_INPUT(GATE)` |
| 28 | `GENERATING_REPORT` | `EV_REPORT_FAILED` | awaiting = `REPORT` | `REPORT_FAILED` | — |
| 29 | `READY_FOR_HUMAN_MERGE` | `EV_GATE_QUESTION_VERIFIED` | awaiting = `USER_INPUT(GATE)` | `READY_FOR_HUMAN_MERGE` | `CMD_REQUEST_HOST_ACTION(ANSWER_GATE_QUESTION)`; awaiting := `HOST(ANSWER_GATE_QUESTION)` |
| 30 | `READY_FOR_HUMAN_MERGE` | `EV_GATE_ANSWER_VERIFIED` | evidence一致 | `READY_FOR_HUMAN_MERGE` | —; awaiting := `USER_INPUT(GATE)` |
| 31 | `READY_FOR_HUMAN_MERGE` | `EV_GATE_CHANGES_VERIFIED` | awaiting = `USER_INPUT(GATE)` | `CHANGES_REQUESTED` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 32 | `READY_FOR_HUMAN_MERGE` | `EV_MERGE_APPROVAL_VERIFIED` | awaiting = `USER_INPUT(GATE)` | `MERGING` | **`CMD_VERIFY_MERGE_PRECONDITIONS`のみ**; awaiting := `MERGE_PRECONDITIONS` |
| 33 | `READY_FOR_HUMAN_MERGE` | `EV_HEAD_CHANGED_EXTERNALLY` | `pending_record`なし | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| 34 | `MERGING` | `EV_MERGE_PRECONDITIONS_OK` | awaiting = `MERGE_PRECONDITIONS` | `MERGING` | **`CMD_EXECUTE_MERGE`（この経路でのみ発行）**; awaiting := `MERGE_OUTCOME(EXECUTE)` |
| 35 | `MERGING` | `EV_MERGE_PRECONDITION_MISMATCH` | awaiting = `MERGE_PRECONDITIONS` | `MERGE_FAILED` | — |
| 36 | `MERGING` | `EV_HEAD_CHANGED_EXTERNALLY` | awaiting = `MERGE_PRECONDITIONS` | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| 37 | `MERGING` | `EV_MERGE_CONFIRMED` | awaiting = `MERGE_OUTCOME(*)` | `MERGED` | — |
| 38 | `MERGING` | `EV_MERGE_NOT_EXECUTED_CONFIRMED` | awaiting = `MERGE_OUTCOME(CANCEL)` | `CANCELLED` | — |
| 39 | `MERGING` | `EV_MERGE_NOT_EXECUTED_CONFIRMED` | awaiting = `MERGE_OUTCOME(FAILURE)` | `MERGE_FAILED` | — |
| 40 | `MERGING` | `EV_MERGE_OUTCOME_UNKNOWN` | awaiting = `MERGE_OUTCOME(*)` | `MERGE_FAILED` | — |
| 41 | `MERGING` | `EV_USER_CANCEL_VERIFIED` | evidence検証済み | `MERGING` | `CMD_QUERY_MERGE_OUTCOME`; awaiting := `MERGE_OUTCOME(CANCEL)` |
| 42 | `MERGING` | `EV_RUN_FAILED` | — | `MERGING` | `CMD_QUERY_MERGE_OUTCOME`; awaiting := `MERGE_OUTCOME(FAILURE)` |
| 43 | `APPLYING_FIXES` | `EV_EXTERNAL_DEPENDENCY_VERIFIED` | evidence一致 | `BLOCKED` | —（`block := BlockContext(EXTERNAL_DEPENDENCY, binding = evidence binding, continuation = (APPLYING_FIXES, `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`, `HOST(APPLY_FINDINGS)`), evidence_ref)`。commandなし） |

**共通規則**（registry内で個別ruleへ展開され、一意性checkの対象になる）:

- **progress共通規則**（Section 3.4）: budget表の5 eventで`LIMIT_REACHED` / `NO_PROGRESS`の場合、`BLOCKED`へ遷移（`block := PROGRESS context`）、commandなし
- `EV_USER_CANCEL_VERIFIED`: terminalと`MERGING`（#41）を除く全stateで同一stateに留まり、`cancelling`設定 + `CMD_HALT_RUN(binding)`のみ発行、awaiting := `HALT_FOR_CANCEL`
- `EV_CANCELLATION_COMPLETED`: terminalと`MERGING`を除く全stateから`CANCELLED`へ。guard: `cancelling`保持中はbinding一致（不一致は構造化error）。`cancelling`なしは緊急停止経路のevidence（run / checkpointへのbind検証済み）のみ。全付随値を破棄
- `EV_RECORD_INTEGRITY_VIOLATION_DETECTED`: terminalを除く全state（`cancelling`中はlifecycle規則5が優先）から`BLOCKED`へ。`block := RECORD_INTEGRITY context`、`pending_record` / `awaiting`を破棄、commandなし
- `EV_RUN_FAILED`: terminal・`MERGING`（#42）・resumable stateを除くactive stateから`FAILED`へ（`recovery_to` := 進入元。`pending_record` / `awaiting` / `cancelling`引継）
- resumable state + `EV_RUN_FAILED`: 同一stateへ留まり全付随値を保持
- terminalは全eventを構造化errorで拒否。いずれにも一致しない`(state, event, guard値)`は未定義遷移として構造化errorで拒否（AC-C01-02）

**decision flowのGitHub会話順序**（#10 / #12 / #14〜#20）: (a) Claude draft投稿確認、(b) Codex verdict投稿確認、(c) Claudeの最終brief / decision record / revised draftの投稿確認、(d) 次のCodexまたはユーザー、の順にGitHub上へ両agentの発言が個別に現れる。

## 6. C-01のscope境界（Phase 1）

**実装する**: `domain/states.py`、`domain/events.py`、`domain/commands.py`、`domain/machine.py`（registry・`initialize(preflight_event)`・`transition(machine_state, event)`）、最小のvalue object（`MachineState` / `BlockContext` / `BlockedContinuation` / `CancelAttempt` / `PendingRecord` / `RecordEvidence` / `OpaqueBinding` / `Awaiting` / `Progress` / `Budget`）。

**実装しない（out of scope）**: GitHub APIアクセス・comment観測（C-05）/ actor認証・record chain・整合性検出・外部evidence検証・comment再利用拒否（C-06）/ checkpoint永続化・状態再構築・消費済みrecord管理（C-07）/ subprocess起動・signal処理・process停止の実行（C-03 / C-09）/ advance-submit engine・intent構造化・counter管理とprogress判定・limit引き上げ検証の実行（C-08 / C-10 / C-11）/ finding ledger本実装（Phase 10）とid・binding採番（C-08）/ CLI / Skill / wrapper。空moduleも作らない。

## 7. Test計画

- registryをdataとして全state × event × guard discriminator値の組合せをtable-drivenで検査（未定義は構造化error）
- **一意性とoverlap**: guard discriminator（awaiting 19値 + progress 3値 + block kind 3値 / reason 2値 + pending / cancelling / return_to / recovery_toの有無）の全値を展開し、一致rule数が常に0または1であることを検査。`recovery_to`と`block`の排他も検査
- 17 stateの到達可能性、到達不能stateの検出、遷移表・遷移図のsnapshot照合
- 純粋性: 同一入力の再適用で同一結果、入力非変更、I/O・時刻・乱数・環境変数への非依存、command列順序の決定性
- terminalからの全event拒否。resumableはresume registryのeventのみ受理
- 付随値が遷移ruleだけで設定され、eventから遷移先・command列を注入できないこと
- **binding**: PRODUCEDなしのVERIFIED、binding不一致、過去evidence再利用、pending中の他semantic event拒否。partial turnの再開
- **awaiting順序**: `CMD_EXECUTE_MERGE`が#34のみ、順序飛ばし・重複・不一致の拒否
- **bounded-progress**（AC-C01-09）: progress対象集合とbudget対応のregistry導出、既定3 round系列（二重計上なし）、5回目turn開始許可・6回目停止、共通counter（clarification 5 turn後のresubmit停止）、loop終了結果の正常処理
- **block解消gate**（AC-C01-11）: 単純resumeのcommandなし`BLOCKED`維持（3 kindすべて）。解消eventの**完全binding一致**（binding / reason / budget / snapshot / fingerprint / head）。**過去または別block向けの解消event、消費済みblockへのreplayの拒否**。`BLOCK_INTERVENTION`の**2経路（PowerShell転記 / GitHub直接comment）同値性**。fallbackの継続破棄 + fresh review
- **block体系**（AC-C01-12）: `EV_RECORD_INTEGRITY_VIOLATION_DETECTED`が全非terminal stateで`BLOCKED`へ入りpending / awaitingを破棄すること。`RECORD_INTEGRITY`が解消eventを受理せずfallbackのみで出られること。`EV_EXTERNAL_DEPENDENCY_VERIFIED` -> `BLOCKED` -> interventionでの継続再現
- **cancel / 緊急停止**（AC-C01-10）: 完了event前のterminal化・新agent起動の禁止。`cancelling`中のstale pending VERIFIED拒否。**全8 resumable stateでのcancel -> halt失敗 -> 別processからのresume系列**で`CMD_HALT_RUN(binding)`再発行だけが返ること（横断規則）。binding不一致完了eventの拒否。`MERGING`の照会経由
- **preflight NG**: `initialize(EV_PREFLIGHT_NG)`後の`FAILED`が`EV_RESUME_VALIDATED` / `EV_RESUME_FALLBACK_REQUIRED`を構造化errorで拒否すること（復帰は新runの`initialize`のみ）
- **user-input recordの2経路同値性**: 6種すべてで両経路が同一semantic遷移へ合流し、直接comment経路で`CMD_PERSIST_RECORD`が発行されないこと
- **resume系列（end-to-end）**: reporter retry、permission resume、pendingあり resume、resumable stateでの`EV_RUN_FAILED`保全
- decision flowの会話順序系列（#10→#14→#15→#16等）

## 8. 設計レビューで確定した判断

1. **Codex起動command**（round 1）: typed purpose方式。実行基盤はC-09へ集約
2. **CI code failure**（round 1）: `CHANGES_REQUESTED` + `CMD_INVALIDATE_APPROVALS`
3. **awaiting lifecycle**（round 2）: command -> expected resultをMachineState上で一意化
4. **user-input recordの2経路**（round 3）: PowerShell転記とGitHub直接commentの合流
5. **budget型bounded-progress**（round 3〜5）: loop継続遷移のみ判定、消費点の明示、共通5-turn counter、off-by-one禁止
6. **block体系**（round 4〜6で改訂）: `BlockContext`で`PROGRESS`（continuation + 理由）/ `EXTERNAL_DEPENDENCY`（continuation + 検出evidence）/ `RECORD_INTEGRITY`（continuationなし、fallbackのみ）を区別。単純resumeは常に`BLOCKED`維持。解消eventはblock attemptへの完全binding一致を要求し、interventionはuser-input record `BLOCK_INTERVENTION`（2経路）のcanonical検証で成立
7. **cancelの2系統・停止完了gate・attempt binding**（round 2〜6で改訂）: attempt bindingは`USER_CANCEL` recordのbindingを再利用。`cancelling`はstate分類より優先する横断resume規則を持ち、全stateで停止・checkpoint完了が他の再開に先行する
8. **preflight NGの復帰**（round 6）: 未開始の`FAILED`はresume系eventを拒否し、新しいrunの`initialize`（preflight再実行）だけを復帰経路とする
