<!-- SPDX-License-Identifier: Apache-2.0 -->

# Implementation plan

| Field | Value |
| --- | --- |
| Status | **Draft** |
| Baseline | [target-experience.md](target-experience.md)（Status: Agreed） |
| Parent roadmap | Issue #2 |
| Owner | Mega-Gorilla |
| Last updated | 2026-08-19 |

target experienceが定義した完成状態を、どのcomponent、どの依存、どの順序で作るかを定義する。用語は[glossary](../glossary.md)、全体像は[architecture overview](../architecture/overview.md)を参照。

本書はcomponentの責務境界とPhaseの正本である。API詳細は各子IssueのPRで設計する。target behaviorを本書で変更しない。変更が必要な場合はdecision briefを提示し、ユーザー合意後にtarget experienceのdecision logへ反映する。

## 1. 設計原則

原則はtarget experienceが要求する性質と、それが破れたときの失敗から導出する。参考実装の観測はこれらを補強する材料であり、根拠そのものではない。観測の詳細は[reference implementation assessment](../research/reference-implementation-assessment.md)にある。

| ID | 原則 | 本projectでの必要性 |
| --- | --- | --- |
| P-002 | **単一のcore engineを持つ**。active host経路とheadless経路が同じengineを共有し、round orchestrationを二重実装しない | 2つのentry pointが別のorchestrationを持つと、GitHub永続化gateやclarification上限の変更が片方に反映されず、同じ状態遷移が経路によって異なる結果になる |
| P-003 | error分類をexit code、構造化出力、`gh api`のstatusで行う。出力文字列の部分一致による分類を禁止する | 一時障害の誤判定はretryとstate遷移を直接誤らせる。本projectはreview本文そのものを扱うため、`timeout`や`quota`を論じる文が分類対象へ混入する |
| P-004 | GitHubからの取得は`--json`の構造境界で行う。出力の行分割で本文を切らない | metadata markerに複数行JSONを格納する。read-after-writeの本文hash照合も本文が壊れれば成り立たない |
| P-005 | GitHubへ投稿する本文はfile経由で渡す。本文をcommand line引数へ置かない | final reportとdecision briefは長文になる。Windowsのcommand line長制限へ到達すると環境依存で失敗する |
| P-006 | permission bypass flagをcode上で構築しない。禁止語のcontract testを置く | target experience Section 9が`bypassPermissions`相当をpresetから使用不可にすることをDecidedとしている。構築経路が存在すれば設定次第で有効化され得る |
| P-007 | canonical recordとユーザー判断の受理を、producerの認証を伴う条件で行う | merge承認、仕様判断、follow-up許可をGitHubから読む。受理条件が緩いと、書込可能な別ユーザーがworkflow authorityを取得できる |
| P-008 | promptへ埋め込むGitHub由来のtextをfenceし、データであって指示ではないと明示する | Issue本文、comment、review threadはすべて外部入力であり、agentのinstructionとして解釈され得る |
| P-009 | artifactはrunごとのdirectoryへ、作成者のみがアクセスできる権限で作成する。権限表現をOSごとに定義する | artifactにはprivate repositoryのdiffとreview内容が含まれる。POSIXのmode指定だけではWindowsで保護されない |
| P-010 | ruffとmypyをPhase 0からCIへ入れる | 後から導入すると既存code全体の修正が必要になり、導入自体が先送りされる |
| P-011 | testはagent CLIと`gh`をprocess境界でfakeし、live serviceへ接続せず全件実行できる状態を保つ | 実行するとGitHubへ書き込む処理を対象にするため、process境界のfakeがなければCIでの全件実行が成立しない |
| P-012 | versionとgit tagを最初から運用する | PluginとController CLIはprotocol versionを交換する。versionが実体を持たないとこの機構が機能しない |
| P-013 | すべてのcodeを`src/`配下のpackageへ置き、`sys.path`操作を禁止する | Controllerは任意repositoryから呼べるinstall済みpackageであることが要件。repository相対pathへ依存すると成立しない |
| P-014 | subprocess呼び出しはlist形式のargvで行う。`shell=True`、`os.system`、`eval`、`exec`を使用しない | agentへ渡す値にはIssue本文やreview結果が含まれ、shell経由ではinjection面になる |
| P-015 | 本projectのcodeでcredentialを保持しない。認証は認証済みCLIへ委譲し、agentごとの到達可能範囲をC-06で制御する | 「渡さない」だけではsubprocessが環境変数やhome配下の設定へ到達できる。委譲と隔離は別の設計項目である |

`P-001`（runtime依存の方針）はPhase 0で決定する技術判断であり、原則ではない。Section 7を参照。

### P-002の意味

禁止するのは、2つのentry pointがそれぞれround orchestrationを実装することである。entry pointが持ってよいのは、引数解析、session boundaryの受け渡し、表示の3つに限る。state遷移、round管理、GitHub投稿の判断はすべてcore engineに置く。

## 2. active host protocol

本節は他のcomponentの前提になる。

### 2.1 制約

Controller CLIはClaude Code sessionの子processとして起動されるため、親のLLM turnを呼び戻せない。TUIへのキー入力注入も禁止されている。したがって「core engineが長時間loopを回し、必要になったらClaudeを呼ぶ」構造は成立しない。その構造はClaudeのsubprocess化（active session契約の違反）かSkill側への再実装（P-002違反）のいずれかになる。

### 2.2 制御の反転

core engineをresume可能なstep engineとして定義する。engineはClaudeを起動しない。次に何をすべきかを返し、呼び出し側が実行する。

```text
engine.advance(run_id) -> HOST_ACTION | AWAIT_USER | TERMINAL
```

主経路の流れは[architecture overview](../architecture/overview.md)のsequence図を参照。engineは1回のadvanceで1 actionだけを返し、結果がGitHubへ永続化され確認されるまで次のactionを決めない。

### 2.3 HOST_ACTIONの種類

初期案。Phase 8で確定する。

| Action | active hostが行うこと |
| --- | --- |
| `APPLY_FINDINGS` | blocking findingを評価し、修正・test・commit・pushする |
| `ASK_CLARIFICATION` | Codex findingへの質問文を作成する |
| `ANSWER_CLARIFICATION` | Codexからの確認要求へ回答を作成する |
| `DRAFT_DECISION_REQUEST` | 判断が必要な理由、候補、推奨をdraftする |
| `DRAFT_DECISION_BRIEF` | Codex verdictを反映した最終briefを作成する |
| `DRAFT_FOLLOWUP_CANDIDATES` | 重複確認済みのfollow-up候補を最大3件draftする |
| `STRUCTURE_USER_INTENT` | merge gateのユーザー入力を4 intentへ構造化する |
| `RUN_LOCAL_TESTS` | 対象headでlocal testを実行し結果を返す |
| `IMPLEMENT_ISSUE` | Issue要件から実装しPRを作成する |

### 2.4 agentごとの起動主体

| Agent | 起動主体 | 理由 |
| --- | --- | --- |
| Claude coder（主経路） | 起動しない。active hostがHOST_ACTIONを実行する | active sessionのcontext維持が要件 |
| Claude coder（headless経路） | Controllerがsubprocessとして起動するadapter | 対話sessionが存在しない復旧経路のため。engineから見たinterfaceは主経路と同一 |
| Codex reviewer / final reporter | Controllerがfresh subprocessとして起動する | read-onlyであり親contextを必要としない |

## 3. Package layout

```text
src/claude_code_codex_review_loop/
  errors.py                 構造化error分類（P-003）
  config.py                 repository設定 -> user設定 -> 組込み既定値
  domain/                   states / events / commands / machine / ledger / ids
  schema/                   validate / registry / migrate / envelope / review /
                            decision / followup / report / merge / action
  process/                  spawn / terminate / job_object / process_group
  policy/                   redaction / permission_profile / trust_rules
  transport/                gh / conversation / marker / render / threads
  identity/                 actor / allowlist / record_chain / credentials / fs_permissions
  runtime/                  checkout / codex / host_active / host_headless / prompt
  workflow/                 engine / actions / pr_mode / issue_mode / clarification /
                            decision / followup / qualification / reporter / merge_gate
  state/                    resume / retention / salvage
  cli.py                    console entry point `cc-review`
```

`policy`は純粋関数として評価だけを行い、GitHubからactor情報を取得しない。`transport`は未検証のmetadataを取得・投稿するI/Oに限定する。actorの解決、record chainの検証、検証済みcanonical recordの生成は`identity`が担う。これによりtransportがredactionを利用しつつ、identityがtransportを利用する一方向の依存になる。

**C-07以降のcomponentは、C-06が検証したcanonical recordだけを入力にする。** C-05が返す未検証metadataをworkflowの判断根拠にしない。

`plugin/`にはSKILL.mdと薄いlauncherだけを置く。`wrappers/`には任意wrapperを置き、wrapperなしでcore loopが動作することを設計条件とする。

## 4. Component

| ID | Component | 責務 | 依存 | Phase |
| --- | --- | --- | --- | --- |
| C-01 | domain state machine | 17 stateと全遷移を副作用のない関数で表現する | なし | 1 |
| C-02 | agent protocol schemaとcheckpoint envelope | agent入出力とcheckpoint envelopeのschema、validation、versioning、migration | C-01 | 2 |
| C-03 | process abstraction | 子process treeの起動・停止・timeoutのOS抽象 | なし | 3 |
| C-04 | security policy | redaction規則、permission profile、trust ruleの純粋な評価 | C-02 | 4 |
| C-05 | GitHub transport | 未検証のGitHub metadataの取得、投稿、read-after-write確認、thread操作 | C-01、C-02、C-03、C-04 | 5 |
| C-06 | canonical record検証とcredential隔離 | actor解決、allowlist照合、record chain検証、検証済みcanonical recordの生成、agentごとの到達可能範囲、OS別file権限 | C-03、C-04、C-05 | 6 |
| C-07 | resumeとretention | GitHubからのstate再構築、artifactの保持と削除 | C-02、C-05、C-06 | 7、14 |
| C-08 | active host protocolとstep engine | Section 2のprotocol。advance / submitの境界 | C-02、C-05、C-07 | 8 |
| C-09 | Codex fresh runtimeと隔離checkout | turnごとのread-only subprocessとexact head checkout | C-03、C-06、C-08 | 9 |
| C-10 | PR mode review loop | review→fix→re-reviewのround管理 | C-08、C-09 | 10 |
| C-11 | decision / clarification / follow-up | 判断フロー、clarification上限、follow-up許可gate | C-08、C-10 | 11 |
| C-12 | qualificationとfinal reporter | local testとCIの確認、final reportの生成 | C-07、C-09、C-10 | 12 |
| C-13 | human merge gate | 対話gateと明示承認後のgated merge | C-06、C-12 | 13 |
| C-14 | Issue modeとhandoff | Issue要件の取得、既存PR再利用、双方向handoff | C-10、C-11 | 15 |
| C-15 | Plugin配布と任意wrapper | version付きPlugin、監視 / 継続wrapper | C-03、C-08 | 16 |

依存図は[architecture overview](../architecture/overview.md)にある。

## 5. Componentの主要な決定と受入条件

受入条件はIDで参照する。節番号は参照に使わない。

### C-01 domain state machine

遷移関数は`(state, event) -> (state, [command])`。GitHub永続化とread-after-write確認の完了をeventとして要求し、gate未通過の遷移を表現できないようにする。

| ID | 受入条件 |
| --- | --- |
| AC-C01-01 | 17 stateすべてが到達可能で、遷移表と遷移図をdata drivenで照合できる |
| AC-C01-02 | 未定義遷移と到達不能stateをtestが検出する |
| AC-C01-03 | 検証済みcanonical recordの永続化eventが無い状態では、次agentを起動するcommandを生成できない |

### C-02 agent protocol schemaとcheckpoint envelope

agentとの構造化protocolとcheckpoint envelopeの**単一の所有者**とする。schemaをtransportや各workflowへ分散させない。

所有するschema: Codex review、coder result、clarification質問 / 回答、decision request / verdict / brief、follow-up候補と評価、final report、merge intentと承認record、`HOST_ACTION` envelopeとsubmit envelope、checkpoint envelope。

必須: すべてのschemaにschema versionを持たせる。未知versionは推測補完せずvalidation errorとする。normalizationやrepairを行う場合も、**repair後に必ず同じvalidatorを通す**。補完してよい範囲を空白正規化や既定値のような損失のない変換に限定し、意味的fieldの捏造を禁止する。

| ID | 受入条件 |
| --- | --- |
| AC-C02-01 | 代表schema corpusとmalformed corpusの両方で、未知version、必須field欠落、型不一致、size超過、cross-field違反が区別できるerrorになる |
| AC-C02-02 | repair経路を通った出力が、repairを経ない出力と同じvalidatorで検証される |
| AC-C02-03 | 旧versionのenvelopeがmigrationで読める。migrationできないversionはsilentに無視せずerrorになる |

### C-03 process abstraction

POSIXはprocess group、WindowsはJob Object相当で子孫を停止する。**Windows側は新規実装**とし、移植元を前提にしない。sleepは標準libraryで行い外部binaryへ依存しない。

| ID | 受入条件 |
| --- | --- |
| AC-C03-01 | 両OSで、timeoutとCtrl+C後に孫processが残らない |
| AC-C03-02 | Ctrl+C 1回でgraceful cancellation、2回目で強制停止になる |

### C-04 security policy

GitHubへ問い合わせずに評価できる純粋なpolicyだけを持つ。redactionは投稿本文、prompt、log、artifactへ共通に適用し、patternを一元管理する。fork PR、allowlist外author、agent設定fileの変更（`CLAUDE.md`、`AGENTS.md`、`.claude/**`、`.codex/**`、`.github/workflows/**`）に対するtrust ruleを、入力データに対する純粋な判定として定義する。

| ID | 受入条件 |
| --- | --- |
| AC-C04-01 | redaction対象文字列が投稿本文、prompt、log、artifactのいずれにも現れない |
| AC-C04-02 | 禁止flagがargv構築経路に存在しないことをcontract testが検出する |
| AC-C04-03 | fork PRとagent設定fileの変更に対する判定が、入力データだけで再現できる |

### C-05 GitHub transport

投稿後にcomment / review ID、URL、本文hash、対象head SHAを取得できるまでturnをcompletedにしない。timeout時は成否を推測せず、idempotency markerで検索してから再投稿する。取得は`--json`境界、投稿本文はfile経由、投稿前にC-04のredactionを適用する。

**このcomponentは認証を行わない。** 取得したmetadataは未検証として上位へ渡し、canonical recordとしての受理可否はC-06が判定する。

予約markerはControllerだけが付加する。agent生成本文に含まれる予約markerは投稿前に除去またはescapeする。取得時はcomment ID、author login、created / updated timestamp、repository、Issue / PR番号、body、payloadを、加工せずC-06へ渡す。

| ID | 受入条件 |
| --- | --- |
| AC-C05-01 | 投稿→再取得→hash一致→ID記録がfake `gh`で検証できる |
| AC-C05-02 | timeout後の再投稿で重複commentが発生しない |
| AC-C05-03 | review threadの取得、解決状態の判定、line findingへのreplyが動作する。thread不可時はcomment URL付きconversation commentへfallbackする |
| AC-C05-04 | agent生成本文へ埋め込まれた予約markerが、投稿前に除去またはescapeされる |
| AC-C05-05 | 取得結果が未検証metadataとして返り、このcomponent単独ではcanonical recordを確定しない |

### C-06 canonical record検証とcredential隔離

C-05が取得した未検証metadataから、検証済みcanonical recordを生成する唯一のcomponentとする。

#### record検証のthreat modelと保証範囲

守る対象は、書込権限を持つ第三者およびagent自身による、canonical recordの偽装・改変・欠落である。

検出できる条件を個別に定義する。

| 条件 | 検出方法 |
| --- | --- |
| 許可されていないactorによるrecord投稿 | actor loginがallowlistと完全一致しない |
| agent本文へ埋め込まれた予約marker | Controller以外が付加したmarkerとして拒否する |
| record本文の改変 | payload hashの不一致 |
| record内容の編集 | `updatedAt`が`createdAt`と異なる |
| 中間recordの削除 | sequence番号のgap |
| recordの並べ替え | 直前recordのhash参照chainの不一致 |
| 既知recordの消失 | checkpointが保持するcomment IDがGitHubで404になる |

**保証しない範囲**: local checkpointを失ったfresh processが、GitHub commentだけを入力に**末尾から連続するrecordの削除（suffix truncation）**を検出することは、本設計では保証しない。hash chainは残存部分の整合性しか示さず、「その後にrecordが存在した」という情報が外部に無いためである。

この残存riskへは、local checkpointだけで対処する。

- 確認済みの最大sequence番号（high-water mark `N`）と、既知のcomment IDをcheckpointへ保存する
- checkpointが残っている場合、**`N`以下の範囲**について欠落を検出する。GitHubから再構築した最大sequenceが`N`より小さい場合、既知comment IDが404になる場合、再構築したsequence集合が`N`までの範囲でgapを持つ場合が対象となる
- `N`より後のrecordはcheckpointへ記録されていないため、存在したかどうかを判定できない。checkpointを失った場合と同様に、この範囲のtail truncationは検出できない残存riskとして受け入れる

**GitHub comment上にmutableなanchor recordを置く方式は採用しない。** 単一commentを更新する方式はController自身の更新が`updatedAt != createdAt`となり編集検知規則と衝突し、append方式は末尾recordと一緒に削除できるため独立した証拠にならない。どちらもMVPで一意な実装にできない。

保証を強めるためにGitHub外の独立した永続化先を追加する選択肢は、MVP scope外とする。必要になった時点でD-010のdecision flowへ接続する。

不整合を検出した場合はsilent repairせず`BLOCKED`とし、差分を提示する。

#### ユーザー判断の受理

ユーザー決定として受理するcommentは、設定したGitHub loginのallowlistと完全一致する場合に限る（D-031）。`authorAssociation`とrepository permissionは補助条件とし、単独の判定根拠にしない。allowlistが未設定または取得できない場合はfail closedとし、bot accountを受理しない。受理したcommentのbody hashを記録し、編集・削除で決定を失効させる。承認はintent、repository、Issue / PR番号、head SHA、merge method、candidate fingerprintへbindする。

Codex reviewer環境から`GH_TOKEN` / `GITHUB_TOKEN`等のtoken変数を除去し、`GH_CONFIG_DIR`、HOME相当、`XDG_CONFIG_HOME`をreviewer専用の一時領域へ差し替える。git credential helperを無効化し、`GIT_ASKPASS` / `SSH_ASKPASS`が対話的に資格情報を取得しないようにする。隔離checkoutのremoteへpush可能な構成を与えない。

Claude coderはfeature branchへのpushまで。merge、follow-up Issue作成、deployはController専用とする。Auto modeが利用できない場合は逐次fallbackではなく、用途に応じて`acceptEdits`（自動化向け）、`default`（対話向け）、`dontAsk`（事前定義した非対話policy）から選択する。例外blockではbypassせず、Permission ID、tool / command、理由、risk、head SHA、resume方法をGitHubへ記録して`AWAITING_TOOL_PERMISSION`へ遷移する。

artifactとtemp fileは、POSIXでは`0600` / `0700`、Windowsでは作成者のみに限定したACLで作成する。

| ID | 受入条件 |
| --- | --- |
| AC-C06-01 | allowlist外loginの承認commentが、merge、follow-up Issue作成、仕様判断のいずれにも使われない |
| AC-C06-02 | allowlistを取得できない場合に、ユーザー判断を受理しない |
| AC-C06-03 | reviewer環境からのGitHub mutation、実repository書込、GitHub write credentialへの到達が、いずれも失敗する |
| AC-C06-04 | `AWAITING_TOOL_PERMISSION`からのresumeが、停止点の操作だけを再実行する |
| AC-C06-05 | 両OSで、artifact directoryが作成者以外からアクセスできない |
| AC-C06-06 | 上表の7条件（不正actor、埋め込みmarker、本文改変、編集、中間削除、並べ替え、既知record消失）を個別に検出して`BLOCKED`にする |
| AC-C06-07 | checkpointのhigh-water mark`N`に対し、GitHubから再構築した最大sequenceが`N`より小さい場合、または`N`までの範囲にgapがある場合に、欠落を検出して`BLOCKED`にする |
| AC-C06-08 | checkpointが保持する既知comment IDが404になった場合に、削除を検出して`BLOCKED`にする。AC-C06-07がsequenceの連続性を、本ACが個々のrecordの実在を扱う |
| AC-C06-09 | checkpointを失ったfresh resume、およびhigh-water mark`N`より後のrecordが削除された場合は、truncationを検出せず正常なconversationとして扱う。この限界をtestで明示する |
| AC-C06-10 | Auto modeの利用可否を検出し、利用できない場合は用途に応じて`acceptEdits`（自動化向け）、`default`（対話向け）、`dontAsk`（事前定義した非対話policy）のいずれかを選択する |
| AC-C06-11 | tool permissionの許可だけでは、merge、follow-up Issue作成、仕様判断（decision approval）のいずれも実行・代行されない |

### C-07 resumeとretention

resumeはGitHub canonical conversationからstateを再構築し、local checkpointはcacheとして照合する。GitHubで確認できないlocal出力を判断根拠にしない。PR lock、coder snapshot、external head updateを保持し、head変更時はreview承認とmerge承認を失効させる。artifactは正常run 30日、`FAILED` / `BLOCKED` / salvage 90日。active / lock保持中のrunを除外したbounded cleanupを起動時と明示commandで行う。

| ID | 受入条件 |
| --- | --- |
| AC-C07-01 | 中断後のresumeが同じturn IDから再開する |
| AC-C07-02 | 質問のみ投稿済みのpartial turnで、質問が重複投稿されない |
| AC-C07-03 | 外部からheadが更新された場合に旧承認が失効する |
| AC-C07-04 | cleanupがactive / lock保持中のrunを削除しない。dry-runで対象を確認できる |
| AC-C07-05 | GitHub recordとlocal artifactが、いずれもapproved head SHAへbindされている |
| AC-C07-06 | resume時に、GitHub commentへ直接入力されたユーザー回答を取得する。comment投稿はtriggerにならない |

### C-08 active host protocolとstep engine

Section 2のprotocolを実装する。active host adapterとheadless adapterを別実装とし、engineから見たinterfaceを同一にする。

**advance / submitの境界**: result pathはControllerがrun directory内に払い出す。呼び出し側から任意pathを受理しない。受理時にcanonical pathがrun directory配下であること、symlink / reparse pointを経由しないこと、所有者権限、size limitを検証する。`HOST_ACTION` envelopeをrun ID、action ID、action kind、repository、Issue / PR番号、expected head SHA、payload hash、schema versionへbindし、submitはこのbindingと一致する場合だけ受理する。submitはone-time nonceで一度だけconsumeし、同一内容の再送は冪等に扱い、内容の異なる重複submitは停止する。

| ID | 受入条件 |
| --- | --- |
| AC-C08-01 | 同一の対話sessionのcontextを維持したまま複数roundを進行できる |
| AC-C08-02 | 主経路でClaude subprocessを起動せず、TUIへのキー入力注入も行わない |
| AC-C08-03 | 同一runに対し、advance / submitを繰り返す以外の制御経路が存在しない |
| AC-C08-04 | headless経路と主経路が、同一シナリオで同じstate遷移を通る |
| AC-C08-05 | stale action、異なるhead、異なるrun、異なるaction kind、path traversal、symlink経由path、size超過result、hashの異なる重複submitを、いずれも受理せず停止する |
| AC-C08-06 | 中断後に別processからresumeして継続できる |
| AC-C08-07 | `HOST_ACTION`へ、検証済みrecordのcomment IDと対象head SHAを含めて渡す |

### C-09 Codex fresh runtimeと隔離checkout

`durable read-only`は「隔離checkout内でのtest / build / 再現に必要な一時書込は許可し、実repositoryとGitHubへの永続変更は禁止する」を意味する。隔離checkoutは対象headから新規作成し、review後に破棄する。破棄前のdirty stateをevidenceとして記録する。session memoryを引き継がず、canonical conversationとfinding ledgerからcontextを毎回再構築する。read-only Web調査を許可するprofileでも、GitHub write credentialへ到達できないようにする。

| ID | 受入条件 |
| --- | --- |
| AC-C09-01 | 隔離checkout内の一時書込が成功し、review終了後にcheckoutが残らない |
| AC-C09-02 | reviewerからの実repository書込とGitHub mutationが失敗する |
| AC-C09-03 | 同一headに対する2回のreviewが、前回session状態に依存しない |
| AC-C09-04 | 隔離checkoutのHEAD、PRのadvertised head、review出力の対象headが一致する |
| AC-C09-05 | read-only Web調査を許可したprofileでも、GitHub write credentialへ到達できない |

### C-10 PR mode review loop

review / fixの最大roundを設定可能とし既定3。承認は対象head SHAへbindする。PR lockにより同一PRへの同時runを防ぐ。

| ID | 受入条件 |
| --- | --- |
| AC-C10-01 | 実PRに対しdry-runでreview→fix→re-reviewが1 round完走する |
| AC-C10-02 | round上限到達で`BLOCKED`へ遷移する |
| AC-C10-03 | 同一PRへの同時runが拒否される |
| AC-C10-04 | 中断後に別processからresumeして継続できる |
| AC-C10-05 | 全reviewerが同一head・同一roundで承認したことを記録し、異なるheadの承認を混在させない |
| AC-C10-06 | coder snapshotを記録し、coderによるhead更新と外部からのhead更新を区別する |

### C-11 decision / clarification / follow-up

clarification counterはGitHub上の`run ID + fingerprint + turn` metadataから再構築し、head SHAだけが変わってもresetしない。review / fixの最大roundとclarification turnを別counterで管理する。clarification中は対象headを固定し、source変更・commit・pushを行わない。follow-up候補は最大3件。Issue作成はControllerだけが行い、候補ごとの許可をcandidate fingerprintとIssue本文hashへbindする。不許可・未回答・作成失敗はfinal reportへ記録するが、非blockingなためmerge承認を無効化しない。

| ID | 受入条件 |
| --- | --- |
| AC-C11-01 | 5 turn上限と5つの早期終了条件を個別にtestできる |
| AC-C11-02 | 同一topic判定がhead変更をまたいで維持される |
| AC-C11-03 | 許可のない候補がIssue化されない |
| AC-C11-04 | Issue本文の意味的変更後に旧許可が失効する |
| AC-C11-05 | 候補が最大3件へdeduplicateされ、候補ごとのCodex verdictが記録される |
| AC-C11-06 | 許可された候補について、作成したIssue URLが元conversationへ記録される |
| AC-C11-07 | `ASK_USER` / `PROCEED_WITH_RECORD` / `REVISE_AND_RESUBMIT`の各verdictで、期待するstate遷移になる |

### C-12 qualificationとfinal reporter

CI pendingは設定可能なbounded foreground wait（既定20分・30秒間隔）とし、上限後は`WAITING_CI`で終了する。final reportはschema検証済みJSONを正とし、Markdown・PR comment・terminal summaryをそこから決定論的にrenderする。言語はrepository設定→user設定→組込み既定（日本語）の順に解決し、未対応値はvalidation errorとする。final reporterはC-09のread-only runtimeで実行する。

| ID | 受入条件 |
| --- | --- |
| AC-C12-01 | CI timeout時に`WAITING_CI`のcheckpointが残り、明示resumeで再開できる |
| AC-C12-02 | 同一JSONから同一Markdownが再現される |
| AC-C12-03 | report生成失敗時に、review承認を保持したまま`REPORT_FAILED`へ遷移する |
| AC-C12-04 | approved headでlocal final testとrequired CIの両方がsuccessでなければ、次のstateへ進まない |
| AC-C12-05 | final reportが、変更内容、test結果、review履歴、残存riskをすべて含む |
| AC-C12-06 | final reportが、follow-up候補ごとのCodex評価、permission状態、作成済みIssue URLを含む |
| AC-C12-07 | PR comment、local JSON、local Markdown、terminal summaryの4出力が同一JSONから生成される |

### C-13 human merge gate

intentは`QUESTION` / `REQUEST_CHANGES` / `APPROVE_MERGE` / `CANCEL`。曖昧な肯定、過去の承認、別PRへの承認から`APPROVE_MERGE`を推論しない。自然言語＋明示確認を主経路とし、次のcommandを補助・復旧経路として併用する（D-028）。3引数すべてを必須とする。

```text
cc-review approve-merge <pr> --repo OWNER/REPO --head <sha> --method <merge|squash|rebase>
```

承認はrepository、PR番号、approved head SHA、merge methodへbindし、いずれかが変われば失効する。merge直前にPR open状態、現在head、review承認、local test、CI、未解決事項、mergeabilityを再取得して照合する。merge APIがtimeoutまたは不明な結果を返した場合は、再送前にPR stateとmerged commit SHAを照会する。

| ID | 受入条件 |
| --- | --- |
| AC-C13-01 | 曖昧入力が`APPROVE_MERGE`にならない |
| AC-C13-02 | head変更後に旧承認でmergeできない |
| AC-C13-03 | merge API timeout後に二重mergeが発生せず、確認できない場合は成功と表示しない |
| AC-C13-04 | 許可方式が複数で未設定の場合に、mergeせずユーザー判断を求める |
| AC-C13-05 | 中断後に別processからresumeして継続できる |
| AC-C13-06 | `READY_FOR_HUMAN_MERGE`到達時にmergeを実行せず、ユーザー入力を待機する |
| AC-C13-07 | `QUESTION`で回答を記録してgateを維持し、`REQUEST_CHANGES`で承認を失効して`CHANGES_REQUESTED`へ戻る |
| AC-C13-08 | approval recordが、repository、PR番号、approved head SHA、merge method、入力経路、comment IDを含む |
| AC-C13-09 | merge直前に、PR open状態、現在head、review承認、local test、CI、未解決事項、mergeabilityをすべて再取得して照合する |
| AC-C13-10 | merge実行後にGitHubからmerged stateとmerged commit SHAを再取得し、確認できた場合だけ`MERGED`を表示する |

### C-14 Issue modeとhandoff

既存PRがある場合は新規作成せず、handoffを両側へ冪等に記録してからPR modeへ合流する。Issue側とPR側のhandoff recordを1つのtransactionとして扱い、片側だけ成立した状態から再開できるようにする。conversation sourceの切替は両側のrecord確認後に行う。

| ID | 受入条件 |
| --- | --- |
| AC-C14-01 | 既存PRがある場合に新規作成せず合流する |
| AC-C14-02 | handoffの二重投稿が発生しない |
| AC-C14-03 | 片側だけ投稿済みの状態から再開しても重複しない |
| AC-C14-04 | PR作成後にvalidationが失敗しても、発見済みPRから再開できる |
| AC-C14-05 | Issueのタイトル、本文、採用対象commentを実装要件として取得する |
| AC-C14-06 | Issue側とPR側の両方のhandoff recordを確認してからconversation sourceを切り替える |
| AC-C14-07 | 既存PRがないIssueから`IMPLEMENT_ISSUE`を実行した場合、新しいPRを1件だけ作成する |
| AC-C14-08 | 作成したPRのrepository、branch、head SHA、Issue referenceを検証してから両側handoffを確定する |
| AC-C14-09 | PR作成APIの結果が不明な場合、再作成する前に既存PRを検索し、重複PRを作らない |

### C-15 Plugin配布と任意wrapper

PluginとCLIはprotocol versionを交換し、非互換時は処理を開始せず更新方法を表示する。対象repositoryはClaude Codeの現在directoryまたは明示`--repo`から解決し、Pluginのinstall directoryと分離する。

| ID | 受入条件 |
| --- | --- |
| AC-C15-01 | 任意repositoryからSkillを起動でき、protocol version不一致を検出する |
| AC-C15-02 | wrapperを導入せずPR modeが完走し、wrapper起動失敗がrunの失敗にならない |
| AC-C15-03 | 監視paneは明示要求時だけ起動し、既定では起動しない。同一run IDで重複しない |
| AC-C15-04 | `tmux`内のrunがSSH切断後も継続し、判断地点でGitHubへ投稿・確認して終了する。無期限待機しない |
| AC-C15-05 | 再接続後に同じSkill commandでresumeでき、同一runの重複起動が拒否される |
| AC-C15-06 | `tmux`外でprocessが終了した場合、最後に確認済みのGitHub checkpointからresumeできる |
| AC-C15-07 | detached run開始前にpermission preflightを実行し、同一runの重複起動を防ぐ |

## 6. Traceability

target experienceのSection 4（DOD）とSection 13（MVP）を、componentとPhaseと受入条件へ対応付ける。

| Requirement | Component | Phase | Acceptance |
| --- | --- | --- | --- |
| DOD-01 | C-09 | 9 | AC-C09-01、AC-C09-04 |
| DOD-02 | C-05、C-06、C-08 | 5、6、8 | AC-C05-01、AC-C06-06、AC-C08-07 |
| DOD-03 | C-10 | 10 | AC-C10-01 |
| DOD-04 | C-10 | 10 | AC-C10-05 |
| DOD-05 | C-12 | 12 | AC-C12-04 |
| DOD-06 | C-12 | 12 | AC-C12-05 |
| DOD-07 | C-01、C-05、C-06 | 1、5、6 | AC-C01-03、AC-C05-01、AC-C06-06 |
| DOD-08 | C-07 | 7 | AC-C07-05 |
| DOD-09 | C-11、C-12 | 11、12 | AC-C11-03、AC-C11-05、AC-C12-06 |
| DOD-10 | C-13 | 13 | AC-C13-06 |
| DOD-11 | C-13 | 13 | AC-C13-07 |
| DOD-12 | C-06、C-13 | 6、13 | AC-C06-01、AC-C13-08 |
| DOD-13 | C-13 | 13 | AC-C13-09 |
| DOD-14 | C-13 | 13 | AC-C13-10 |
| MVP-01 | C-10 | 10 | AC-C10-01 |
| MVP-02 | C-14 | 15 | AC-C14-01、AC-C14-03、AC-C14-05〜09 |
| MVP-03 | C-15 | 16 | AC-C15-01 |
| MVP-04 | C-15 | 16 | AC-C15-01 |
| MVP-05 | C-08、C-09 | 8、9 | AC-C08-01、AC-C09-03 |
| MVP-06 | C-08 | 8 | AC-C08-04 |
| MVP-07 | C-06、C-09 | 6、9 | AC-C06-03、AC-C09-01、AC-C09-04、AC-C09-05 |
| MVP-08 | C-06 | 6 | AC-C06-04、AC-C06-10 |
| MVP-09 | C-06 | 6 | AC-C06-11 |
| MVP-10 | C-10 | 10 | AC-C10-01、AC-C10-02 |
| MVP-11 | C-07、C-10 | 7、10 | AC-C07-03、AC-C10-03、AC-C10-06 |
| MVP-12 | C-12 | 12 | AC-C12-04 |
| MVP-13 | C-12 | 12 | AC-C12-01 |
| MVP-14 | C-03 | 3 | AC-C03-01 |
| MVP-15 | C-03、C-07 | 3、7 | AC-C03-02、AC-C07-01 |
| MVP-16 | C-15 | 16 | AC-C15-03 |
| MVP-17 | C-15 | 16 | AC-C15-04、AC-C15-05、AC-C15-07 |
| MVP-18 | C-12、C-13 | 12、13 | AC-C12-05、AC-C13-06、AC-C13-08、AC-C13-09、AC-C13-10 |
| MVP-19 | C-12 | 12 | AC-C12-07 |
| MVP-20 | C-11 | 11 | AC-C11-01、AC-C11-07 |
| MVP-21 | C-01、C-05、C-06、C-07 | 1、5、6、7 | AC-C01-03、AC-C05-01、AC-C06-06、AC-C07-01 |
| MVP-22 | C-07 | 7 | AC-C07-06 |
| MVP-23 | C-13 | 13 | AC-C13-04、AC-C13-08 |
| MVP-24 | C-11 | 11 | AC-C11-03、AC-C11-05、AC-C11-06 |
| MVP-25 | C-07 | 14 | AC-C07-04 |
| MVP-26 | C-04、C-06 | 4、6 | AC-C04-01、AC-C04-03、AC-C06-06 |

設計へ影響する決定の対応。決定の全文は[target experience](target-experience.md) Section 14のdecision logにある。

| Decision | 設計への影響 | Component |
| --- | --- | --- |
| D-014 | active sessionをhostとするため、engineをstep engineにする（Section 2） | C-08 |
| D-015 | reviewerをturnごとにfresh起動する | C-09 |
| D-028 | merge承認に固定commandを併用し、3引数を必須にする | C-13 |
| D-029 | 検証対象をMSI installer配布のPowerShell 7に限定する | C-03、C-15 |
| D-030 | transportのinterfaceを新設し、再利用はcomponent単位で評価する。新しいpublic transport boundaryはraw I/OとrecordのC-05 / C-06にまたがる | C-05、C-06 |
| D-031 | ユーザー判断の受理をlogin allowlistの完全一致に限る | C-06 |

## 7. Deviations from target experience

target experienceの`Proposed`のうち、そのまま採用しないものだけを記載する。ここに無い`Proposed`は記述どおり実装する。

| Section | 内容 | 本書の扱い | 理由 |
| --- | --- | --- | --- |
| 3 | Skill mode主経路、headless CLIは補助 | 条件付き採用 | Section 2の制御反転を前提とする。両者は同じstep engineを共有する |
| 5.4 | 参考実装のcapability再利用でControllerを最小化 | 方針変更 | D-030により、再利用はcomponent単位の評価に置き換える。Controller最小化はcodeの再利用ではなく責務の限定で達成する |
| 9 | safety behavior | 具体化して採用 | login allowlist、credential隔離、OS別file権限をC-04とC-06で追加する |
| 10.1 | checkpointの保存項目 | 構造を追加して採用 | C-02のversioned envelopeへ格納し、fieldは利用するPhaseで追加する |

### Phase 0で決定する技術判断（P-001）

runtime依存をゼロに保つか、schema検証にlibraryを導入するかを決める。必要なのは本projectが定義する固定のagent出力形式の検証であり、汎用JSON Schema validatorの自作ではない。

| 候補 | 内容 |
| --- | --- |
| 1 | 依存ゼロを維持し、対応するschema機能を明示的に限定した専用protocol validatorを実装する |
| 2 | 成熟したvalidator libraryを導入する |

評価軸はsupply chain risk、配布size、Windows / Linux互換性、validationの網羅性、診断品質、malformed入力への耐性、保守cost。Phase 0で代表schema corpusとmalformed corpusを先に定義し、両案のprototypeを比較してCodex reviewを受ける。重大なtrade-offが判明した場合だけユーザーへ確認する。`pyproject.toml`が現在`dependencies = []`であることは、stub状態の事実であって要件ではない。

## 8. 実装順序

各Phaseを1つの子Issueとし、Issue #2から参照する。1 Phaseを複数のPRへ分けてよい。

| Phase | 内容 | Component | 完了条件 | 追加するcheckpoint field |
| --- | --- | --- | --- | --- |
| 0 | 基盤と品質ゲート | — | CIでlint、type、coverage、size ratchetが動作する。P-001を決定する | — |
| 1 | domain state machine | C-01 | AC-C01-01〜03 | — |
| 2 | protocol schemaとenvelope | C-02 | AC-C02-01〜03 | envelope schemaとmigration policyを定義（永続化はPhase 5以降） |
| 3 | process abstraction | C-03 | AC-C03-01、AC-C03-02 | — |
| 4 | security policy | C-04 | AC-C04-01〜03 | — |
| 5 | GitHub transport | C-05 | AC-C05-01〜05 | conversation cursor、comment / review ID、URL、本文hash、idempotency marker |
| 6 | canonical record検証とcredential隔離 | C-06 | AC-C06-01〜11 | record sequence high-water mark、既知comment ID、permission mode / profile、Permission ID、blockされたtool、要求scope、承認bind情報 |
| 7 | resume | C-07 | AC-C07-01〜03、AC-C07-05、AC-C07-06 | run ID、state、base / observed / approved head SHA、PR lock、coder snapshot |
| 8 | active host protocolとstep engine | C-08 | AC-C08-01〜07 | action ID、未完了`HOST_ACTION`、nonce、submit状態 |
| 9 | Codex fresh runtimeと隔離checkout | C-09 | AC-C09-01〜05 | 隔離checkout、sandbox / network profile、実行したtest / build、dirty status、破棄結果 |
| 10 | PR mode review loop | C-10 | AC-C10-01〜06。**CLI経由のdogfooding開始** | round、finding ledgerとresolution、coder実行前後HEAD、push後head |
| 11 | decision / clarification / follow-up | C-11 | AC-C11-01〜07 | clarification counter、fingerprint、未解決decision request、follow-up候補と許可record |
| 12 | qualificationとfinal reporter | C-12 | AC-C12-01〜07 | test command / result、GitHub check名 / result / URL、artifact path |
| 13 | human merge gate | C-13 | AC-C13-01〜10 | merge gate intent、approved head SHA、入力経路、approval comment ID、merge method、API結果、merged commit SHA |
| 14 | retention、migration、salvage | C-07 | AC-C07-04。全fieldがenvelope schemaと整合し、旧versionがmigrationで読める | 横断検証。新規fieldは追加しない |
| 15 | Issue modeとhandoff | C-14 | AC-C14-01〜09 | Issue番号、handoff record、conversation source切替状態 |
| 16 | Plugin配布と任意wrapper | C-15 | AC-C15-01〜07 | wrapper session ID、監視pane状態 |
| 17 | release acceptance | 全体 | 下記scenario matrix（S-1〜S-9） | — |

### 順序の根拠

- Phase 0が先行するのは、lintとtype gateを後から入れると既存code全体の修正が必要になるため
- Phase 2のschemaがtransportより前なのは、checkpoint envelopeとagent protocolの所有者を1箇所に固定し、後続Phaseがそれぞれ独自形式を持つのを防ぐため
- Phase 4のsecurity policyがtransportより前なのは、transportが投稿前redactionを利用するため。actorの解決とrecord検証はGitHub metadataを必要とするためPhase 6へ置く。Phase 5の成果物は未検証metadataを扱うI/Oであり、canonical recordとしての受理判定はPhase 6で成立する。Phase 7以降のcomponentは検証済みrecordだけを入力にする
- Phase 8のactive host protocolがPR modeより前なのは、Section 2の制御構造が全workflowの前提であり、後から反転すると全workflowを書き直すことになるため
- checkpoint fieldは、それを利用するPhaseと同じPhaseで追加する。Phase 14は新規fieldを追加せず、retention、schema整合性、migration、salvageの横断検証に充てる
- dogfoodingとユーザー操作を伴うPhase 8、10、13は、中断後に別processからresumeする受入testを完了条件に含める。同一process内のresumeだけでは、新しいClaude Code sessionからの再開を保証できない
- Phase 16のwrapperが後段なのは、wrapperなしでcore loopが動作することが設計条件であるため。ただし初回releaseには含める

### Phase 17 release acceptance

| ID | Scenario | 期待する終了state | 検査するevidence |
| --- | --- | --- | --- |
| S-1 | Windows、active Skill、PR mode、承認してmerge | `MERGED` | review record、対応summary、final reportの4出力、approval record、merged commit SHAの再確認 |
| S-2 | Windows、headless CLI、PR mode | `READY_FOR_HUMAN_MERGE` | S-1と同じ種類のrecordが同じ形式で残る（AC-C08-04） |
| S-3 | Linux/SSH、active Skill、Issue mode（既存PRなし） | `READY_FOR_HUMAN_MERGE` | 新規PRが1件だけ作成されたこと、Issue / PR双方向のhandoff record、review履歴 |
| S-4 | Linux/SSH、`tmux`内でSSH切断、判断地点へ到達 | `AWAITING_USER_DECISION` | decision briefが投稿・確認済み。processが無期限待機していない |
| S-5 | S-4から再接続してresume | `READY_FOR_HUMAN_MERGE` | 同一run IDで継続し、重複run・重複commentが無い |
| S-6 | wrapper未導入環境でPR mode | `READY_FOR_HUMAN_MERGE` | wrapperなしでもS-1と同じrecordが残る |
| S-7 | `tmux`外で切断しprocess終了、再接続してresume | 再開後にS-3と同じ地点 | 最後に確認済みのcheckpointから再開したことが確認できる |
| S-8 | security negative: allowlist外authorの承認、reviewerからのGitHub mutation、偽record | 進行しない | AC-C06-01、AC-C06-03、AC-C06-06が実環境で成立する |
| S-9 | failure: CI timeout、report失敗、merge API timeout | `WAITING_CI` / `REPORT_FAILED` / `MERGE_FAILED` | 各stateのcheckpointと、二重merge・成功誤表示が無いこと |

各scenarioで、GitHub上に期待するcanonical recordが残っていることを証跡とする。WindowsのscenarioはMSI installerまたは`Microsoft.PowerShell` winget packageからprovisionした環境で実行し、PowerShellのversionとinstall sourceを証跡へ含める（D-029）。

初回releaseの条件はS-1〜S-9をすべて通すこと。Phase 16までの完了は必要条件であり、十分条件ではない。

## 9. 品質ゲート

| ゲート | 内容 | 導入 |
| --- | --- | --- |
| test | `python -m pytest -q`。live serviceへ接続せず実行できる（P-011） | Phase 0 |
| coverage | baselineを測定しratchetする。subprocess coverageを有効化する | Phase 0 |
| lint | ruff | Phase 0 |
| type | mypy。Phase 0で対象package、許容する例外、ratchet方法を記録し、以後は緩めない | Phase 0 |
| module size | 固定行数の即時failではなくbaselineからのratchet。責務数、循環依存、複雑度、testabilityと併せてreviewし、例外はPRで根拠とともに認める | Phase 0 |
| CI matrix | Ubuntu / Windows × Python 3.11 | 導入済み |
| contract test | SPDX表示、repository参照、CLI名称、禁止flag（P-006）、baseline link | 一部導入済み |
| version | `pyproject.toml`のversionとgit tagを同期する（P-012） | Phase 0 |

CIは常に全testを実行する。regression testは対象のIssue番号を参照する。

## 10. Riskと未決事項

| ID | Risk | 緩和 |
| --- | --- | --- |
| R-01 | Codex / Claude CodeのCLI interfaceが変わる | runtime層へ隔離し、versionを起動時に確認する。CLI固有の分岐をworkflow層へ漏らさない |
| R-02 | GitHub APIのrate limitがreview loopの実用性を下げる | 取得を差分cursorへ限定する。C-12のbounded CI waitを除き、恒常的なpollingを行わない |
| R-03 | 対象repositoryのCI所要時間がbounded waitを超える | 待機上限をrepository設定で調整可能にする。resumeを軽量に保つ |
| R-04 | HOST_ACTIONの粒度が粗すぎる / 細かすぎる | Phase 8でPR modeの1 roundを通して粒度を検証してからPhase 10へ進む |
| R-05 | 単一core engineの制約がSkillとCLIの要求差で崩れる | entry pointに置いてよい責務を3つへ限定し、AC-C08-04で検出する |
| R-06 | credential隔離がOSまたはCLI versionの差で破れる | AC-C06-03を両OSのCIで常時実行する。隔離手段を1箇所へ集約する |

未決事項:

- P-001のruntime依存方針（Phase 0）
- checkpoint envelopeのversioning方式とmigration policyの詳細（Phase 2）
- coverage floorとmodule size baselineの初期値（Phase 0）
- HOST_ACTIONの最終的な種類と粒度（Phase 8）
- protocol versionの表現形式と互換range（Phase 16）
