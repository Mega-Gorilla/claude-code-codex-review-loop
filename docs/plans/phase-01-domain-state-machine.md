<!-- SPDX-License-Identifier: Apache-2.0 -->

# Phase 1計画: C-01 domain state machine

| Field | Value |
| --- | --- |
| Status | **Accepted**（高水準実装計画。本計画PRのユーザー承認とmergeで確定する。完全遷移・guard排他・到達可能性の正本はC-01実装のcode registryとtestであり、本書は契約・受入条件・test系列を定める） |
| 正本関係 | [implementation plan](implementation-plan.md)のC-01節の詳細。target behaviorは[target experience](target-experience.md)に従い、本書は変更しない |
| 対応Issue | #6（本書は計画。Issue #6のcloseはC-01実装PRで行う） |
| 受入条件 | AC-C01-01〜12 |

## 1. 目的と正本の役割分担

C-01は、target experienceの「State model」節が定義する17 stateと全遷移を、副作用のない純粋関数として表現するdomain state machineである。本書は**高水準の実装計画**として、(1) 不変条件、(2) 責務境界、(3) 状態モデルの基本semantics、(4) sub-protocolごとの必須契約、(5) 受入条件と受入test系列を確定する。

**本書は完全遷移表を確定しない。** すべてのedge caseをnormativeな文章で先に固定する方式は、設計レビュー12 roundの経験から、組合せの検証手段として不適切と判断した（1件の文章修正が別の組合せに新しい矛盾を生む）。正本は次のとおり分担する。

| 資料 | 役割 |
| --- | --- |
| 本計画文書 | **normative**: 不変条件・責務境界・必須契約・受入条件・受入test系列 |
| C-01実装のcode registry | **完全遷移の単一の正本**。全ruleをdataとして保持する |
| property / sequence test | registryの一意性・到達可能性・純粋性と、本書の契約・系列の**機械検証** |
| 生成された遷移表・遷移図 | 実装PRでregistryから導出して文書へ反映し、snapshot照合する（AC-C01-01） |

文章だけで実装より先に完全性を証明したようには見せない。実装中に本書の契約と矛盾が見つかった場合は、実装を契約に合わせるか、矛盾をPR上で明示して契約側を改訂する（silent divergenceを作らない）。

## 2. 不変条件（target experienceから導出）

1. **GitHub canonical**: agent / userの発言を伴う遷移は、canonical recordの投稿とread-after-write確認の完了をevidenceとして受けてからのみ進む。未永続化の出力を判断根拠にしない
2. **head binding / merge安全**: merge実行は直前の全条件再検証の成功結果を受けてからのみ発行される。実行後はGitHub照会の結果だけが成否を決め、成功を偽らず、二重mergeせず、成否不明を別の情報で上書きしない
3. **human merge gate**: mergeへ進む承認は、GitHub login allowlistと完全一致で検証されたユーザーrecordのみ（D-031、fail closed）
4. **bounded progress**: round / turn上限・膠着の判定結果が継続を許す場合にのみ、次agentを起動するcommandが発行される。上限到達時は新しいagent commandを一切発行せず`BLOCKED`で停止する
5. **安全なcancel**: `CANCELLED`へは、active processの停止とcheckpoint保存の完了を確認した後にのみ遷移する（intent検証は真正性の保証であり、停止の保証ではない）
6. **integrity非消失・fail closed**: 検出済みのintegrity violationはsilentに失わない。canonical chainの改変・削除はsilent repairせず、検証された回復手段がない限り`BLOCKED`のまま留まる
7. **純粋性**: C-01はI/O・時刻・乱数・環境変数へ依存せず、binding等のopaque値は等価比較のみを行い、counter算術を行わない。付随情報は遷移ruleだけが設定し、eventから遷移先やcommand列を注入できない

## 3. 責務境界

| Component | C-01との関係 |
| --- | --- |
| C-01 | 遷移判断のみ。commandは記述であり実行しない。1遷移のcommand列は決定論的で、条件分岐の意味を持たない（条件で結果が分かれる処理は、必ず結果eventを受けて次のruleが判断する） |
| C-05 | GitHub transport。recordの投稿（冪等）・既存commentの観測・read-after-write確認 |
| C-06 | 検証。actor / record chain / 外部evidence（直接comment）の検証、integrity violationの検出とviolation binding採番、incident record内容の構成 |
| C-07 | checkpoint、resume時の状態再構築、承認失効の実行、salvage手順（Phase 14） |
| C-08 | advance-submit engine。event組立、user intentの構造化、record bindingの採番、process停止の実行 |
| C-09 / C-10 / C-11 | Codex起動（typed purpose）、round counter管理、clarification / decision管理とprogress・膠着判定 |
| C-12 / C-13 | CI確認・final reporter、merge preconditions検証・実行・結果照会 |

## 4. 状態モデルの基本semantics

- **17 state**はtarget experienceの定義どおり。**terminal** = `MERGED` / `CANCELLED`（全eventを構造化errorで拒否）。**resumable** = `WAITING_CI` / `AWAITING_USER_DECISION` / `AWAITING_TOOL_PERMISSION` / `READY_FOR_HUMAN_MERGE` / `BLOCKED` / `FAILED` / `REPORT_FAILED` / `MERGE_FAILED`の8つ。残る7つが**active**
- 開始は`initialize(preflight_event) -> (machine_state, [command])`の専用API。preflight成功では`RUNNING_REVIEW`へ入り、**purpose = `CODE_REVIEW`のCodex起動command 1件と対応する応答期待値**を返す（初回reviewの開始を欠いた実装は本契約に適合しない。5.1）。preflight失敗では`FAILED`へ入りcommandを返さない。可視stateに「未開始」を追加しない。preflight失敗で作られた`FAILED`はresume系eventを拒否し、新しいrunの`initialize`（preflight再実行）だけを復帰経路とする
- 遷移関数は`transition(machine_state, event) -> (machine_state, [command])`。未定義の入力はsilent no-opにせず、構造化errorで拒否する
- **MachineStateは排他的contextの直和を実装候補とする**: 可視stateに加え、`NormalContext` / `CancellingContext` / `IncidentContext` / `BlockedContext`等の互いに排他なcontextで「進行中の手続き」を表現し、**不正な付随値の組合せを型として表現不能にする**。多数のoptional fieldの直積を文章上の組合せ不変条件で制約する方式は採らない（設計レビューで矛盾の主因になった）。最終的な型はC-01実装PRで確定する
- guardは有限のtyped discriminatorに限定し、registry上の各（状態, event, guard）に一致するruleが0件または1件であることをtestで機械検証する。優先順位による解決はしない

## 5. Sub-protocolの必須契約

C-01の遷移は、main workflowと4つの直交するsub-protocol（record persistence / cancellation / integrity incident / merge transaction）の合成として実装する。以下は設計レビュー（round 1〜12）で確定した製品レベルの契約であり、実装がこれを満たすことを節8の系列testで検証する。

### 5.1 main workflowとbounded progress

- Codex起動はtyped purpose（`CODE_REVIEW` / `CLARIFICATION` / `DECISION_VERDICT`）を持つ単一のcommandとする。CI code failureは`CHANGES_REQUESTED`へ戻し、既存承認を失効させる
- 応答を要するcommandは「次に受理してよい応答」の期待値を同一ruleで設定し、応答は期待値一致でのみ受理・消費される。順序を飛ばした応答・消費済み応答の再入力は拒否する
- decision flowでは、Claude draft / Codex verdict / Claudeの最終brief・decision record・revised draftが**それぞれ個別のcanonical record**としてGitHubへ現れる（省略・結合しない）。verdict確認前のbrief / decision record投稿は拒否される
- **budget契約**: `REVIEW_ROUND`は新しいfix roundの開始で1回だけ消費し（同一roundの完了で二重計上しない）、`CLARIFICATION_TURN`は「質問／再提出と回答の一往復 = 1 turn」で**5回目のturn開始を許可し、6回目の開始を停止**する。`REVISE_AND_RESUBMIT`は同一topic（fingerprint）のclarification turnとして共通counterを消費する。counter管理と判定はC-10 / C-11の責務で、C-01は判定結果のみを受ける
- **loopを終了する結果は上限turnで得られたものでも常に処理する**（5回目のclarificationで得た合意、上限時のASK_USER / PROCEED verdict等）
- 上限・膠着で`BLOCKED`へ入るとき、本来の継続（遷移先・command列・付随action）をregistry由来の値として保存する。**同一条件での単純resumeは継続を再現せず**、limit引き上げの検証・膠着を解消するcanonical recordの確認・head変更によるfallback（継続破棄 + 承認失効 + fresh review）のいずれかを経てのみ、保存した継続を1回だけ再現または破棄する。解消evidenceは対象blockへの完全なbinding一致を要求する

### 5.2 canonical record persistence

- 内部record（agent / Controller生成）は`PRODUCED -> 冪等な永続化command -> VERIFIED`の対で進む。対応するPRODUCEDを経ないVERIFIED、binding不一致、過去evidenceの再利用は拒否する。永続化commandは冪等（既投稿なら確認のみ）であることをC-05へ要求する
- 確認待ちのrecordは**単一のpending**として保持され、保持中は当該手続きを進めるevent以外のsemantic eventを拒否する。投稿後・確認前に中断したpartial turnはcheckpointから同一turnとして再開できる（resumeは永続化確認の再発行のみを返し、次agentを起動しない）
- **user-input recordは2経路**: 主経路のPowerShell / Skill入力はC-08がintentへ構造化し、内部record規約でGitHubへ転記する（bindingはC-08採番。evidenceはactor / input route / head / intentを保持）。GitHubへの直接commentは再投稿せず、C-06がcomment ID・body hash・actor（allowlist完全一致）・対象headを検証したexternal evidenceとして直接受理する（bindingはC-06導出。消費済みcommentの再提示は拒否）。両経路は同一のsemantic eventへ合流する
- record自身のbinding（一意性・再利用防止）と、解除・停止の対象を指すbinding（block / cancel attempt）は**別のfield**として扱い、C-01は新たな採番をしない（純粋性）

### 5.3 cancellationとprocess停止

- 対話cancelはcancel intentのcanonical検証後、同一の処理位置に留まり**停止command（process tree停止 + checkpoint保存）だけ**を発行し、attempt bindingが一致する停止完了eventを受けてからのみ`CANCELLED`へ遷移する。実行中processが無ければ完了eventは即時返る。Ctrl+C等の緊急停止は、C-03 / C-08が停止・checkpointを行い、run / checkpointへのbindを検証した完了eventを直接入力する
- cancel進行中は、stale pendingのVERIFIEDを含む全semantic継続を拒否し、**stateの分類に関わらず、失敗・明示resumeは停止commandの冪等再発行だけ**を返す（停止・checkpoint完了が他のあらゆる再開に先行する）
- `MERGING`だけはmerge結果の照会を優先し、merge未実行の確認（cancel起点）でのみ`CANCELLED`になる
- `CANCELLED`は現在runのterminalであり、resumeは直前の安全なcheckpointから**新しいrunとして**開始する

### 5.4 integrity violationとincident監査

- C-06が検出するintegrity violation（canonical commentの改変・削除・sequence gap。AC-C06-06〜08）は、violation binding付きのevidenceで入力される（同一違反の再検出は同じbindingで冪等、別違反は別binding。404 / sequence gap / hash差分など参照可能なrecordが存在しない違反も表現できる）
- **検出を受理する全経路で、既存承認を即時かつ冪等に失効**させる（「改変された決定は失効する」を遅延させない）
- 検出時の安全規則: `MERGING`のmerge実行後・成否不明では**outcome確定を最優先**し、検出済みevidenceを保持したまま照会を継続する（成否不明をintegrityで上書きしない。integrityの回復だけではagent / merge commandを発行できない）。preconditions段階ではmergeを実行せず安全停止する。active stateでは停止gate（process停止・checkpoint完了の確認）を経てからのみ`BLOCKED`へ入る。cancel進行中は検出を記録して停止処理を継続する
- 検出済みで未記録のviolationは**集合**として保持し、複数検出は常にunion（上書き禁止）。**terminal（`MERGED` / `CANCELLED`）へは、全violationが検証済みのincident record（canonical record gateを通過した監査記録。cancelで未完了になったturnの監査参照を含む）へ含まれた後にのみ遷移**する。記録が部分的な場合は残余で次のincident cycleへ直列化する。incident記録の各段階の失敗・resumeは、段階に対応するcommand（作成依頼 / 冪等な永続化）だけを冪等に再発行する
- `RECORD_INTEGRITY`起因の`BLOCKED`はgenericなfallbackで出られない（**fail closed**）。出口は、整合性が復元され同一chainを再検証できたこと、または明示的なsalvage手順で旧chainと承認を破棄し新しいbaselineを確立したこと（提供はC-07 / Phase 14。それまで供給源は存在しない）を検証した専用evidenceのみ

### 5.5 merge transaction

- merge承認の検証後は**preconditions再検証commandのみ**を発行し、全条件一致の結果eventを受けて初めてmerge実行commandを発行する（**実行commandの発行経路はこの1つに限る**）。不一致はhead変更なら承認失効 + fresh review、それ以外は`MERGE_FAILED`
- 実行後はGitHub照会の結果eventだけが`MERGED` / `MERGE_FAILED`を決める。成否不明は`MERGE_FAILED`として安全停止する（検出済みintegrityを保持する場合は照会を継続する。5.4）
- `MERGING`中のcancel・runtime失敗は即terminalにせず、照会を経由する
- `MERGING`でcheckpointされたrunの明示resumeは、outcome照会待ちなら照会のみ、preconditions待ちなら再検証のみを再発行する
- 外部からのhead変更を検出した遷移は、承認を失効させてfresh reviewへ戻す

## 6. 実装方針

- 全遷移ruleを**単一のregistry**としてdataで定義し、`domain/machine.py`がそれを解釈する。共通規則（cancel / failure / progress / integrity等）もregistry内で個別ruleへ展開する
- guardは有限typed discriminatorに限定し、**一意性（overlapなし）・17 state到達可能性・純粋性・付随値の非注入をproperty testで機械検証**する。集合の包含のような判定は、C-01内部で有限値（例: 記録済み集合とのcoverage 2値）へ決定論的に導出してからguardに使う
- 節8の受入test系列を**sequence testとして実装**し、本書の契約を検証する
- registryから遷移表・遷移図を生成して文書へ反映し、snapshot照合をtestで行う（AC-C01-01）
- value objectは排他的context直和を第一候補として実装時に確定する
- **Phase 1のscope**: `domain/states.py` / `domain/events.py` / `domain/commands.py` / `domain/machine.py`（registry・`initialize`・`transition`）と、成立に不可欠な最小のvalue objectのみ。GitHub I/O・検証・checkpoint・subprocess・engine・counter管理・CLI / Skill / wrapperはout of scope（各担当componentのPhaseで実装）。finding ledgerはPhase 10、id / binding採番はC-08。空moduleも先行実装も作らない

## 7. 受入条件

受入条件は[implementation plan](implementation-plan.md)のAC-C01-01〜12（正本）に定義され、本書の契約（節2・節5）と節8の系列に対応する。概要: registry導出と照合（01）/ 未定義遷移の拒否と到達可能性（02）/ evidence gate（03）/ 純粋性（04）/ terminal保護とresume（05）/ 復帰位置の正しさ（06）/ merge照会の安全（07）/ guard一意性とnegative系列（08）/ bounded progress（09）/ cancelの停止完了gate（10）/ block解消gate（11）/ progress以外のblockとincident監査（12）。

## 8. 受入test系列一覧

C-01実装PRは、少なくとも次の系列をproperty / sequence testとして実装する。これらは設計レビュー12 roundで特定した安全系列であり、文章上の完全遷移表に代わってedge caseを追跡する（括弧は主対応AC）。

**registry性質（property test）**

- R1: 到達可能な（状態 × 付随値 × event × guard値）の全展開で、一致ruleが常に0件または1件。優先順位による解決が存在しない（08）
- R2: 純粋性 — 同一入力の再適用で同一結果、入力非変更、I/O・時刻・乱数・環境変数への非依存、command列順序の決定性（04）
- R3: 17 stateすべて到達可能、到達不能stateの検出、terminalからの全event拒否、未定義遷移の構造化error（01、02、05）
- R4: 付随情報（復帰先・継続・binding等）が遷移ruleのみで設定され、eventから注入できない。不正な付随値の組合せ（排他であるべき進行中手続きの同時保持等）を構築・受理できないことを、表現方式（context直和等）に依存しない形でproperty / static testで検証する（06）
- R5: 生成した遷移表・遷移図と文書のsnapshot照合（01）

**canonical record（03）**

- C1: PRODUCED -> 冪等persist -> VERIFIEDの正常系。PRODUCEDなしのVERIFIED / binding不一致 / 過去evidenceの再利用 / pending中の他semantic eventの拒否
- C2: partial turn（pending保持）-> checkpoint -> resumeで、永続化確認の再発行のみが返る（次agentを起動しない）
- C3: user-input record全種の2経路同値性（PowerShell転記経路とGitHub直接comment経路が同一のsemantic遷移へ合流し、直接comment経路では永続化commandが発行されない）

**workflowとbounded progress（09、11）**

- W0: initializeの正常系 — preflight成功eventから`RUNNING_REVIEW`へ入り、purpose = `CODE_REVIEW`のCodex起動command 1件と応答期待値が返り、最初のreview結果待ちになる（I9の失敗系列と対。03）
- W1: 既定3 review roundの開始から停止までの系列。roundの二重計上がない
- W2: clarificationの5回目turn開始が許可され、5回目の回答処理後の6回目開始で停止する。同一fingerprintのresubmitが共通counterを消費し、5 turn消費後のresubmitが停止する
- W3: loop終了結果（上限turnのCONFIRMED / REVISED / WITHDRAWN / ESCALATED、上限時のASK_USER / PROCEED）が正常に処理される
- W4: 上限到達時に新しいCodex / host commandが一度も発行されない。block後の単純resumeはcommandなしで`BLOCKED`維持。limit引き上げ検証 / 膠着解消record確認（完全binding一致）でのみ本来のcommand列（付随action含む）が1回再現される。別block向け・消費済みblockへの解消eventは拒否。fallbackは継続を破棄しfresh reviewへ入る
- W5: decision flowの会話順序（draft -> verdict -> brief / record / reviseが個別recordとして順に要求され、verdict確認前のbrief / record投稿が拒否される）

**cancellation（10）**

- X1: intent検証 -> 停止command -> 完了event -> `CANCELLED`。完了event前はterminalにならず、新agentを起動しない
- X2: 全8 resumable stateでのcancel -> 停止失敗 -> 別processからのresumeで、停止commandの再発行だけが返る
- X3: attempt binding不一致の完了event（過去attempt）の拒否。緊急停止経路のrun / checkpoint bind検証
- X4: `MERGING`中のcancelが照会を経由し、merge未実行の確認（cancel起点）でのみ`CANCELLED`になる

**merge transaction（07、08）**

- M1: merge実行commandの発行経路が「preconditions一致eventの消費」の1経路のみであること。実行前の完了event・preconditions OKの重複入力の拒否
- M2: outcome不明 -> `MERGE_FAILED`安全停止（通常時）。head変更検出 -> 承認失効 + fresh review（CI待ち / merge gate / preconditions段階 / `MERGE_FAILED`の各局面）
- M3: `MERGING`でのcheckpoint -> 新processからの明示resumeが、outcome照会待ち（実行 / cancel / 失敗の3起点すべて）では照会のみ、preconditions待ちでは再検証のみを再発行する

**integrityとincident（12）**

- I1: 検出を受理する全経路で、承認が即時・冪等に失効する
- I2: `MERGING`の3局面のend-to-end — preconditions段階は安全停止 + 失効、outcome段階は検出evidenceを保持して照会継続（不明が続く限りterminalにもfresh reviewにも進めない。integrityの回復だけではagent / merge commandを発行できない）、確定後は5.4の終端処理へ進む
- I3: active stateでは停止gateの完了までblock化されず、resumable stateでは直接blockされる。進入時に旧resume情報が残らない
- I4: `RECORD_INTEGRITY`のblockがgeneric fallbackを拒否し、復元再検証 / salvage確立の専用evidence（対象binding一致）のみで出られる。未修復のchainからfresh reviewを起動できない
- I5: 複数violation（E1 -> E2）の検出（cancel中 / outcome照会中 / incident記録中 / 停止gate中）で上書き・silent lossがなく、全violationが検証済みincident recordへ含まれるまでterminalに進まない（部分記録は次cycleへ直列化される）
- I6: incident recordがcanonical record gateを通過し、`MERGED` / `CANCELLED`両targetの正常系列がterminalへ到達する。作成失敗 / 投稿失敗 / 確認失敗のそれぞれからのresumeが、段階に対応するcommandだけを冪等に再発行する（`MERGING`起点・非`MERGING`起点とも）
- I7: pending中の外部cancel -> integrity検出 -> 停止完了 -> stale pending破棄（監査参照は保持され検証済みrecordへ残る）-> incident記録 -> `CANCELLED`のfull系列
- I8: 404 / sequence gap / hash mismatchの各種別で、同一違反の再検出が同じbindingとして冪等に扱われ、別違反が別attemptになる
- I9: preflight NGで作られた`FAILED`がresume系eventを拒否する（復帰は新runの`initialize`のみ）

**resume整合（05、06）**

- Z1: resumeの優先順位 — 停止 / 記録の手続き中はその冪等再発行、pending保持中は永続化確認、応答待ち中は対応commandの再発行、いずれも無ければ復帰先の駆動command。reporter retry / CI resume / permission resume（復帰先対応commandと応答期待値の同時設定）の各系列
- Z2: resumable stateでの失敗が状態と付随情報を保持する

## 9. 設計レビューで確定した判断（round 1〜12の決定録）

1. Codex起動はtyped purpose方式（`CODE_REVIEW` / `CLARIFICATION` / `DECISION_VERDICT`）。実行基盤はC-09へ集約
2. CI code failureは`CHANGES_REQUESTED` + 承認失効
3. command -> expected resultの対応をMachineState上で一意にする（応答期待値のlifecycle）
4. user-input recordはPowerShell転記とGitHub直接commentの2経路で、同一semantic eventへ合流する
5. bounded progressはbudget型（loopを継続する遷移のみが判定を受け、消費点を明示し、loop終了結果は常に処理する。clarificationとresubmitは同一fingerprintの共通5-turn counter、5回目許可・6回目停止）
6. blockは`PROGRESS` / `EXTERNAL_DEPENDENCY` / `RECORD_INTEGRITY`の3種。単純resumeでは継続を再現せず、解消evidenceは対象blockへの完全binding一致を要求する。`RECORD_INTEGRITY`はfail closed（専用の復元 / salvage evidenceのみが出口。salvage供給はPhase 14）
7. cancelは対話cancel（intent検証 -> 停止 -> 完了gate）と緊急停止の2系統で、いずれも停止・checkpoint完了後にのみ`CANCELLED`。attempt bindingは起点recordのbindingを再利用する
8. preflight NGの未開始`FAILED`はresume不可。新runの`initialize`のみが復帰経路
9. merge成否不明（outcome uncertainty）はintegrityで上書きしない（照会の継続が最優先）
10. incident監査はcanonical record gateを通過した検証済みrecordを要求し、検出済みviolation集合の全記録までterminalへ進まない（部分記録は直列化）。cancelで未完了になったturnの監査参照も保持・記録する
11. 検出・解消・記録の各手続きは冪等な再発行でresumeし、停止・checkpoint・記録の完了が他のあらゆる再開に先行する
12. 完全遷移の正本は実装のcode registryとproperty / sequence testとし、文書は契約・受入条件・test系列と生成結果の反映に徹する（本方針。ユーザー・Codex合意: [PR #26 comment](https://github.com/Mega-Gorilla/claude-code-codex-review-loop/pull/26#issuecomment-5343602104)）
