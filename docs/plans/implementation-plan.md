<!-- SPDX-License-Identifier: Apache-2.0 -->

# Implementation plan

| Field | Value |
| --- | --- |
| Status | **Draft** |
| Baseline | [target-experience.md](target-experience.md)（Status: Agreed） |
| Parent roadmap | Issue #2 |
| Owner | Mega-Gorilla |
| Last updated | 2026-08-18 |

## 1. この文書の目的と範囲

[target-experience.md](target-experience.md)はユーザーから見た完成状態を定義した合意済みのgold documentである。本書はそれを**どのcomponent、どの依存関係、どの順序で作るか**を定義する。

| 文書 | 答える問い |
| --- | --- |
| target-experience.md | 何を作るか。操作、表示、停止条件、復旧、最終成果物 |
| 本書 | どう作るか。設計原則、component、依存、実装順序、品質ゲート |

本書の役割は3つある。

1. target-experienceの`Proposed`（implementation planで検証する実装詳細）を確定させる
2. `Open`（ユーザー判断または技術検証が必要）へ推奨案とdecision briefを提示する
3. componentと依存関係を定義し、実装子Issueの発行根拠にする

### 本書で決めないこと

- **target behaviorの変更**は行わない。変更が必要な場合はSection 8のdecision briefとして提示し、ユーザーの明示回答後にtarget-experienceのdecision logへ反映する
- **ユーザー判断が必要な事項の確定**は行わない。本書が単独で`Open`を`Decided`へ変更することはない
- **各componentのAPI詳細設計**は行わない。本書はcomponentの責務境界（seam）の位置までを固定し、signature levelの設計は各子IssueのPRで行う

### 用語

target-experienceのSection 2と同じ`Decided` / `Proposed` / `Open` / `Superseded`を使用する。本書が導入する実装原則は`P-NNN`、componentは`C-NN`、実装phaseは`Phase N`で参照する。

## 2. 設計原則

### 2.1 原則の導き方

本書の設計原則は、target-experienceが要求する性質と、それが破れたときに起きる失敗から導出する。

参考実装（[ADR-0002](../decisions/0002-independent-reimplementation.md)に出典を記録）の調査結果は、**同種の設計でどの失敗が実際に起きるかを示す傍証**として併記する。原則の正当性は本projectの要件から自立して説明し、外部の評価を根拠にしない。

傍証として参照するaudit文書について、次の点を明示する。

| 項目 | 内容 |
| --- | --- |
| 所在 | 参考実装repository内の文書 |
| Auditor表記 | Anthropic Claude（Claude Code） |
| 対象commit | `d3faf6c` |
| 現行との乖離 | audit記載の`orchestrator.py` 5,573行に対し、本調査時点のcloneでは8,971行 |

したがってこのauditは独立した第三者保証ではなく、対象commitも現行から乖離している。総合gradeを保証として扱わず、**個別に再現確認できた観測事実だけ**を引用する。

### 2.2 原則

| ID | 原則 | 本projectでの必要性 | 傍証（参考実装で観測された失敗） |
| --- | --- | --- | --- |
| P-002 | **単一のcore engineを持つ**。active host経路とheadless経路が同じengineを共有し、round orchestrationを二重実装しない | target-experienceはSkill主経路とheadless CLI補助経路の両方を要求する（D-014）。両者が別のorchestrationを持つと、GitHub永続化gate（Section 5.3）やclarification上限（D-011）の変更が片方に反映されず、同じ状態遷移が経路によって異なる結果になる | CLI用とSkill用でround orchestrationが二重実装され、protocol変更のたびに両方へ反映する必要が生じていた |
| P-003 | error分類をexit code、構造化出力、`gh api`のstatusで行う。出力文字列の部分一致による分類を禁止する | 一時障害の誤判定はretryとstate遷移を直接誤らせる。本projectはreview本文そのものを扱うため、`timeout`や`quota`を論じるreview文が分類対象へ混入する | `quota` / `timeout` / `overloaded`等の語をfree-textから拾って一時障害と判定していた。HTTP statusも`"404"`の部分一致で判定していた |
| P-004 | GitHubからの取得は必ず`--json`の構造境界で行う。出力の行分割で本文を切らない | metadata markerに複数行JSONを格納するため、行分割前提の取得では成立しない。read-after-writeの本文hash照合も本文が壊れれば成り立たない | comment本文を`--jq '.[].body'`で取得して改行分割しており、複数行bodyが1行ずつ別fragmentになっていた |
| P-005 | GitHubへ投稿する本文はfile経由で渡す。本文をcommand line引数へ置かない | final reportとdecision briefは長文になる。Windowsのcommand line長制限へ到達すると、投稿が環境依存で失敗する | Skill helper層が`--body`直渡しだった（coreの投稿関数は既にfile経由を使用しており、これはhelper層固有の問題） |
| P-006 | permission bypass flagをcode上で構築しない。禁止語のcontract testを置く | target-experience Section 9は`bypassPermissions`と`--dangerously-skip-permissions`相当をpresetから使用不可にすることをDecidedとしている。code上に構築経路が存在すれば、設定や分岐次第で有効化され得る | skill modeがbypass flagを無条件付与し、文書上の「bypassはopt-in」という説明と実挙動が矛盾していた |
| P-007 | ユーザー決定として受理するGitHub commentを、設定されたGitHub loginのallowlistで制限する | 本projectはmerge承認、仕様判断、follow-up Issue許可をGitHub commentから読む。受理条件が緩いと、書込可能な別ユーザーがworkflow authorityを取得できる。詳細はSection 6のC-06 | 本文末尾の署名行だけで「人間の要求」と判定しており、書込可能repositoryでは誰でも詐称できた |
| P-008 | promptへ埋め込むGitHub由来のtextをfenceし、データであって指示ではないと明示する | Issue本文、comment、review threadはすべて外部入力であり、そのままagentのinstructionとして解釈され得る | Issue本文やcommentを区切りなくpromptへ埋め込んでいた |
| P-009 | artifactはrunごとのdirectoryへ、作成者のみがアクセスできる権限で作成する。予測可能な共有pathを使わない。権限表現をOSごとに定義する | artifactにはprivate repositoryのdiffとreview内容が含まれる。POSIXのmode指定だけではWindowsで保護されない | 予測可能な`/tmp`配下のpathへdefault umaskでagent responseを書いていた |
| P-010 | ruffとmypyをPhase 0からCIへ入れる | 後から導入すると既存code全体の修正が必要になり、導入自体が先送りされる | 22,000行規模でlint / type gateを持たなかった |
| P-011 | testはagent CLIと`gh`をprocess境界でfakeし、外部依存ゼロで全件実行できる状態を保つ | 本projectのtestは、実行するとGitHubへ書き込む処理を対象にする。process境界のfakeがなければ、CIでの全件実行が成立しない | agent CLIと`gh`をprocess境界でfakeすることで、外部依存なしに全testが動作していた |
| P-012 | versionとgit tagを最初から運用する | PluginとController CLIはprotocol versionを交換する（D-026）。versionが実体を持たないとこの機構が機能しない | 400 commit以上にわたりversionが固定でtagもなかった |
| P-013 | すべてのcodeを`src/`配下のpackageへ置き、`sys.path`操作を禁止する。外部へはconsole entry pointで公開する | Controllerは任意repositoryから呼べるinstall済みpackageであることが要件（D-026）。repository相対pathへ依存すると成立しない | helper群がpackage外にあり、`sys.path.insert`でpackage内部のprivate関数を参照していた |
| P-014 | subprocess呼び出しは必ずlist形式のargvで行う。`shell=True`、`os.system`、`eval`、`exec`を使用しない | agentへ渡す値にはIssue本文やreview結果が含まれ、shell経由ではinjection面になる | 全subprocess呼び出しがlist形式で、shell injection面が存在しなかった |
| P-015 | 本projectのcodeでcredentialを保持しない。認証は認証済みCLIへ委譲し、agentごとの到達可能範囲をSection 6のC-06で明示的に制御する | 「渡さない」だけではsubprocessが環境変数やhome配下の設定へ到達できる。委譲と隔離は別の設計項目である | state fileやlogへ秘密情報が入らない設計だった |

`P-001`は依存方針に関する提案であり、原則ではなくSection 8のユーザー判断待ち事項として扱う。

### 2.3 P-002の意味

target-experienceは、対話型Claude Code Skillを主経路、`cc-review` headless CLIを補助・復旧経路と定めている（D-014）。2つのentry pointが存在すること自体は要件である。

本書が禁止するのは、**2つのentry pointがそれぞれround orchestrationを実装すること**である。entry pointが持ってよいのは、引数解析、session boundaryの受け渡し、表示の3つに限る。state遷移、round管理、GitHub投稿の判断はすべてcore engineに置く。

## 3. active host protocol

本節はP-002を実現するための制御構造を定義する。**本書で最も後戻りの大きい設計であり、他のcomponentはこの前提の上に構築する。**

### 3.1 制約

D-014は、既存の対話型Claude Code sessionがcontextを維持したままhost / coderを担当することを要件としている。ここから2つの制約が導かれる。

1. Controller CLIはClaude Code sessionの**子process**として起動される。親のLLM turnを後から呼び戻して作業させることはできない
2. ControllerからTUIへのキー入力注入は禁止されている（D-003で維持）

したがって「core engineが長時間loopを回し、必要になったらClaudeを呼ぶ」構造は成立しない。その構造を採ると、Claudeをfresh subprocess化してactive sessionのcontextを失うか、Skill側へ別のorchestrationを実装してP-002へ違反するかのいずれかになる。

### 3.2 制御の反転

core engineを、**resume可能なstep engine**として定義する。engineはClaudeを起動しない。次に何をすべきかを返し、呼び出し側が実行する。

```text
engine.advance(run_id) -> 次のいずれか

  HOST_ACTION   active hostが実行すべき作業（構造化）
  AWAIT_USER    ユーザー入力待ち（AWAITING_USER_DECISION / READY_FOR_HUMAN_MERGE 等）
  TERMINAL      終了state（MERGED / BLOCKED / FAILED / CANCELLED 等）
```

主経路の流れは次のとおり。

```text
1. Skill内のactive Claudeが  cc-review advance --run <id>  を呼ぶ
2. engineがGitHubとcheckpointからstateを再構築し、次のHOST_ACTIONを返す
3. active Claudeが自身のcontextでそのactionを実行する
4. 結果を  cc-review submit --run <id> --action <id> --result <file>  でControllerへ渡す
5. Controllerがschema検証 -> GitHubへ投稿 -> read-after-write確認 -> checkpoint更新
6. 1へ戻る
```

engineは1回のadvanceで1つのactionだけを返し、その間に状態を進めない。actionの結果がGitHubへ永続化され確認されるまで、次のactionは決まらない（Section 5.3のgate）。

### 3.3 HOST_ACTIONの種類

初期案。Phase 6で確定する。

| Action | active hostが行うこと |
| --- | --- |
| `APPLY_FINDINGS` | blocking findingを評価し、修正・test・commit・pushする |
| `ASK_CLARIFICATION` | Codex findingへの質問文を作成する |
| `ANSWER_CLARIFICATION` | Codexからの確認要求へ回答を作成する |
| `DRAFT_DECISION_REQUEST` | 判断が必要な理由、候補、推奨をdraftする（D-010） |
| `DRAFT_DECISION_BRIEF` | Codex verdictを反映した最終briefを作成する |
| `DRAFT_FOLLOWUP_CANDIDATES` | 重複確認済みのfollow-up候補を最大3件draftする（D-024） |
| `STRUCTURE_USER_INTENT` | merge gateのユーザー入力を4 intentへ構造化する（D-013） |
| `RUN_LOCAL_TESTS` | 対象headでlocal testを実行し結果を返す |
| `IMPLEMENT_ISSUE` | Issue要件から実装しPRを作成する（Issue mode） |

### 3.4 agentごとの起動主体

| Agent | 起動主体 | 理由 |
| --- | --- | --- |
| Claude coder（主経路） | 起動しない。active hostがHOST_ACTIONを実行する | active sessionのcontext維持がD-014の要件 |
| Claude coder（headless経路） | Controllerがsubprocketとして起動するadapter | 対話sessionが存在しない復旧経路のため。engineから見たinterfaceは主経路と同一 |
| Codex reviewer / final reporter | Controllerがfresh subprocessとして起動する | read-onlyであり親contextを必要としない。D-015がfresh sessionを要求する |

headless adapterとactive host adapterは別実装とし、engineは両者を同じ`HOST_ACTION`の実行者として扱う。engineにどちらの経路かを分岐させない。

### 3.5 受入条件

- 同一の対話sessionのcontextを維持したまま、複数roundを進行できる
- 主経路でClaude subprocessを起動しない
- TUIへのキー入力注入を行わない
- 同一runに対し、advance / submitを繰り返す以外の制御経路が存在しない
- headless経路と主経路が同じengine・同じstate遷移を通ることを、両経路の同一シナリオtestで確認できる

## 4. 層構造とpackage layout

### 4.1 層

| 層 | 責務 | 副作用 | 依存 |
| --- | --- | --- | --- |
| domain | state machine、event、command、ledger | なし（純粋関数） | なし |
| schema | agent入出力の定義と検証 | なし | なし |
| process | subprocess起動・停止のOS抽象 | process | なし |
| transport | GitHub canonical conversationの投稿・取得・検証 | GitHub | domain、schema、process |
| security | trust判定、permission profile、credential隔離 | 環境変数、filesystem | process |
| runtime | 隔離checkout、Codex runtime、host adapter | process、filesystem | process、security |
| workflow | step engineと各protocol | transport、runtime経由のみ | 全層 |
| state | checkpoint、resume、artifact retention | filesystem | domain、transport、security |
| entrypoint | 引数解析、session boundary、表示 | 標準入出力 | workflow |

domain層は副作用を持たず、`transition(state, event) -> (state, [command])`の形で次に行うべきことをcommandとして**記述する**。commandの**実行**はworkflow層が行う。これによりstate遷移の全経路を純粋関数のtestで網羅できる。

### 4.2 package layout

```text
src/claude_code_codex_review_loop/
  __init__.py
  errors.py                 構造化error分類（P-003）
  config.py                 repository設定 -> user設定 -> 組込み既定値
  domain/                   states / events / commands / machine / ledger / ids
  schema/                   validate / review / decision / followup / report / merge
  process/                  spawn / terminate / job_object / process_group
  transport/                gh / conversation / render / metadata / threads
  security/                 trust / permission / credentials / fs_permissions
  runtime/                  checkout / codex / host_active / host_headless / prompt
  workflow/                 engine / actions / pr_mode / issue_mode / clarification /
                            decision / followup / qualification / reporter / merge_gate
  state/                    checkpoint / resume / retention
  cli.py                    console entry point `cc-review`
```

`plugin/`にはSKILL.mdと薄いlauncherだけを置く。workflow判断を持たず、install済みCLIをprotocol version交換のうえ呼び出す（D-026）。

`wrappers/`にはWindows Terminalと`tmux`の任意wrapperを置く。wrapperなしでcore loopが動作することを設計条件とする（D-017）。

## 5. Component一覧

`docs/architecture/README.md`が挙げていた10項目に対し、Section 3の制御構造とSection 6のsecurity要件を反映して13 componentへ再編する。順序矛盾の原因になっていた2項目を前段と後段へ分割し、横断的なsecurity境界を独立componentとして追加した。

| ID | Component | architecture項目 | 依存 | Phase |
| --- | --- | --- | --- | --- |
| C-01 | domain state machine、event、command | 1 | なし | 1 |
| C-02 | process abstraction（Windows / POSIX） | 10（前段） | なし | 2 |
| C-03 | GitHub canonical conversation transport | 2 | C-01、C-02 | 3 |
| C-04 | trust、permission、credential境界 | 新規（横断） | C-02、C-03 | 4 |
| C-05 | checkpoint、resume、artifact retention | 9 | C-01、C-03、C-04 | 5、12 |
| C-06 | active host protocolとstep engine | 3（前段） | C-01、C-03、C-05 | 6 |
| C-07 | Codex fresh reviewer runtimeと隔離checkout | 4 | C-02、C-04、C-06 | 7 |
| C-08 | PR mode review loop | 5（前段） | C-06、C-07 | 8 |
| C-09 | decision / clarification / follow-up protocol | 6 | C-06、C-08 | 9 |
| C-10 | test・CI qualificationとfinal reporter | 7 | C-05、C-07、C-08 | 10 |
| C-11 | human merge gate | 8 | C-04、C-10 | 11 |
| C-12 | Issue modeとIssue-to-PR handoff | 5（後段） | C-08、C-09 | 13 |
| C-13 | Plugin配布と任意wrapper | 3（後段）、10（後段） | C-02、C-06 | 14 |

### 5.1 依存グラフ

```text
  C-01 domain          C-02 process
     |    \             /    |    \
     |     \           /     |     \
     |      C-03 transport   |      C-13 plugin / wrapper
     |       /     |          \      |
     |      /   C-04 security   \    |
     |     /      /      \       \   |
     C-05 state  /        C-07 codex runtime
          \     /             /
           C-06 host protocol
                |      \
           C-08 PR mode  \
             /     \      \
        C-09 protocols  C-10 qualification / reporter
             |               |
           C-12 issue      C-11 merge gate
```

## 6. Component定義

各componentについて責務、主要な決定、観測可能な受入条件を示す。API詳細は各子IssueのPRで設計する。

### C-01. domain state machine、event、command

- **責務**: target-experience Section 7の17 stateと全遷移を、副作用のない関数として表現する
- **主要な決定**: 遷移関数は`(state, event) -> (state, [command])`。GitHub永続化とread-after-write確認の完了をeventとして要求し、gate未通過の遷移を表現できないようにする
- **受入条件**: 17 stateすべてが到達可能。Section 7の遷移図と遷移表をdata drivenで照合できる。未定義遷移と到達不能stateをtestが検出する

### C-02. process abstraction（Windows / POSIX）

- **責務**: 子process treeの起動・停止・timeoutをOS差分を吸収して提供する
- **主要な決定**: WindowsはJob Object相当、POSIXはprocess groupで子孫を確実に停止する。argvはlist形式のみ（P-014）。sleepは標準libraryで行い外部binaryへ依存しない
- **受入条件**: 両OSのCIで、timeoutとCtrl+C後に孫processが残らないことをtestが確認する。Ctrl+C 1回でgraceful、2回目で強制停止

### C-03. GitHub canonical conversation transport

- **責務**: agent発言とユーザー決定をGitHubへ代理投稿し、再取得して一致を確認する（Section 5.3）
- **主要な決定**: 投稿後にcomment / review ID、URL、本文hash、対象head SHAを取得できるまでturnをcompletedにしない。timeout時は成否を推測せずidempotency markerで検索してから再投稿する
- **主要な決定**: 取得は`--json`境界（P-004）。投稿本文はfile経由（P-005）。metadata markerは`CC_REVIEW_META`とし、HTML comment内のJSONとして格納する。単一行前提を置かない
- **主要な決定**: review threadの取得、解決状態の判定、line findingへのreplyを扱う。threadを作成できない場合は元comment URLを含むconversation commentへfallbackする
- **主要な決定**: 公開用renderは発言者とmodelを明示し、credential redactionを投稿前に適用する
- **受入条件**: fake `gh`に対し、投稿→再取得→hash一致→ID記録が検証できる。timeout後の再投稿で重複commentが発生しない。canonical commentが改変された場合にsilent repairせず停止する。redaction対象の文字列が投稿本文へ現れないことをtestが確認する

### C-04. trust、permission、credential境界

target-experience Section 9のsafety behaviorを、agentごとの到達可能範囲として具体化する。

#### ユーザー本人の識別

- **主要な決定**: ユーザー決定として受理するcommentは、repository設定またはuser設定で許可した**GitHub loginのallowlistと完全一致**する場合に限る。`authorAssociation`とrepository permissionは追加条件として使い、単独の判定根拠にしない
- **主要な決定**: bot accountからのcommentをユーザー決定として受理しない
- **主要な決定**: 受理したcommentのbody hashを記録し、編集・削除された場合はその決定を失効させる。allowlistから外れたloginの過去決定も、以後のgate通過根拠にしない
- **主要な決定**: 承認はintent、repository、Issue / PR番号、head SHA、merge method、candidate fingerprintへbindする
- **受入条件**: allowlist外のloginが投稿した承認commentがmerge、follow-up Issue作成、仕様判断のいずれにも使われないことをtestが確認する

#### Codex reviewerのdurable read-only

「write credentialを渡さない」を、到達可能範囲の制御として定義する。

- **主要な決定**: reviewer環境から`GH_TOKEN` / `GITHUB_TOKEN`等のtoken変数を除去する
- **主要な決定**: `GH_CONFIG_DIR`、HOME相当（`HOME` / `USERPROFILE`）、`XDG_CONFIG_HOME`をreviewer専用の一時領域へ差し替える
- **主要な決定**: git credential helperを無効化し、`GIT_ASKPASS` / `SSH_ASKPASS`が対話的に資格情報を取得しないようにする
- **主要な決定**: 隔離checkoutのremoteへpush可能な構成を与えない
- **受入条件**: reviewer環境からGitHub mutationと実repositoryへの書込を試みるnegative testが、いずれも失敗することを確認する

#### Claude coderのpermission

- **主要な決定**: Auto modeの利用可否を検出し、不可の場合は`acceptEdits` -> `default` -> `dontAsk`の順でfallbackする。`bypassPermissions`相当をcode上で構築しない（P-006）
- **主要な決定**: coderはfeature branchへのpushまで、merge・follow-up Issue作成・deployはController専用とする。tool permissionとworkflow承認を別のauthorityとして扱う
- **主要な決定**: 例外blockではbypassせず、Permission ID、tool / command、理由、risk、head SHA、resume方法をGitHubへ記録して`AWAITING_TOOL_PERMISSION`へ遷移する。resume時はPermission IDとheadを再検証し、停止した操作だけを再実行する
- **受入条件**: 禁止flagがargvへ現れないことをcontract testが確認する。`AWAITING_TOOL_PERMISSION`からのresumeが停止点の操作だけを再実行することをtestが確認する

#### 信頼できない入力

- **主要な決定**: fork PRまたはallowlist外authorのPRでは、agent instructions、hooks、workflow、testの実行を既定拒否する
- **主要な決定**: `CLAUDE.md`、`AGENTS.md`、`.claude/**`、`.codex/**`、`.github/workflows/**`の変更をreview入力と表示の両方で強調する
- **主要な決定**: promptへ埋め込むGitHub由来textをfenceする（P-008）
- **受入条件**: fork PRに対する既定拒否と、agent設定fileの変更検出をtestが確認する

#### file権限

- **主要な決定**: artifactとtemp fileは、POSIXでは`0600` / `0700`、Windowsでは作成者のみに限定したACLで作成する。権限設定は共通interfaceの背後でOSごとに実装する
- **受入条件**: 両OSのCIで、artifact directoryが作成者以外からアクセスできないことをtestが確認する

### C-05. checkpoint、resume、artifact retention

- **責務**: Section 10.1のcheckpoint保存、GitHubからのstate再構築、artifactの保持と削除
- **主要な決定**: resumeはGitHub canonical conversationからstateを再構築し、local checkpointはcacheとして照合する。GitHubで確認できないlocal出力を判断根拠にしない
- **主要な決定**: PR lock、coder snapshot、external head updateを保存する。head変更時は承認とreview承認を失効させ、fresh reviewへ戻す
- **主要な決定**: artifactは正常run 30日、`FAILED` / `BLOCKED` / salvage 90日。active / lock保持中のrunを除外したbounded cleanupを起動時と明示commandで行う（D-023）。保存先はrunごとのdirectoryへC-04のfile権限で作成する
- **受入条件**: 中断後のresumeが同じturn IDから再開する。質問のみ投稿済みのpartial turnで質問が重複投稿されない。外部からheadが更新された場合に旧承認が失効する。cleanupがactive runを削除しない

### C-06. active host protocolとstep engine

- **責務**: Section 3で定義したstep engineとHOST_ACTION protocolを実装する
- **主要な決定**: Section 3.2〜3.4のとおり。engineは1回のadvanceで1 actionだけを返し、結果のGitHub永続化が確認されるまで状態を進めない
- **主要な決定**: active host adapterとheadless adapterを別実装とし、engineから見たinterfaceを同一にする
- **受入条件**: Section 3.5のとおり

### C-07. Codex fresh reviewer runtimeと隔離checkout

- **責務**: review turnごとにfreshなread-only subprocessを起動し、exact headの隔離checkout内でのみ検証させる（D-015、D-025）
- **主要な決定**: 隔離checkoutは対象headから新規作成し、review後にcheckoutごと破棄する。破棄前のdirty stateをevidenceとして記録する
- **主要な決定**: session memoryを引き継がず、GitHub canonical conversationとfinding ledgerからcontextを毎回再構築する。渡す入力は現在headの完全diff、前headからの差分、過去findingとdisposition、Claudeの対応、clarification、ユーザー決定、test / CI結果
- **主要な決定**: 到達可能範囲の制御はC-04に従う
- **受入条件**: reviewerがrepositoryとGitHubへ永続変更できないことをnegative testが確認する。同一headに対する2回のreviewが、前回session状態に依存しないことを確認する

### C-08. PR mode review loop

- **責務**: Section 5.1の22 stepをstep engine上の流れとして実装する
- **主要な決定**: review / fixの最大roundを設定可能とし既定3。承認は対象head SHAへbindする。PR lockにより同一PRへの同時runを防ぐ
- **受入条件**: 実PRに対しdry-runでreview→fix→re-reviewが1 round完走する。round上限到達で`BLOCKED`へ遷移する。loop中にheadが外部更新された場合に承認が失効する

### C-09. decision / clarification / follow-up protocol

- **責務**: D-010のユーザー判断フロー、D-011のclarification protocol、D-024のApproved follow-up
- **主要な決定**: clarification counterはGitHub上の`run ID + finding / decision fingerprint + turn` metadataから再構築し、head SHAだけが変わってもresetしない。review / fixの最大roundとclarification turnを別counterで管理する
- **主要な決定**: clarification中は対象headを固定し、source変更・commit・pushを行わない。codeを変更した場合はclarificationを終了して新roundとする
- **主要な決定**: follow-up候補は最大3件、Codex評価は`CREATE_ISSUE` / `SUMMARY_ONLY` / `LINK_EXISTING` / `REVISE_AND_RESUBMIT`。Issue作成はControllerだけが行い、候補ごとのユーザー許可をcandidate fingerprintとIssue本文hashへbindする。許可後に意味的内容が変われば許可を失効させる
- **主要な決定**: 不許可・未回答・Issue作成失敗はfinal reportへ記録するが、非blockingなためmerge承認を無効化しない
- **受入条件**: 5 turn上限と5つの早期終了条件を個別にtestできる。同一topic判定がhead変更をまたいで維持される。許可のない候補がIssue化されないことをtestが確認する。Issue本文変更後に旧許可が失効する

### C-10. test・CI qualificationとfinal reporter

- **責務**: 承認headに対するlocal testとGitHub CIの確認、final reportの生成と投稿
- **主要な決定**: CI pendingは設定可能なbounded foreground wait（既定20分・30秒間隔）とし、上限後は`WAITING_CI`で終了する（D-020）
- **主要な決定**: final reportはschema検証済みJSONを正とし、Markdown・PR comment・terminal summaryをそこから決定論的にrenderする。言語はrepository設定→user設定→組込み既定（日本語）の順に解決し、未対応値はvalidation errorとする（D-019）
- **主要な決定**: final reporterはC-07のread-only runtimeで実行し、code実行を原則必要としない
- **受入条件**: CI timeout時に`WAITING_CI`のcheckpointが残り明示resumeで再開できる。同一JSONから同一Markdownが再現される。report生成失敗時にreview承認を保持したまま`REPORT_FAILED`へ遷移する

### C-11. human merge gate

- **責務**: `READY_FOR_HUMAN_MERGE`の対話gateと、明示承認後のgated merge（D-013）
- **主要な決定**: intentは`QUESTION` / `REQUEST_CHANGES` / `APPROVE_MERGE` / `CANCEL`。曖昧な肯定、過去の承認、別PRへの承認から`APPROVE_MERGE`を推論しない
- **主要な決定**: 承認はrepository、PR番号、approved head SHA、merge methodへbindし、いずれかが変われば失効する。承認者の識別はC-04のallowlistに従う
- **主要な決定**: merge直前にPR open状態、現在head、review承認、local test、CI、未解決事項、mergeabilityを再取得して照合する。merge APIがtimeoutまたは不明な結果を返した場合は、再送前にPR stateとmerged commit SHAを照会する。GitHub上でmerge完了を確認できた場合だけ`MERGED`へ遷移する
- **受入条件**: 曖昧入力が`APPROVE_MERGE`にならない。head変更後に旧承認でmergeできない。merge API timeout後に二重mergeが発生せず、確認できない場合は成功と表示しない

### C-12. Issue modeとIssue-to-PR handoff

- **責務**: Section 5.2のIssue modeとhandoff
- **主要な決定**: 既存PRがある場合は新規作成せず、handoffを両側へ冪等に記録してからPR modeへ合流する。PR作成後の失敗でIssue実装をやり直さない。conversation sourceの切替は両側のhandoff record確認後に行う
- **受入条件**: 既存PR再利用が動作する。handoffの二重投稿が発生しない。PR作成後にvalidationが失敗しても、発見済みPRから再開できる

### C-13. Plugin配布と任意wrapper

- **責務**: version付きClaude Code Pluginの配布と、Windows Terminal / `tmux`の任意wrapper
- **主要な決定**: PluginとCLIはprotocol versionを交換し、非互換時は処理を開始せず更新方法を表示する。対象repositoryはClaude Codeの現在directoryまたは明示`--repo`から解決し、Pluginのinstall directoryと分離する
- **主要な決定**: wrapperなしでcore loopが動作する。wrapper起動失敗をrunの失敗にしない。同一run IDの監視paneを重複作成しない。`tmux`内のSSH runは切断後もユーザー判断不要な範囲を継続し、安全gateでGitHubへ投稿して終了する
- **受入条件**: 任意repositoryからSkillを起動できる。protocol version不一致を検出する。wrapper未導入環境でPR modeが完走する

## 7. Traceability matrix

decisionとcomponentとphaseの対応を示す。実装子Issueはこの表を分割単位の根拠にする。

| Decision | 内容 | Component | Phase |
| --- | --- | --- | --- |
| D-001、D-002 | ユーザー起動、自動検知なし | C-06 | 6 |
| D-003 | TUIキー入力注入の禁止 | C-06 | 6 |
| D-004 | PowerShellからの操作とlog監視 | C-02、C-13 | 2、14 |
| D-005 | 無人auto-merge / deployの禁止 | C-04、C-11 | 4、11 |
| D-007、D-008 | 討議分離、docs分類 | 文書のみ | — |
| D-009 | Issue modeの要件取得と既存PR再利用 | C-12 | 13 |
| D-010 | ユーザー判断フロー | C-09 | 9 |
| D-011 | clarification protocol | C-09 | 9 |
| D-012 | GitHub canonical conversation | C-03 | 3 |
| D-013 | merge gateと`MERGED` | C-11 | 11 |
| D-014 | active Claude sessionをhost / coderとする | C-06 | 6 |
| D-015 | Codex fresh session | C-07 | 7 |
| D-016 | 初回releaseにPR / Issue両mode | C-08、C-12 | 8、13、15 |
| D-017 | 監視paneは明示要求時のみ | C-13 | 14 |
| D-018 | `tmux`内SSH継続 | C-13 | 14 |
| D-019 | final report言語の解決順 | C-10 | 10 |
| D-020 | bounded CI waitと`WAITING_CI` | C-10 | 10 |
| D-021 | comment回答は明示resume時に取得 | C-05 | 5、12 |
| D-022 | merge methodの選択 | C-11 | 11 |
| D-023 | artifact retention | C-05 | 12 |
| D-024 | Approved follow-upの許可gate | C-09 | 9 |
| D-025 | Auto modeとpermission分離 | C-04 | 4 |
| D-026 | Plugin配布とController CLI | C-13 | 14 |
| D-027 | 親roadmap Issue | 文書のみ | — |
| D-028〜D-030 | Section 8のユーザー判断待ち | C-11、C-03 | 11、3 |

Section 4の完了定義14項目との対応。

| 完了定義 | Component | Phase |
| --- | --- | --- |
| 1. Codexがread-onlyでreviewしている | C-07 | 7 |
| 2. blocking findingがcomment IDとhead SHA付きで永続化 | C-03 | 3 |
| 3. 修正後の新headが再レビューされる | C-08 | 8 |
| 4. 全reviewerが同一head・同一roundで承認 | C-08 | 8 |
| 5. 承認headでfinal testとCIが成功 | C-10 | 10 |
| 6. final reporterが変更・test・履歴・riskを説明 | C-10 | 10 |
| 7. 各turnとユーザー決定がGitHubで確認できる | C-03 | 3 |
| 8. GitHub記録とlocal artifactがapproved headへ結び付く | C-05 | 5、12 |
| 9. follow-up候補が評価と許可状態付きでreportへ記録 | C-09、C-10 | 9、10 |
| 10. `READY_FOR_HUMAN_MERGE`でmergeせず待機 | C-11 | 11 |
| 11. 質問で待機継続、修正依頼で承認無効化 | C-11 | 11 |
| 12. 明示承認がbind情報付きでGitHubへ記録 | C-04、C-11 | 4、11 |
| 13. 直前再検証が承認対象と完全一致する場合だけmerge | C-11 | 11 |
| 14. merge完了とmerged commit SHAを確認して`MERGED` | C-11 | 11 |

Section 13のMVP inclusionsは、上記2表と各componentの受入条件で網羅する。Phase 15のrelease acceptanceで、inclusionsを1件ずつ確認する。

## 8. ユーザー判断待ちの提案

本節はdecision briefである。**いずれもユーザーの明示回答があるまで`Decided`としない。** target-experienceのdecision logへは`Proposed: ユーザー判断待ち`として記録済みである。

### D-028: merge承認の入力形式（Section 8のOpen）

| 項目 | 内容 |
| --- | --- |
| 判断が必要な内容 | merge承認を固定commandで補助するか、明示確認付き自然言語だけにするか |
| 制約 | 承認はrepository、PR番号、approved head SHA、merge methodへbindすることがSection 5.5で必須 |
| 候補1（推奨） | 自然言語＋明示確認を主経路とし、`cc-review approve-merge <pr> --head <sha> --method <m>`を補助経路として併用する |
| 候補1の利点 | bind対象を引数として検証できる。headless経路とSSH再接続後のresumeでも同一形式が使える |
| 候補1の欠点 | 覚えるsyntaxが増える。commandと自然言語で2経路のtestが必要 |
| 候補2 | 明示確認付き自然言語のみ |
| 候補2の利点 | 対話体験に統一できる |
| 候補2の欠点 | bind対象がClaudeの解釈を経由するため、Controllerは承認対象を検証ではなく信頼で扱う。headless経路の承認手段を別途設計する必要が残る |
| Codex評価 | 候補1を妥当と評価。ただしユーザー合意が必要 |
| 推奨 | 候補1（Recommended）。固定commandのみとしないのは、D-013が対話gateを正常終了の形と定めているため |

### D-029: PowerShellの検証範囲（Section 12のOpen）

| 項目 | 内容 |
| --- | --- |
| 判断が必要な内容 | Windows Store版とMSI版PowerShellの両方を正式検証するか |
| 候補1（推奨） | MSI installer配布のPowerShell 7を正式検証対象とし、Store版は未検証riskとして明記、Approved follow-up候補とする |
| 候補1の利点 | GitHub-hosted runnerで検証を自動化できる。MVP完成を遅らせない |
| 候補1の欠点 | Store版利用者は自己責任になる |
| 候補2 | 両方を正式検証対象とする |
| 候補2の欠点 | Store版はGitHub-hosted runnerで再現できず、手動検証手順と定期実施が必要 |
| Codex評価 | 候補1を妥当と評価。ただし`winget`は配布形式ではなく導入経路であるため、対象packageとinstallerを正確に定義すること |
| 推奨 | 候補1（Recommended）。対象を「MSI installerで配布されるPowerShell 7」と定義し、導入経路（`winget`を含む）と区別して記述する。Section 11のreport例が既にStore版検証をfollowup-001の例としており整合する |

### D-030: transportの再利用範囲（Section 5.4の修正提案）

| 項目 | 内容 |
| --- | --- |
| 判断が必要な内容 | Section 5.4の再利用表を事前承認として扱うか、component単位の再利用判定へ置き換えるか |
| 観測事実 | 参考実装のcore投稿関数は本文をfile経由で渡しており（dry-runのみ引数渡し）、長大bodyのsidecar分割と上限checkも実装済み。ただし戻り値がなく、comment ID / URL / 本文hashを返さない。Skill helper側は本文を引数で渡し、IDも返さない |
| 影響 | 公開interfaceの変更は必須。ただし「本文の渡し方」を理由に全面書き直しとする根拠はない |
| 候補1（推奨） | 公開interfaceを新設し、再利用判定はSection 10のcomponent単位表で行う。algorithmとtestの選択移植余地を残す |
| 候補2 | Section 5.4の再利用表を維持する |
| 候補2の欠点 | read-after-writeを満たさないAPIを再利用対象として事前承認したままになる |
| Codex評価 | 新interface設計は妥当。全面新規実装の根拠は不足 |
| 推奨 | 候補1（Recommended） |

### P-001: runtime依存の方針

| 項目 | 内容 |
| --- | --- |
| 判断が必要な内容 | runtime依存をゼロに保つか、schema検証に成熟したlibraryを導入するか |
| 制約 | target-experienceはCodex出力のJSON Schema検証を要求する。汎用validatorを自作すると検証漏れがsecurityとstate遷移へ直結する |
| 候補1 | 依存ゼロを維持し、対応するschema機能を明示的に限定した専用protocol validatorを実装する |
| 候補2 | 成熟したvalidator libraryを導入する |
| 評価軸 | supply chain risk、配布size、Windows / Linux互換性、validationの網羅性、保守cost |
| 推奨 | Phase 0で両案のprototypeを作り、上記評価軸と受入testで比較して決定する。**現時点では推奨案を確定しない**。`pyproject.toml`が現在`dependencies = []`であることは、stub状態の事実であって要件ではない |

## 9. `Proposed`の確定

target-experienceの`Proposed`項目について、採用可否を示す。**採用**はtarget-experienceの記述をそのまま実装方針とすることを意味し、記述の変更を伴わない。

| Section | Proposed内容 | 判定 | 対応component |
| --- | --- | --- | --- |
| 3 | 実装順序はPR mode先行 | 採用 | Section 11のPhase構成 |
| 3 | Skill mode主経路、headless CLIは補助 | 採用（条件付き） | C-06。Section 3の制御反転を前提とする |
| 3 | terminalへstate、次action、URLを簡潔表示 | 採用 | C-06、C-10 |
| 3 | Codex logを別paneで観測可能にする | 採用 | C-13 |
| 3 | review / fix最大3 round | 採用 | C-08 |
| 3 | review承認後のCI pendingは`WAITING_CI` | 採用 | C-10 |
| 3 | CI既定20分・30秒間隔 | 採用 | C-10 |
| 3 | ユーザー判断とmerge gateは対話sessionで受ける | 採用 | C-06、C-11 |
| 3 | comment回答は次の明示resume時に取得 | 採用 | C-05 |
| 3 | Codex reviewerはheadごとにfresh subprocess | 採用 | C-07 |
| 3 | `tmux`内SSH runは切断後も安全gateまで継続 | 採用 | C-13 |
| 3 | merge methodはrepository設定で選択 | 採用 | C-11 |
| 3 | artifact保持30日 / 90日 | 採用 | C-05 |
| 3 | Auto mode推奨とfallback階層 | 採用 | C-04 |
| 3 | Plugin配布とController CLI package | 採用 | C-13 |
| 3 | 既存transport再利用でControllerを最小化 | **判断待ち** | D-030（Section 8） |
| 5.1 | PR mode正常系の22 step | 採用 | C-08。各stepを受入条件へ展開する |
| 6.1〜6.4 | terminal表示 | 採用 | C-06、C-10、C-11。metadata markerは`CC_REVIEW_META`、logは`.cc-review-logs/` |
| 8 | intervention policyの表 | 採用 | C-01。各行をstate遷移testの受入条件へ落とす |
| 9 | safety behavior | 採用（具体化） | C-04でallowlist、credential隔離、OS別file権限を追加 |
| 10.1 | checkpointの保存項目 | 採用 | C-05。schema化しversion管理する |
| 10.2 | Ctrl+C 1回でgraceful、2回目で強制 | 採用 | C-02 |
| 10.3 | resume手順 | 採用 | C-05 |
| 10.5 | merge failureの実装詳細 | 採用 | C-11 |
| 10.6 | SSH切断時のwrapper実装 | 採用 | C-13 |
| 10.7 | artifact retentionの実装詳細 | 採用 | C-05 |
| 11 | final reportの出力4種とreport例 | 採用 | C-10 |
| 12 | platform差分の実装詳細 | 採用 | C-02、C-13 |
| 13 | later phases | 採用 | MVP対象外を維持 |

## 10. 参考実装の再利用判定

[ADR-0002](../decisions/0002-independent-reimplementation.md)のSelective porting policyに従い、領域ごとに判定する。**本表は移植の事前承認ではない。** 実際に移植する場合は、対象file、source commit、理由、適用license、移植後testを移植PRへ記録する。

| 領域 | 観測事実 | 判定 | 理由 |
| --- | --- | --- | --- |
| core comment投稿 | 本文をfile経由で渡す。長大bodyのsidecar分割と上限checkあり。戻り値なしでID / URL / hashを返さない | interfaceは新設、algorithmは選択移植候補 | read-after-writeにID返却が必須。分割と上限checkは要件に合致する |
| Skill helper層の投稿 | 本文を引数で渡す。IDを返さない | 再利用しない | P-004、P-005の両方に反する |
| round metadataとresume | GitHub commentからround stateを復元する処理が存在する | 選択移植候補 | 本projectのcanonical state再構築と目的が一致する。metadata形式は`CC_REVIEW_META`へ変更が必要 |
| 公開用comment render | 発言者とmodelを明示するrenderが存在する | 選択移植候補 | 要件に合致する。decision / clarificationのkind追加が必要 |
| Issue→PR handoff | 冪等なhandoff処理が存在する | 選択移植候補 | C-12の要件と一致する |
| 子process停止 | Windows / POSIX差分の扱いが存在する | 選択移植候補 | C-02の要件と一致する |
| orchestrator | 単一moduleが大規模化し、CLI / Skillで二重実装されている | 再利用しない | P-002に反する。Section 3の制御構造とも異なる |
| permission / sandbox構成 | bypass flagを無条件付与する経路がある | 再利用しない | P-006に反する |

## 11. 実装順序

dependency順に16 phaseへ分割する。各phaseを1つの子Issueとし、Issue #2から参照する。1 phaseは複数の小さなPRへ分けてよい。

| Phase | 内容 | Component | 完了条件 |
| --- | --- | --- | --- |
| 0 | 基盤: `errors`、`config`、品質ゲート | — | CIでlint、type、coverage、size ratchetが動作する。P-001の比較prototypeで依存方針を決定する |
| 1 | domain state machine | C-01 | 17 stateと全遷移が純粋関数でtestできる |
| 2 | process abstraction | C-02 | 両OSで孫processが残らない。Ctrl+Cの2段階が動作する |
| 3 | GitHub transport | C-03 | 投稿→再取得→hash一致。timeout後の重複投稿なし。thread reply動作 |
| 4 | trust / permission / credential境界 | C-04 | allowlist外の承認が受理されない。reviewerのmutation negative testが失敗する |
| 5 | 最小checkpointとresume | C-05 | 中断後に同じturn IDから再開できる |
| 6 | active host protocolとstep engine | C-06 | Section 3.5の受入条件を満たす |
| 7 | Codex fresh runtimeと隔離checkout | C-07 | write権限なしでtestを実行しcheckoutを破棄する |
| 8 | PR mode review loop | C-08 | 実PRに対しdry-runで1 round完走。**CLI経由のdogfooding開始** |
| 9 | decision / clarification / follow-up | C-09 | 5 turn上限、早期終了、許可gateが個別にtestできる |
| 10 | qualificationとfinal reporter | C-10 | CI timeoutで`WAITING_CI`、同一JSONから同一Markdown |
| 11 | human merge gate | C-11 | 曖昧入力が承認にならない。merge timeout後に二重mergeしない |
| 12 | checkpoint全項目とretention | C-05 | Section 10.1の全項目を保存。cleanupがactive runを除外する |
| 13 | Issue modeとhandoff | C-12 | 既存PR再利用と冪等handoffが動作する |
| 14 | Plugin配布と任意wrapper | C-13 | wrapperなしでcore loopが動作する |
| 15 | release acceptance | 全体 | 主経路Skill、headless fallback、Windows、Linux/SSHでPR modeとIssue modeをend-to-endで通す |

### 順序の根拠

- Phase 0を先行させるのは、lintとtype gateを後から入れると既存code全体の修正が必要になるためである（P-010）
- Phase 2のprocess abstractionをPhase 7のCodex runtimeより前に置くのは、runtimeがprocess抽象の利用者であり、逆順では抽象がruntime固有の形へ引きずられるためである
- Phase 4のsecurity境界をagent起動より前に置くのは、credential隔離を後から差し込むと隔離漏れの検出が難しくなるためである
- Phase 5のcheckpoint / resumeを早い段階に置くのは、後から載せるとstate管理が全workflowへ散らばるためである。Phase 5では最小限（run ID、state、head SHA、GitHub cursor）だけを扱い、Section 10.1の全項目はPhase 12で扱う
- Phase 6のactive host protocolをPR modeより前に置くのは、Section 3の制御構造が全workflowの前提であり、後から反転すると全workflowを書き直すことになるためである
- Phase 14のwrapperは、wrapperなしでcore loopが動作することが設計条件であるため後段に置く。ただし初回releaseには含める

### 初回release条件

D-016のとおり、PR modeとIssue modeの両方の受入条件を満たすまで初回releaseとしない。Phase 15のrelease acceptance完了をrelease条件とする。Phase 14までの完了は必要条件であり、十分条件ではない。

## 12. 品質ゲート

| ゲート | 内容 | 導入 |
| --- | --- | --- |
| test | `python -m pytest -q`。外部依存ゼロ。agent CLIと`gh`はprocess境界でfake（P-011） | Phase 0 |
| coverage | baselineを測定しratchetする。subprocess coverageを有効化する | Phase 0 |
| lint | ruff | Phase 0 |
| type | mypy。段階的にstrictへ寄せる | Phase 0 |
| module size | 固定行数の即時failではなく、baselineからのratchetとする。責務数、循環依存、複雑度、testabilityと併せてreviewし、例外はPRで根拠とともに認める | Phase 0 |
| CI matrix | Ubuntu / Windows × Python 3.11 | 導入済み |
| contract test | SPDX表示、repository参照、CLI名称、禁止flag（P-006）、baseline link | 一部導入済み |
| version | `pyproject.toml`のversionとgit tagを同期する（P-012） | Phase 0 |

CIは常に全testを実行し、test fileの追加漏れが構造的に起こらないようにする。regression testは対象のIssue番号を参照する。

CIとcontract testは文書の存在と形式を検証するが、本書の要件整合性そのものは検証しない。Section 7のtraceability matrixは、その不足をreviewで補うための対応表である。

## 13. Riskと未決事項

| ID | Risk | 影響 | 緩和 |
| --- | --- | --- | --- |
| R-01 | Codex / Claude CodeのCLI interfaceが変わる | agent起動が失敗する | runtime層へ隔離し、versionを起動時に確認する。CLI固有の分岐をworkflow層へ漏らさない |
| R-02 | GitHub APIのrate limitがreview loopの実用性を下げる | 長時間runが停止する | 取得を差分cursorへ限定し、read-after-write以外のpollingを行わない |
| R-03 | 対象repositoryのCI所要時間がbounded waitを超える | `WAITING_CI`が常態化する | 待機上限をrepository設定で調整可能にする。resumeを軽量に保つ |
| R-04 | active host protocolのHOST_ACTION粒度が粗すぎる / 細かすぎる | round数が増えるか、hostへ渡す責務が過大になる | Phase 6でPR modeの1 roundを通して粒度を検証してからPhase 8へ進む |
| R-05 | 単一core engineの制約がSkillとCLIの要求差で崩れる | P-002が形骸化する | entry pointに置いてよい責務を3つへ限定し、両経路の同一シナリオtestで検出する |
| R-06 | credential隔離がOSまたはCLI versionの差で破れる | reviewerがdurable read-onlyでなくなる | negative testを両OSのCIで常時実行する。隔離手段を1箇所へ集約する |

未決事項:

- Section 8のD-028、D-029、D-030、P-001（ユーザー判断待ち）
- checkpoint schemaのversioning方式（Phase 5で決定）
- coverage floorとmodule size baselineの初期値（Phase 0で測定して決定）
- HOST_ACTIONの最終的な種類と粒度（Phase 6で決定）
- protocol versionの表現形式とPlugin / CLIの互換range（Phase 14で決定）
