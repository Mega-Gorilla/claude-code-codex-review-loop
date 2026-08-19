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

**registryの一意性不変条件**: guardは自由なpredicateではなく、**有限のtyped discriminator**（Section 2の`awaiting`値、`pending_record`のkind / binding一致、`progress`値、`block`のkind / reason、`cancelling` / `return_to` / `recovery_to` / `deferred_integrity`の有無とbinding一致）に限定する。到達可能なMachineState付随値の各組合せ × 各eventに対し、一致するruleは**0件または1件**である。共通規則（cancel / failure / progress / integrity等）はregistry内で個別ruleへ展開され、重複・overlapはdiscriminator全値の展開により機械的に検査してfailさせる。優先順位による解決は行わない（AC-C01-08）。

**付随値の組合せ不変条件**: `recovery_to`があるのは`FAILED`のみ、`block`があるのは`BLOCKED`とintegrity halt gate中（Section 3.5.1）のみ、`return_to`があるのは`AWAITING_TOOL_PERMISSION`（およびそこからのcancel / failure保全中）のみ、`deferred_integrity`があるのは`MERGING`のoutcome段階と`cancelling`中のみ。`recovery_to`と`block`は**排他**。`deferred_integrity`はblock化・terminal監査記録のいずれかで必ず消費され、silentに破棄されない。各遷移ruleは進入元固有のresume metadata（`recovery_to` / `return_to` / 旧`block`）を明示的に引き継ぐか破棄し、到達可能な全MachineStateでこの不変条件をtestで検査する。

## 2. MachineState

可視の17 stateとは別に、immutableな`MachineState`を導入する。

```text
MachineState（frozen）
  state: State                       # 可視の17 stateのいずれか
  return_to: State | None            # AWAITING_TOOL_PERMISSIONからの復帰先
  recovery_to: State | None          # EV_RUN_FAILEDで入ったFAILEDの安全な再開地点
  block: BlockContext | None         # BLOCKED（またはintegrity halt gate中）の停止理由・解消policy・（あれば）継続
  cancelling: CancelAttempt | None   # 進行中のcancel attempt（停止・checkpoint完了待ち）
  deferred_integrity: IntegrityEvidenceRef | None  # 検出済みだが処理を保留中のintegrity violation（Section 3.5.1）
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
  evidence: RecordRef | IntegrityEvidenceRef | None  # EXTERNAL_DEPENDENCYは検出record参照、RECORD_INTEGRITYは違反の記述

IntegrityEvidenceRef（frozen）
  binding: OpaqueBinding             # violation / block attemptのbinding。C-06が生成・検証し、同一違反の再検出は
                                     # 同じbinding（冪等に同一attempt）、別違反は別bindingになる
  # 参照可能なrecordが存在しない違反（削除・sequence gap）も表現できる一般形。
  # 404 / 欠落sequence位置 / 改変前後のbody hash等をopaqueに保持し、C-01は解釈しない

BlockResolutionEvidence（frozen）
  target_block_binding: OpaqueBinding  # 解除対象のblock attempt（canonical本文 / metadataからC-06が検証・抽出）
  record: RecordEvidence | None        # INTERVENTIONの場合、BLOCK_INTERVENTION record自身（record bindingは別値）
  reason / budget / counter_snapshot / fingerprint / head  # blockとの完全一致照合用

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

- 付随値は**registry内の遷移ruleだけが設定**し、eventから注入できない。`BlockedContinuation`のcommand列・awaitingはregistryの該当行から導出される有限集合であり、任意のcommand列を持ち込めない。binding / head / snapshot / fingerprintはevent evidence由来の監査値で、C-01は**等価比較のみ**を行う
- **bindingの役割分離**: recordのbinding（一意性・再利用防止）と、解除対象を指すbinding（`target_block_binding` / cancel attempt binding）は**別のfield**である。`BLOCK_INTERVENTION`のような解消recordは自身のrecord bindingを新規に持ち（経路1はC-08採番、経路2はC-06導出）、解除対象のblock attempt bindingはcanonical本文 / metadataに含めてC-06が検証する。cancel attemptのbindingは`USER_CANCEL` recordのbindingを再利用する。C-01は新たな採番をしない
- 数量判定・counterの管理はC-10 / C-11の責務であり、C-01は判定結果（`progress`）のみを受けて算術を行わない

### 2.1 Awaiting（有限のtyped discriminator、20値）

`awaiting`は「どのcommandを発行済みで、次にどの応答だけを受理するか」を表す。値は次の20値に限る。

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
| `MERGE_PRECONDITIONS` | `CMD_VERIFY_MERGE_PRECONDITIONS` | `EV_MERGE_PRECONDITIONS_OK` / `EV_MERGE_PRECONDITION_MISMATCH` / `EV_HEAD_CHANGED_EXTERNALLY` / `EV_RECORD_INTEGRITY_VIOLATION_DETECTED` |
| `MERGE_OUTCOME(EXECUTE)` | `CMD_EXECUTE_MERGE` | `EV_MERGE_CONFIRMED` / `EV_MERGE_OUTCOME_UNKNOWN` |
| `MERGE_OUTCOME(CANCEL)` | `CMD_QUERY_MERGE_OUTCOME`（cancel起点） | `EV_MERGE_CONFIRMED` / `EV_MERGE_NOT_EXECUTED_CONFIRMED` / `EV_MERGE_OUTCOME_UNKNOWN` |
| `MERGE_OUTCOME(FAILURE)` | `CMD_QUERY_MERGE_OUTCOME`（failure起点） | 同上 |
| `HALT_FOR_CANCEL` | `CMD_HALT_RUN(binding)` | `EV_CANCELLATION_COMPLETED`（`cancelling`とのbinding一致） |
| `HALT_FOR_BLOCK` | `CMD_HALT_RUN(binding)`（integrity halt gate） | `EV_BLOCK_HALT_COMPLETED`（`block.binding`一致） |
| `USER_INPUT(DECISION)` | —（進入ruleが設定） | user-input record `USER_DECISION`（両経路） |
| `USER_INPUT(GATE)` | —（進入ruleが設定） | user-input record `GATE_QUESTION` / `GATE_CHANGES` / `MERGE_APPROVAL`（両経路） |
| `USER_INPUT(PERMISSION)` | —（進入ruleが設定） | `EV_PERMISSION_RESUME_VALIDATED` |

**lifecycle規則**（AC-C01-08の核）:

1. 応答を要するcommandを発行する遷移ruleは、対応する`awaiting`値を**同一ruleで設定**する
2. 応答eventは、`awaiting`が当該応答を受理する値である場合**のみ**受理され、受理時に`awaiting`を**消費**または次の期待値へ**更新**する
3. `awaiting`不一致・消費済み再入力・順序飛ばしは**構造化errorで拒否**される
4. 例外として`awaiting`に関わらず受理されるのは、`EV_RUN_FAILED`（共通規則）、`USER_CANCEL`（Section 3.3）、`EV_CANCELLATION_COMPLETED`（binding guard）、`EV_RECORD_INTEGRITY_VIOLATION_DETECTED`（Section 3.5.1。`MERGING`はawaiting別の専用rule）、resume系のみ
5. **`cancelling`または`awaiting = HALT_FOR_BLOCK`の保持中**は、対応するhalt完了event（binding一致）と`EV_RUN_FAILED`、および`EV_RECORD_INTEGRITY_VIOLATION_DETECTED`（`deferred_integrity`への記録と承認失効のみ行い、状態を変えない。Section 3.5.1）以外の全semantic eventを拒否する。**この間の`EV_RUN_FAILED`と明示resumeは状態・付随値を維持し、`CMD_HALT_RUN(binding)`の再発行だけを返す**（Section 4.1の横断規則）

## 3. Record体系

### 3.1 内部record（Controller / agentが生成し、Controllerが投稿する）

1. `EV_*_PRODUCED(kind, binding)`: **許可source state（Section 3.2）かつ`awaiting`一致**の場合のみ受理。`awaiting`を消費し、`pending_record`を設定して`CMD_PERSIST_RECORD(kind, binding)`を返す。状態は変えない
2. `EV_*_VERIFIED(evidence)`: **`pending_record`とkindおよびbindingが一致する場合だけ**受理され、`pending_record`を消費して状態を進め、次のcommandと`awaiting`を設定する

対応する`PRODUCED`を経ない`VERIFIED`、過去evidence再利用、別turn流用はbinding不一致として拒否（AC-C01-03）。`pending_record`保持中は、対応する`VERIFIED`・`EV_RUN_FAILED`・外部経路の`EV_USER_CANCEL_VERIFIED`・`EV_RECORD_INTEGRITY_VIOLATION_DETECTED`以外のsemantic eventを拒否する（内部経路の`USER_CANCEL` / `BLOCK_INTERVENTION`のPRODUCEDはpending slotが空くまでC-08が保留）。partial turnは`pending_record`ごとcheckpointから再開できる。`CMD_PERSIST_RECORD`は**冪等**であることをC-05へ要求する。

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
| `EXTERNAL_DEPENDENCY` | `APPLYING_FIXES` | `HOST(APPLY_FINDINGS)` | `EV_EXTERNAL_DEPENDENCY_VERIFIED` |
| `PERMISSION_BLOCK` | `RUNNING_REVIEW` / `APPLYING_FIXES` | `CODEX(CODE_REVIEW)` / `HOST(APPLY_FINDINGS)` | `EV_TOOL_PERMISSION_BLOCKED` |
| `CI_TIMEOUT` | `WAITING_CI` | `CI_RESULT` | `EV_CI_TIMEOUT_RECORDED` |
| `CI_CODE_FAILURE` | `WAITING_CI` | `CI_RESULT` | `EV_CI_CODE_FAILURE_VERIFIED` |
| `FINAL_REPORT` | `GENERATING_REPORT` | `REPORT` | `EV_REPORT_VERIFIED` |
| `GATE_ANSWER` | `READY_FOR_HUMAN_MERGE` | `HOST(ANSWER_GATE_QUESTION)` | `EV_GATE_ANSWER_VERIFIED` |

### 3.3 user-input record（6種、2経路）

**経路1 — PowerShell / Skill入力（主経路）**: C-08がintentへ構造化し、内部recordと同じ`PRODUCED -> CMD_PERSIST_RECORD -> VERIFIED`でGitHubへ転記・確認する。record bindingはC-08が採番し、evidenceはactor・input route・対象head・intentを保持する。

**経路2 — GitHub直接comment**: 既に永続化済みのため`CMD_PERSIST_RECORD`を通さない。C-05が観測 -> C-06がcomment ID・body hash・actor（allowlist完全一致、D-031、fail closed）・対象headを検証してtyped external evidenceを生成 -> C-01は`VERIFIED` eventとして直接受理。record bindingはC-06がcomment参照から導出し、消費済みcomment IDの再提示はC-06 / C-07が拒否する。

**両経路は同一の`EV_*_VERIFIED` semantic eventへ合流**する。`BLOCK_INTERVENTION`のcanonical本文 / metadataは**解除対象のblock attempt binding（`target_block_binding`）を含み**、C-06が現在のblockへの参照として検証してからC-01へ渡す（record自身のbindingとは別。Section 3.5）。

| RecordKind | 受理state | 受理guard | VERIFIED event |
| --- | --- | --- | --- |
| `USER_DECISION` | `AWAITING_USER_DECISION` | awaiting = `USER_INPUT(DECISION)` | `EV_USER_DECISION_VERIFIED` |
| `GATE_QUESTION` | `READY_FOR_HUMAN_MERGE` | awaiting = `USER_INPUT(GATE)` | `EV_GATE_QUESTION_VERIFIED` |
| `GATE_CHANGES` | `READY_FOR_HUMAN_MERGE` | awaiting = `USER_INPUT(GATE)` | `EV_GATE_CHANGES_VERIFIED` |
| `MERGE_APPROVAL` | `READY_FOR_HUMAN_MERGE` | awaiting = `USER_INPUT(GATE)` | `EV_MERGE_APPROVAL_VERIFIED` |
| `BLOCK_INTERVENTION` | `BLOCKED` | `block.kind`が解消を許可（Section 3.5の解消matrix） | `EV_BLOCK_RESOLVED_INTERVENTION` |
| `USER_CANCEL` | terminal以外の全state | 不問（Section 3.1の規則に従う） | `EV_USER_CANCEL_VERIFIED` |

（`USER_CANCEL` / `BLOCK_INTERVENTION`のPRODUCEDは`awaiting`不問だが`pending_record`が空であることを要求し、`awaiting`を消費せず維持する）

### 3.4 progress discriminatorとbudget（bounded-progress判定）

上限・膠着の判定はC-10 / C-11が行い、**同じbounded loopをもう1回継続する遷移だけ**に`progress ∈ {CONTINUE, LIMIT_REACHED, NO_PROGRESS}`を付与して入力する。**loopを終了する結果は上限turnで得られたものでも常に処理する**。counterの消費点と判定点はregistryのdataとして明示し、二重計上を防ぐ。

| Budget | counterを消費する遷移（新しいloop単位の開始） | 判定のみ（消費しない） | 境界のsemantics |
| --- | --- | --- | --- |
| `REVIEW_ROUND` | `EV_REVIEW_BLOCKING_VERIFIED`、`EV_CI_CODE_FAILURE_VERIFIED` | `EV_FIX_RESULT_VERIFIED`（同一round完了。二重計上しない。`NO_PROGRESS`判定のみ） | 1 round = review -> fix -> re-reviewの一巡。既定3 roundの系列testで境界を固定 |
| `CLARIFICATION_TURN` | `EV_CLARIFICATION_QUESTION_VERIFIED`、`EV_VERDICT_RESUBMIT_VERIFIED`（同一fingerprintで共通counter） | — | 1 turn = 質問／再提出と回答の一往復。**5回目のturn開始を許可し、6回目の開始を`LIMIT_REACHED`とする** |

**progress共通規則**: `CONTINUE`のみ表の遷移とcommand発行。`LIMIT_REACHED` / `NO_PROGRESS`は`pending_record`消費のうえ`BLOCKED`へ遷移し、`block := BlockContext(PROGRESS, binding = 当該evidence binding, head, continuation = 当該行のTo / Commands / awaiting, reason / budget / snapshot / fingerprint)`を保存して**commandを発行しない**（AC-C01-09）。進入時に`recovery_to` / `return_to`は保持しない（元より非設定）。

### 3.5 Block体系（BLOCKEDの3種と解消matrix）

`BLOCKED`は常に`block: BlockContext`を保持する。**いずれのkindでも、単純resume（`EV_RESUME_VALIDATED`）は継続を再現せず、`BLOCKED`を維持してcommandを発行しない**。

| kind | 進入 | continuation | 解消 |
| --- | --- | --- | --- |
| `PROGRESS` | progress共通規則 | あり | `EV_BLOCK_RESOLVED_LIMIT_RAISED`（reason = LIMIT_REACHED）/ `EV_BLOCK_RESOLVED_INTERVENTION`（reason = NO_PROGRESS）/ generic fallback |
| `EXTERNAL_DEPENDENCY` | `EV_EXTERNAL_DEPENDENCY_VERIFIED`（#43） | あり | `EV_BLOCK_RESOLVED_INTERVENTION` / generic fallback |
| `RECORD_INTEGRITY` | Section 3.5.1 | **なし** | **専用evidenceのみ**: `EV_INTEGRITY_RESTORED_VALIDATED` / `EV_INTEGRITY_SALVAGE_ESTABLISHED`。**generic fallback（`EV_RESUME_FALLBACK_REQUIRED`）は受理しない**（fail closed） |

**解消evidenceのbinding**（AC-C01-11）: 解消eventのevidenceは`BlockResolutionEvidence`であり、**`target_block_binding`（解除対象のblock attempt）と`block.binding`の一致**、およびreason / budget / counter snapshot / fingerprint / headの完全一致をC-01の有限guardとして要求する（該当しないkindのfieldはNoneどうしの一致）。`BLOCK_INTERVENTION`の場合、**record自身のbinding（一意性・再利用防止）とtarget_block_bindingは別のfield**であり、record再利用はrecord規約（C-06 / C-07の消費済み管理）が拒否し、対象不一致はC-01が拒否する。過去または別block向けの解消event、消費済みblockへのreplayは構造化errorで拒否する。

`RECORD_INTEGRITY`の出口は、C-06 / C-07が検証した専用evidenceに限る:

- `EV_INTEGRITY_RESTORED_VALIDATED`: canonical chainの整合性が復元され、**同一chainを再検証できた**（target_block_binding一致） -> `RUNNING_REVIEW` + `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`
- `EV_INTEGRITY_SALVAGE_ESTABLISHED`: **明示的なsalvage手順で旧chainと承認を破棄し、安全な新しいbaselineを確立した**（target_block_binding一致。手順の提供はC-07 / Phase 14。それまで本eventの供給源は存在せず、fail closedのまま`BLOCKED`が維持される） -> 同上（新baseline上のfresh review）

未修復の同一chainからfresh reviewを起動できないことをnegative testで固定する（AC-C01-12）。

### 3.5.1 RECORD_INTEGRITYの進入（検出時の安全規則）

`EV_RECORD_INTEGRITY_VIOLATION_DETECTED(evidence: IntegrityEvidenceRef)`はC-06が発する（AC-C06-06〜08: canonical commentの改変・削除・sequence gap）。`evidence.binding`がviolation / block attemptのbindingになる（同一違反の再検出は同じbindingで冪等、別違反は別binding）。**受理する全経路で`CMD_INVALIDATE_APPROVALS`（冪等）を即時発行**し、「改変された決定は失効する」契約を遅延させない。進入はstateの種類で分かれ、merge安全とprocess停止を優先する:

1. **`MERGING`でmerge実行後または成否不明（awaiting = `MERGE_OUTCOME(*)`）**: `BLOCKED`にしない。`MERGING`と`awaiting`を維持し、**`deferred_integrity := evidence`を保存**して`CMD_INVALIDATE_APPROVALS`と`CMD_QUERY_MERGE_OUTCOME`を発行する（**outcome確定が最優先**）。以後のoutcome確定ruleは`deferred_integrity`をguardにSection 3.5.2で処理し、**外部componentによるterminal後の再入力には依存しない**
2. **`MERGING`のpreconditions段階（awaiting = `MERGE_PRECONDITIONS`）**: mergeを実行せず安全停止する。`BLOCKED`へ遷移し、`block := RECORD_INTEGRITY context`、`CMD_INVALIDATE_APPROVALS`を発行
3. **active state（`MERGING`を除く6 state）**: **halt gateを経由**する。状態を維持したまま`block := RECORD_INTEGRITY context`を設定し、`pending_record` / 旧`awaiting`を破棄して`CMD_INVALIDATE_APPROVALS`と`CMD_HALT_RUN(block.binding)`を発行、`awaiting := HALT_FOR_BLOCK`。binding一致の`EV_BLOCK_HALT_COMPLETED`で`BLOCKED`へ遷移する
4. **resumable state（8 state、`cancelling`なし）**: agent processを伴わないため直接`BLOCKED`へ遷移し、`CMD_INVALIDATE_APPROVALS`を発行する
5. **`cancelling`中（stateを問わず）**: 状態と停止処理を変えず、**`deferred_integrity := evidence`の記録と`CMD_INVALIDATE_APPROVALS`のみ**を行う。処理はSection 3.5.2のcancel完了ruleが引き継ぐ

**いずれの進入でも、`recovery_to` / `return_to` / 旧`block`を破棄**し（1と5は現在の処理を維持するため破棄せず`deferred_integrity`のみ追加）、付随値の組合せ不変条件（Section 1）を保つ。`deferred_integrity`保持中に同一bindingの再検出が来た場合は冪等に維持し、別bindingの新violationは`CMD_RECORD_INTEGRITY_INCIDENT`で即時監査記録して先着の`deferred_integrity`を維持する。

### 3.5.2 deferred_integrityの終端処理（検出済み違反をsilentに失わない）

`deferred_integrity`保持中のoutcome / cancel確定ruleは、通常のruleとguardで分離され、次を一意に決める。

- **merge完了（`EV_MERGE_CONFIRMED`、deferredあり）**: mergeは実際に完了しているため`MERGED`を偽らない。`MERGED`へ遷移し、**`CMD_RECORD_INTEGRITY_INCIDENT(evidence)`（差分提示と監査記録。C-06 / C-07）と`CMD_INVALIDATE_APPROVALS`をterminal遷移のcommand列に含めて**発行する（terminal後のevent再入力に依存しない）
- **merge未実行確認・不明（`EV_MERGE_NOT_EXECUTED_CONFIRMED`（failure起点）/ `EV_MERGE_OUTCOME_UNKNOWN`、deferredあり）**: `MERGE_FAILED`ではなく**`BLOCKED`（`block := RECORD_INTEGRITY context`、binding = deferred evidence）**へ遷移し、`CMD_RECORD_INTEGRITY_INCIDENT`を発行する。これにより`MERGE_FAILED`の通常resume（`EV_RESUME_SAME_HEAD_VALIDATED`）でintegrity gateを迂回できない
- **cancel起点のmerge未実行確認（deferredあり）**: ユーザーのcancel意思を尊重して`CANCELLED`へ遷移するが、**`CMD_RECORD_INTEGRITY_INCIDENT`と`CMD_INVALIDATE_APPROVALS`を伴い**、検出済み違反を監査記録へ残す（新runのpreflight / chain検証が違反に直面する）
- **cancel停止完了（`EV_CANCELLATION_COMPLETED`、deferredあり）**: 同上 — `CANCELLED`へ遷移するが、incident記録と承認失効を伴う。**検出済みintegrityを破棄して無条件に`CANCELLED`へ進むことはない**

`deferred_integrity`はこれらのruleで必ず消費される（block化またはterminal監査記録）。

### 3.6 cancelの2系統（いずれも停止完了後にのみCANCELLEDへ入る）

- **対話cancel**: `EV_USER_CANCEL_VERIFIED`は同一stateに留まり、`cancelling := CancelAttempt(binding)`（`USER_CANCEL` recordのbindingを再利用）を設定して`CMD_HALT_RUN(binding)`だけを発行し、`awaiting := HALT_FOR_CANCEL`とする。binding一致の`EV_CANCELLATION_COMPLETED`で初めて`CANCELLED`へ遷移する。processが無ければC-08が完了eventを即時返す。`MERGING`のみ結果照会を優先する（#41）
- **緊急停止（Ctrl+C等）**: C-03 / C-08が停止・checkpointを行い、**run / checkpointへのbindをC-07 / C-08が検証した**`EV_CANCELLATION_COMPLETED`を直接入力する

cancel中の安全規則: `cancelling`保持中はbinding一致の完了eventと`EV_RUN_FAILED`以外の全semantic eventを拒否し、stale `pending_record`はsemantic継続に使わない（監査保持、`CANCELLED`で破棄）。**stateの分類に関わらず、明示resumeは`CMD_HALT_RUN(binding)`の再発行だけを返す**（横断規則）。binding不一致の完了eventは拒否する。`CANCELLED`は現在runのterminalで、resumeは直前の安全なcheckpointから新しいrunとして開始する。

### 3.7 record以外のevent

| Event | 意味 | 発生元 |
| --- | --- | --- |
| `EV_PREFLIGHT_OK` / `EV_PREFLIGHT_NG` | preflight検証結果（`initialize`専用） | C-07 / C-08 |
| `EV_FIX_STARTED` | hostがfinding対応へ着手 | C-08 |
| `EV_PERMISSION_RESUME_VALIDATED` | Permission IDとheadの再検証を伴う明示resume | C-08 |
| `EV_CI_SUCCEEDED` / `EV_CI_INFRA_FAILURE` | 対象headのCI結果 | C-12 |
| `EV_CI_RESUME_REQUESTED` | `WAITING_CI`からの明示resume | C-08 |
| `EV_REPORT_FAILED` / `EV_REPORTER_RETRY_REQUESTED` | report失敗 / 再実行指示 | C-12 / C-08 |
| `EV_MERGE_PRECONDITIONS_OK` / `EV_MERGE_PRECONDITION_MISMATCH` | merge直前再検証の結果 | C-13 |
| `EV_MERGE_CONFIRMED` / `EV_MERGE_NOT_EXECUTED_CONFIRMED` / `EV_MERGE_OUTCOME_UNKNOWN` | merge結果照会 | C-13 |
| `EV_HEAD_CHANGED_EXTERNALLY` | 外部からのhead更新を検出 | C-07 |
| `EV_CANCELLATION_COMPLETED` | cancel時のprocess停止とcheckpoint保存の完了（binding付き） | C-08 |
| `EV_BLOCK_HALT_COMPLETED` | integrity halt gateのprocess停止とcheckpoint保存の完了（`block.binding`一致） | C-08 |
| `EV_RECORD_INTEGRITY_VIOLATION_DETECTED` | canonical commentの改変・削除・sequence gapの検出（violation bindingを含む`IntegrityEvidenceRef`付き） | C-06 |
| `EV_BLOCK_RESOLVED_LIMIT_RAISED` | limit設定がsnapshot超に引き上げられたことの検証（`BlockResolutionEvidence`） | C-10 / C-11 |
| `EV_BLOCK_RESOLVED_INTERVENTION` | user-input record `BLOCK_INTERVENTION`のcanonical検証（`BlockResolutionEvidence`） | C-06 / C-11 |
| `EV_INTEGRITY_RESTORED_VALIDATED` | canonical chainの整合性復元と同一chainの再検証（`BlockResolutionEvidence`） | C-06 / C-07 |
| `EV_INTEGRITY_SALVAGE_ESTABLISHED` | 明示salvageによる新baseline確立の検証（`BlockResolutionEvidence`。供給はPhase 14以降） | C-07 |
| `EV_RUN_FAILED` | bounded retry後の失敗 | 各層 |
| `EV_RESUME_VALIDATED` / `EV_RESUME_FALLBACK_REQUIRED` | resume preflightの結果 | C-07 |
| `EV_RESUME_SAME_HEAD_VALIDATED` | merge失敗後の同一head・全条件再確認 | C-07 / C-13 |

### 3.8 Command一覧

| Command | 意味 | 実行component |
| --- | --- | --- |
| `CMD_PERSIST_RECORD(kind, binding)` | canonical recordの投稿と検証（冪等） | C-05 / C-06 |
| `CMD_REQUEST_CODEX_REVIEW(purpose)` | fresh reviewerの起動 | C-09 |
| `CMD_REQUEST_HOST_ACTION(kind)` | active hostへの作業依頼 | C-08 |
| `CMD_CHECK_CI` / `CMD_GENERATE_REPORT` | CI確認 / reporter起動 | C-12 |
| `CMD_HALT_RUN(binding)` | active process treeの停止とcheckpoint保存（cancel attemptまたはblock attemptへbind） | C-03 / C-08 |
| `CMD_VERIFY_MERGE_PRECONDITIONS` / `CMD_QUERY_MERGE_OUTCOME` | merge直前再検証 / 結果照会 | C-13 |
| `CMD_EXECUTE_MERGE` | **`awaiting = MERGE_PRECONDITIONS`の消費を伴うSection 5の#34でのみ発行される**merge実行 | C-13 |
| `CMD_INVALIDATE_APPROVALS` | review / merge承認の失効（**冪等**であることをC-07へ要求する） | C-07 |
| `CMD_RECORD_INTEGRITY_INCIDENT(evidence)` | integrity violationの差分提示と監査記録 | C-06 / C-07 |

commandは記述のみでC-01は実行しない。command列の順序は決定論的で、条件分岐の意味は無い。

## 4. 分類とresume registry

| 分類 | State |
| --- | --- |
| terminal | `MERGED`、`CANCELLED`（全event拒否） |
| resumable | `WAITING_CI`、`AWAITING_USER_DECISION`、`AWAITING_TOOL_PERMISSION`、`READY_FOR_HUMAN_MERGE`、`BLOCKED`、`FAILED`、`REPORT_FAILED`、`MERGE_FAILED` |
| active | 残りの7 state |

### 4.1 resume protocol

**横断規則（最優先）**: `cancelling`または`awaiting = HALT_FOR_BLOCK`の保持中は、stateの分類に関わらず（terminal / `MERGING`を除く）、明示resumeが**`CMD_HALT_RUN(binding)`の再発行だけ**を返し、状態と付随値を維持する。停止・checkpoint完了が他のあらゆる再開に先行する。

**`FAILED`のresume**（`EV_RESUME_VALIDATED`。横断規則の次に優先順位で決まる）:

1. `pending_record`あり: `source_state`へ戻り、同一bindingの`CMD_PERSIST_RECORD`再発行のみ
2. `awaiting`あり: 復帰先へ戻り、`awaiting`対応commandを再発行（Section 2.1）
3. `recovery_to`あり: 駆動command表（`RUNNING_REVIEW` -> CODE_REVIEW review / `CHANGES_REQUESTED`・`APPLYING_FIXES` -> APPLY_FINDINGS / `CLARIFYING_REVIEW` -> CLARIFICATION / `REVIEWING_DECISION_REQUEST` -> DECISION_VERDICT / `GENERATING_REPORT` -> report。それぞれ対応するawaitingを設定）
4. いずれも無い（preflight NGの未開始`FAILED`）: `EV_RESUME_VALIDATED` / `EV_RESUME_FALLBACK_REQUIRED`を**構造化errorで拒否**。復帰は新しいrunの`initialize`のみ

**`BLOCKED`のresume（解消gate）**: Section 3.5の解消matrixに従う。単純resumeは`BLOCKED`維持・commandなし。解消eventは`BlockResolutionEvidence`の**完全binding一致**で受理され、`continuation`があれば1回だけ再現して`block`と不要なresume metadataを消去する。`RECORD_INTEGRITY`はgeneric fallbackを受理せず、専用evidence（`EV_INTEGRITY_RESTORED_VALIDATED` / `EV_INTEGRITY_SALVAGE_ESTABLISHED`）のみで出られる。`PROGRESS` / `EXTERNAL_DEPENDENCY`のgeneric fallback（`EV_RESUME_FALLBACK_REQUIRED`）は継続を破棄し、`CMD_INVALIDATE_APPROVALS` + fresh reviewへ入る。

`recovery_to`は`EV_RUN_FAILED`による`FAILED`進入ruleだけが、`block`は`BLOCKED`進入rule（およびintegrity halt gate）だけが設定する。**両者は排他**であり、integrity進入時は旧`recovery_to` / `return_to` / 旧`block`を破棄する（Section 3.5.1）。

### 4.2 resume registry

| From | Event | Guard | To | Commands / awaiting更新 |
| --- | --- | --- | --- | --- |
| terminal / `MERGING`以外の全state | 明示resume | **`cancelling`または`HALT_FOR_BLOCK`あり** | 同一state | `CMD_HALT_RUN(binding)`再発行のみ（横断規則） |
| `FAILED` | `EV_RESUME_VALIDATED` | `cancelling`なし、優先順位1〜3該当 | Section 4.1 | Section 4.1 |
| `FAILED` | `EV_RESUME_VALIDATED` / `EV_RESUME_FALLBACK_REQUIRED` | 付随値なし（preflight NG） | —（拒否） | 構造化error。復帰は新runの`initialize`のみ |
| `FAILED` | `EV_RESUME_FALLBACK_REQUIRED` | `cancelling`なし、付随値あり | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)`（pending / 旧awaiting破棄） |
| `BLOCKED` | `EV_BLOCK_RESOLVED_LIMIT_RAISED` | kind = PROGRESS、reason = LIMIT_REACHED、**完全binding一致** | `continuation.resume_state` | 保存command列とawaitingを1回再現し`block`消去 |
| `BLOCKED` | `EV_BLOCK_RESOLVED_INTERVENTION` | （PROGRESS ∧ NO_PROGRESS）∨ EXTERNAL_DEPENDENCY、**完全binding一致** | 同上 | 同上 |
| `BLOCKED` | `EV_INTEGRITY_RESTORED_VALIDATED` / `EV_INTEGRITY_SALVAGE_ESTABLISHED` | kind = RECORD_INTEGRITY、**target_block_binding一致** | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)`（`block`消去） |
| `BLOCKED` | `EV_RESUME_VALIDATED` | `cancelling`なし | `BLOCKED` | —（停止理由と解消経路の提示のみ） |
| `BLOCKED` | `EV_RESUME_FALLBACK_REQUIRED` | kind ∈ {PROGRESS, EXTERNAL_DEPENDENCY} | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)`（`block`破棄） |
| `BLOCKED` | `EV_RESUME_FALLBACK_REQUIRED` | **kind = RECORD_INTEGRITY** | —（拒否） | 構造化error（fail closed。専用evidenceのみが出口） |
| `REPORT_FAILED` | `EV_REPORTER_RETRY_REQUESTED` | `cancelling`なし | `GENERATING_REPORT` | `CMD_GENERATE_REPORT`; awaiting := `REPORT` |
| `MERGE_FAILED` | `EV_RESUME_SAME_HEAD_VALIDATED` | `cancelling`なし | `READY_FOR_HUMAN_MERGE` | —; awaiting := `USER_INPUT(GATE)` |
| `MERGE_FAILED` | `EV_HEAD_CHANGED_EXTERNALLY` | — | `RUNNING_REVIEW` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| `WAITING_CI` | `EV_CI_RESUME_REQUESTED` | `cancelling`なし | `WAITING_CI` | `CMD_CHECK_CI`; awaiting := `CI_RESULT` |
| `AWAITING_TOOL_PERMISSION` | `EV_PERMISSION_RESUME_VALIDATED` | `cancelling`なし、awaiting = `USER_INPUT(PERMISSION)`、`return_to`あり | `return_to` | awaiting消費 + `return_to`対応の駆動command + 次のawaiting（`return_to`消去） |
| `AWAITING_USER_DECISION` / `READY_FOR_HUMAN_MERGE` | user-input record | 各guard | Section 5 | 通常eventがresumeを兼ねる |

**resumable stateの保全**: 共通`EV_RUN_FAILED`はresumable stateには適用せず、同一stateに留まり全付随値を保持する。`FAILED`進入rule（`EV_RUN_FAILED`）は`recovery_to` := 進入元を設定し、`pending_record` / `awaiting` / `cancelling`を引き継ぐ。

## 5. 完全遷移表

`VERIFIED`のGuard列には`pending_record`一致または外部evidence検証済みが暗黙に含まれる。`PRODUCED`は表から省略（Section 3.2 / 3.3）。「CONTINUE（budget）」はprogress guard。

| # | From | Event | Guard | To | Commands / awaiting更新 |
| --- | --- | --- | --- | --- | --- |
| 1 | （`initialize`） | `EV_PREFLIGHT_OK` | — | `RUNNING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CODE_REVIEW)`; awaiting := `CODEX(CODE_REVIEW)` |
| 2 | （`initialize`） | `EV_PREFLIGHT_NG` | — | `FAILED` | —（付随値なし。resume系拒否、復帰は新runの`initialize`のみ） |
| 3 | `RUNNING_REVIEW` | `EV_REVIEW_BLOCKING_VERIFIED` | evidence一致、CONTINUE（REVIEW_ROUND消費） | `CHANGES_REQUESTED` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 4 | `RUNNING_REVIEW` | `EV_REVIEW_APPROVED_VERIFIED` | evidence一致 | `WAITING_CI` | `CMD_CHECK_CI`; awaiting := `CI_RESULT` |
| 5 | `RUNNING_REVIEW` | `EV_TOOL_PERMISSION_BLOCKED` | evidence一致 | `AWAITING_TOOL_PERMISSION` | —（`return_to := RUNNING_REVIEW`; awaiting := `USER_INPUT(PERMISSION)`） |
| 6 | `CHANGES_REQUESTED` | `EV_FIX_STARTED` | awaiting = `HOST(APPLY_FINDINGS)` | `APPLYING_FIXES` | —（awaiting維持） |
| 7 | `CHANGES_REQUESTED` | `EV_CLARIFICATION_QUESTION_VERIFIED` | evidence一致、CONTINUE（CLARIFICATION_TURN消費） | `CLARIFYING_REVIEW` | `CMD_REQUEST_CODEX_REVIEW(CLARIFICATION)`; awaiting := `CODEX(CLARIFICATION)` |
| 8 | `CLARIFYING_REVIEW` | `EV_CLARIFICATION_CONFIRMED_VERIFIED` / `EV_CLARIFICATION_REVISED_VERIFIED` | evidence一致 | `CHANGES_REQUESTED` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)`（loop終了結果） |
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
| 19 | `REVIEWING_DECISION_REQUEST` | `EV_VERDICT_RESUBMIT_VERIFIED` | evidence一致、CONTINUE（CLARIFICATION_TURN消費。共通counter） | `REVIEWING_DECISION_REQUEST` | `CMD_REQUEST_HOST_ACTION(REVISE_DECISION_REQUEST)`; awaiting := `HOST(REVISE_DECISION_REQUEST)` |
| 20 | `AWAITING_USER_DECISION` | `EV_USER_DECISION_VERIFIED` | awaiting = `USER_INPUT(DECISION)` | `APPLYING_FIXES` | `CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 21 | `AWAITING_TOOL_PERMISSION` | `EV_PERMISSION_RESUME_VALIDATED` | awaiting = `USER_INPUT(PERMISSION)`、`return_to`あり | `return_to` | Section 4.2の同名rule |
| 22 | `WAITING_CI` | `EV_CI_SUCCEEDED` | awaiting = `CI_RESULT` | `GENERATING_REPORT` | `CMD_GENERATE_REPORT`; awaiting := `REPORT` |
| 23 | `WAITING_CI` | `EV_CI_CODE_FAILURE_VERIFIED` | evidence一致、CONTINUE（REVIEW_ROUND消費） | `CHANGES_REQUESTED` | `CMD_INVALIDATE_APPROVALS`、`CMD_REQUEST_HOST_ACTION(APPLY_FINDINGS)`; awaiting := `HOST(APPLY_FINDINGS)` |
| 24 | `WAITING_CI` | `EV_CI_INFRA_FAILURE` | awaiting = `CI_RESULT` | `WAITING_CI` | `CMD_CHECK_CI`（awaiting維持） |
| 25 | `WAITING_CI` | `EV_CI_TIMEOUT_RECORDED` | evidence一致 | `WAITING_CI` | —; awaiting := なし |
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
| 37 | `MERGING` | `EV_MERGE_CONFIRMED` | awaiting = `MERGE_OUTCOME(*)`、deferredなし | `MERGED` | — |
| 38 | `MERGING` | `EV_MERGE_NOT_EXECUTED_CONFIRMED` | awaiting = `MERGE_OUTCOME(CANCEL)`、deferredなし | `CANCELLED` | — |
| 39 | `MERGING` | `EV_MERGE_NOT_EXECUTED_CONFIRMED` | awaiting = `MERGE_OUTCOME(FAILURE)`、deferredなし | `MERGE_FAILED` | — |
| 40 | `MERGING` | `EV_MERGE_OUTCOME_UNKNOWN` | awaiting = `MERGE_OUTCOME(*)`、deferredなし | `MERGE_FAILED` | — |
| 41 | `MERGING` | `EV_USER_CANCEL_VERIFIED` | evidence検証済み | `MERGING` | `CMD_QUERY_MERGE_OUTCOME`; awaiting := `MERGE_OUTCOME(CANCEL)` |
| 42 | `MERGING` | `EV_RUN_FAILED` | — | `MERGING` | `CMD_QUERY_MERGE_OUTCOME`; awaiting := `MERGE_OUTCOME(FAILURE)` |
| 43 | `APPLYING_FIXES` | `EV_EXTERNAL_DEPENDENCY_VERIFIED` | evidence一致 | `BLOCKED` | —（`block := EXTERNAL_DEPENDENCY context`、continuation = (APPLYING_FIXES, APPLY_FINDINGS依頼, `HOST(APPLY_FINDINGS)`)） |
| 44 | `MERGING` | `EV_RECORD_INTEGRITY_VIOLATION_DETECTED` | awaiting = `MERGE_PRECONDITIONS` | `BLOCKED` | `CMD_INVALIDATE_APPROVALS`（`block := RECORD_INTEGRITY context`。merge未実行で安全停止） |
| 45 | `MERGING` | `EV_RECORD_INTEGRITY_VIOLATION_DETECTED` | awaiting = `MERGE_OUTCOME(*)` | `MERGING` | `CMD_INVALIDATE_APPROVALS`、`CMD_QUERY_MERGE_OUTCOME`（awaiting維持。**deferred_integrity := evidence**。outcome確定はSection 3.5.2のdeferred付きruleが処理） |
| 46 | `MERGING` | `EV_MERGE_CONFIRMED` | awaiting = `MERGE_OUTCOME(*)`、**deferredあり** | `MERGED` | `CMD_RECORD_INTEGRITY_INCIDENT(evidence)`、`CMD_INVALIDATE_APPROVALS`（mergeの完了は偽らず、監査記録と失効を伴って終了） |
| 47 | `MERGING` | `EV_MERGE_NOT_EXECUTED_CONFIRMED` | awaiting = `MERGE_OUTCOME(CANCEL)`、**deferredあり** | `CANCELLED` | `CMD_RECORD_INTEGRITY_INCIDENT(evidence)`、`CMD_INVALIDATE_APPROVALS` |
| 48 | `MERGING` | `EV_MERGE_NOT_EXECUTED_CONFIRMED` | awaiting = `MERGE_OUTCOME(FAILURE)`、**deferredあり** | `BLOCKED` | `CMD_RECORD_INTEGRITY_INCIDENT(evidence)`（`block := RECORD_INTEGRITY context`（binding = deferred evidence）。`MERGE_FAILED`の通常resumeでintegrity gateを迂回させない） |
| 49 | `MERGING` | `EV_MERGE_OUTCOME_UNKNOWN` | awaiting = `MERGE_OUTCOME(*)`、**deferredあり** | `BLOCKED` | `CMD_RECORD_INTEGRITY_INCIDENT(evidence)`（同上） |

**共通規則**（registry内で個別ruleへ展開）:

- **progress共通規則**: budget表の5 eventで`LIMIT_REACHED` / `NO_PROGRESS` -> `BLOCKED`（`block := PROGRESS context`）、commandなし
- **integrity共通規則**（Section 3.5.1）: `EV_RECORD_INTEGRITY_VIOLATION_DETECTED`は**全経路で`CMD_INVALIDATE_APPROVALS`（冪等）を即時発行**する。active state（`MERGING`除く6 state）では状態維持 + `block`設定 + pending / 旧awaiting破棄 + `CMD_HALT_RUN`（awaiting := `HALT_FOR_BLOCK`）。resumable state（`cancelling`なし）では直接`BLOCKED`。いずれも`recovery_to` / `return_to` / 旧`block`を破棄。`MERGING`は#44 / #45。`cancelling`中は`deferred_integrity`への記録と失効のみ（状態不変）
- `EV_BLOCK_HALT_COMPLETED`: awaiting = `HALT_FOR_BLOCK`かつ`block.binding`一致で`BLOCKED`へ（不一致は構造化error）
- `EV_USER_CANCEL_VERIFIED`: terminalと`MERGING`（#41）を除く全stateで同一state維持、`cancelling`設定 + `CMD_HALT_RUN(binding)`のみ、awaiting := `HALT_FOR_CANCEL`
- `EV_CANCELLATION_COMPLETED`: terminalと`MERGING`を除く全stateから`CANCELLED`へ（binding guard）。**`deferred_integrity`保持中は`CMD_RECORD_INTEGRITY_INCIDENT(evidence)`と`CMD_INVALIDATE_APPROVALS`を伴い、検出済み違反を破棄せず監査記録へ残す**（Section 3.5.2）。その他の付随値は破棄
- `EV_RUN_FAILED`: terminal・`MERGING`（#42）・resumable state・halt gate中（横断規則）を除くactive stateから`FAILED`へ（`recovery_to` := 進入元。pending / awaiting / cancelling引継）
- resumable state + `EV_RUN_FAILED`: 同一state維持・全付随値保持
- terminalは全event拒否。未定義`(state, event, guard値)`は構造化errorで拒否（AC-C01-02）

**decision flowのGitHub会話順序**（#10 / #12 / #14〜#20）: (a) Claude draft投稿確認、(b) Codex verdict投稿確認、(c) Claudeの最終brief / decision record / revised draft投稿確認、(d) 次のCodexまたはユーザー、の順に両agentの発言が個別にGitHubへ現れる。

## 6. C-01のscope境界（Phase 1）

**実装する**: `domain/states.py`、`domain/events.py`、`domain/commands.py`、`domain/machine.py`（registry・`initialize`・`transition`）、最小のvalue object（`MachineState` / `BlockContext` / `BlockedContinuation` / `BlockResolutionEvidence` / `IntegrityEvidenceRef` / `CancelAttempt` / `PendingRecord` / `RecordEvidence` / `OpaqueBinding` / `Awaiting` / `Progress` / `Budget`）。

**実装しない（out of scope）**: GitHub APIアクセス・comment観測（C-05）/ actor認証・record chain・整合性検出・整合性復元検証・外部evidence検証（C-06）/ checkpoint・状態再構築・salvage手順（C-07、salvageはPhase 14）/ subprocess・signal・process停止の実行（C-03 / C-09）/ engine・intent構造化・counter管理・limit引き上げ検証（C-08 / C-10 / C-11）/ finding ledger（Phase 10）とid・binding採番（C-08）/ CLI / Skill / wrapper。

## 7. Test計画

- registryをdataとして全state × event × guard discriminator値をtable-drivenで検査（未定義は構造化error）
- **一意性とoverlap**: discriminator全値（awaiting 20値 + progress 3値 + block kind 3値 / reason 2値 + pending / cancelling / return_to / recovery_toの有無）の展開で一致rule数0または1。`recovery_to`と`block`の排他、**付随値の組合せ不変条件（Section 1）を到達可能な全MachineStateで検査**
- 17 state到達可能性、遷移表・遷移図のsnapshot照合、純粋性、terminal全拒否、付随値のevent非注入
- **binding**: PRODUCEDなしVERIFIED・不一致・再利用・pending中の他event拒否、partial turn再開
- **awaiting順序**: `CMD_EXECUTE_MERGE`は#34のみ、順序飛ばし・重複・不一致拒否
- **bounded-progress**（AC-C01-09）: budget対応のregistry導出、3 round系列（二重計上なし）、5回目許可・6回目停止、共通counter、loop終了結果の処理
- **block解消gate**（AC-C01-11）: 単純resumeのcommandなし維持（3 kind）。`BlockResolutionEvidence`の完全一致。**同じtargetへの別intervention record（1回目のみ受理）/ 別blockを指すrecord（target不一致で拒否）/ 同一recordのreplay（record規約で拒否）を分けて検査**。`BLOCK_INTERVENTION`の2経路同値性
- **integrity**（AC-C01-12）: `MERGING`の3つのawaiting値それぞれで、検出から終端までの**end-to-end系列**を検査する — preconditions段階は安全停止 + 承認失効、outcome段階は`deferred_integrity`保存 + 照会維持のうえ、`EV_MERGE_CONFIRMED`（-> `MERGED` + incident記録 + 失効）/ failure起点の未実行確認・不明（-> `BLOCKED`。`MERGE_FAILED`経由の通常resumeでgateを迂回できない）/ cancel起点の未実行確認（-> `CANCELLED` + incident記録）の全分岐で**同じevidenceが最終状態または安全停止まで保持される**こと。`cancelling`中の検出 -> `EV_CANCELLATION_COMPLETED`の系列で違反が破棄されず監査記録されること。**受理する全経路で`CMD_INVALIDATE_APPROVALS`が即時発行される**こと。active 6 stateでhalt gate経由（`EV_BLOCK_HALT_COMPLETED`まで`BLOCKED`にならない）。resumable stateで直接`BLOCKED`。進入時の`recovery_to` / `return_to` / 旧`block`破棄。**generic fallbackで未修復chainからfresh reviewを起動できない**こと（専用evidenceのみが出口）。**404 / sequence gap / hash mismatchの各種別で、同一違反の再検出が同じbindingとして冪等に扱われ、別違反が別attemptになる**こと
- **cancel / 緊急停止**（AC-C01-10）: 完了event前のterminal化禁止、`cancelling`中のstale pending拒否、全8 resumable stateのcancel -> halt失敗 -> resumeでhalt再発行のみ、binding不一致完了event拒否、`MERGING`照会経由
- **preflight NG**: resume系event拒否（新runの`initialize`のみ）
- user-input record 6種の2経路同値性、resume系列end-to-end、decision flow会話順序系列

## 8. 設計レビューで確定した判断

1. **Codex起動command**（round 1）: typed purpose方式
2. **CI code failure**（round 1）: `CHANGES_REQUESTED` + 承認失効
3. **awaiting lifecycle**（round 2）: command -> expected resultの一意化
4. **user-input recordの2経路**（round 3）: PowerShell転記とGitHub直接commentの合流
5. **budget型bounded-progress**（round 3〜5）: loop継続遷移のみ判定、共通5-turn counter、off-by-one禁止
6. **block体系**（round 4〜7で改訂）: `BlockContext` 3 kind。解消は`BlockResolutionEvidence`（**record bindingとtarget_block_bindingを分離**）の完全一致。`RECORD_INTEGRITY`はgeneric fallbackを受理せず、整合性復元またはsalvage確立の専用evidenceのみを出口とする（fail closed。salvage手順はPhase 14まで未供給）
7. **integrityの検出時安全**（round 7）: `MERGING`ではoutcome確定を最優先し、preconditions段階では承認失効付きで安全停止。active stateではhalt gate（`HALT_FOR_BLOCK`）でprocess停止完了後にのみ`BLOCKED`へ入る
8. **cancelの2系統・停止完了gate・attempt binding**（round 2〜6）: `USER_CANCEL` record bindingの再利用、横断resume規則
9. **preflight NGの復帰**（round 6）: resume系event拒否、新runの`initialize`のみ
10. **deferred_integrityと即時失効**（round 8）: outcome段階・cancel中に検出したintegrity violationは`deferred_integrity`としてMachineStateに保持し、terminal後の再入力に依存せずoutcome / cancel確定ruleが必ず消費する（block化またはterminal監査記録 + 承認失効）。violation bindingは`IntegrityEvidenceRef`にC-06が持たせ、同一違反の再検出は冪等。`CMD_INVALIDATE_APPROVALS`（冪等）は検出を受理する全経路で即時発行する
