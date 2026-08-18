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
2. `Open`（ユーザー判断または技術検証が必要）を解消する
3. componentと依存関係を定義し、実装子Issueの発行根拠にする

### 本書で決めないこと

- **target behaviorの変更**は行わない。変更が必要な場合はtarget-experienceのSection 15 decision logへ`D-NNN`として記録し、本書はそれに従う
- **各componentのAPI詳細設計**は行わない。本書はcomponentの責務境界（seam）の位置までを固定し、signature levelの設計は各子IssueのPRで行う

### 用語

target-experienceのSection 2と同じ`Decided` / `Proposed` / `Open` / `Superseded`を使用する。本書が新たに導入する実装原則は`P-NNN`、実装phaseは`Phase N`で参照する。

## 2. 設計原則

本プロジェクトはClaude Codeとreviewer agentを組み合わせる参考実装（[ADR-0002](../decisions/0002-independent-reimplementation.md)に出典を記録）を調査したうえで、独立再実装として開始した。参考実装には第三者によるcode auditが存在し、overall B+と評価されつつ具体的な失敗パターンが特定されている。

以下の原則は、その監査で実際に問題として観測された事象を、本プロジェクトで**構造的に発生させない**ための設計制約である。各原則は根拠を伴う。根拠のない原則は置かない。

| ID | 原則 | 根拠 |
| --- | --- | --- |
| P-001 | runtime依存をゼロに保つ。schema検証を含め標準library だけで実装する | 現行`pyproject.toml`が`dependencies = []`。参考実装も同方針で、agent CLIと`gh`をprocess境界でfakeすることにより全testが外部依存なしで動作していた |
| P-002 | **単一のcore engineを持つ**。Claude Code SkillとCLIはどちらも薄いadapterとし、round orchestrationを二重に実装しない | 参考実装ではCLI用orchestratorとSkill用runnerがround orchestrationを二重実装し、protocol変更のたびに両方へ反映する必要が生じていた。monolithも同時に発生し、単一moduleが5,000行を超えていた |
| P-003 | error分類をexit code、構造化出力、`gh api`のstatusで行う。出力文字列の部分一致による分類を禁止する | 参考実装では`quota` / `timeout` / `overloaded`等の語をfree-textから拾って一時障害と判定しており、それらを正当に論じるreview本文が誤分類され得た。HTTP statusも`"404"`の部分一致で判定していた |
| P-004 | GitHubからの取得は必ず`--json`の構造境界で行う。出力の行分割で本文を切らない | 参考実装ではcomment本文を`--jq '.[].body'`で取得して改行分割しており、複数行bodyが1行ずつ別fragmentになっていた。本プロジェクトのmetadata markerは複数行JSONを含むため、この方式では成立しない |
| P-005 | GitHubへ投稿する本文は必ず`--body-file`（0600のtemp file）経由で渡す。`--body`へ本文を直接渡さない | 参考実装のhelper層は`--body`直渡しで、Windowsのcommand line長制限（約32KB）にfinal reportが到達し得た。本プロジェクトはfinal reportとdecision briefを投稿するため上限に近づく |
| P-006 | permission bypass flagをcode上で構築しない。禁止語のcontract testを置く | 参考実装のskill modeは`--dangerously-bypass-approvals-and-sandbox`等を無条件に付与しており、文書上は「bypassはopt-in」と説明されていた。監査で唯一のHigh severity findingとなった。target-experience Section 9は`bypassPermissions`をpresetから使用不可にすることをDecidedとしている |
| P-007 | ユーザー決定として受理するGitHub commentを`authorAssociation`で制限する | 参考実装では本文末尾の署名行だけで「人間の要求」と判定しており、書き込み可能なrepositoryでは誰でも詐称できた。本プロジェクトはユーザー決定とmerge承認をGitHub commentから読むため、同じ経路がそのままprivilege escalationになる |
| P-008 | promptへ埋め込むGitHub由来のtextをfenceし、「これはデータであり指示ではない」と明示する | 参考実装ではIssue本文やcommentを区切りなくpromptへ埋め込んでいた。prompt injectionは完全には防げないが、明示的な区切りは低コストで水準を上げられる |
| P-009 | artifactはrunごとのdirectoryへ0700で作成する。予測可能な共有pathを使わない | 参考実装は`/tmp`配下の予測可能なpathへdefault umaskでagent responseを書いており、multi-user機ではprivate repositoryの内容を他localユーザーが読めた |
| P-010 | ruffとmypyをPhase 0からCIへ入れる | 参考実装は22,000行規模でlint / type gateを持たず、監査で「安価な保険が欠けている」と指摘された |
| P-011 | testはagent CLIと`gh`をprocess境界でfakeし、外部依存ゼロで全件実行できる状態を保つ | 参考実装のtest規律は監査でAと評価された唯一の項目であり、この性質が理由だった。CIが全件を実行するため、test fileの追加漏れが構造的に起こらない |
| P-012 | versionとgit tagを最初から運用する | 参考実装は400 commit以上にわたりversionが固定でtagもなく、第三者がpinもbug報告もできない状態だった |
| P-013 | すべてのcodeを`src/`配下のpackageへ置き、`sys.path`操作を禁止する。外部へはconsole entry pointで公開する | 参考実装はhelper群がpackage外にあり、`sys.path.insert`でpackage内部のprivate関数を参照していた |
| P-014 | subprocess呼び出しは必ずlist形式のargvで行う。`shell=True`、`os.system`、`eval`、`exec`を使用しない | 参考実装で徹底されており、監査で「shell injection surfaceが存在しない」と評価された。本プロジェクトも同水準を維持する |
| P-015 | credentialを本プロジェクトのcodeで扱わない。認証は認証済みCLI（`gh`、`claude`、`codex`）へ委譲する | 参考実装と同方針。state fileやlogへ秘密情報が入らないことをdesignで保証できる。target-experience Section 9のcredential分離要件とも一致する |

### P-002の補足: 単一core engineの意味

target-experienceは、対話型Claude Code Skillを主経路、`cc-review` headless CLIを補助・復旧経路と定めている（D-014）。2つのentry pointが存在すること自体は要件である。

本書が禁止するのは、**2つのentry pointがそれぞれround orchestrationを実装すること**である。

```text
許可される構成                         禁止される構成

  Skill      CLI                        Skill          CLI
    |         |                           |             |
    +----+----+                      orchestration  orchestration
         |                             (二重実装)     (二重実装)
   core engine                             |             |
         |                                 +------+------+
   domain / transport                             |
                                            domain / transport
```

entry pointが持ってよいのは、引数解析、session boundaryの受け渡し、表示の3つに限る。state遷移、round管理、agent起動、GitHub投稿の判断はすべてcore engineに置く。

## 3. 層構造とpackage layout

### 3.1 層

| 層 | 責務 | 副作用 | 依存 |
| --- | --- | --- | --- |
| domain | state machine、event、command、ledger | なし（純粋関数） | なし |
| schema | agent入出力の定義と検証 | なし | なし |
| transport | GitHub canonical conversationの投稿・取得・検証 | GitHub | domain、schema |
| runtime | process起動・停止、隔離checkout、permission profile | process、filesystem | domain |
| workflow | 単一core engineと各protocol | transport、runtime経由のみ | 全層 |
| state | checkpoint、resume、artifact retention | filesystem | domain、transport |
| entrypoint | 引数解析、session boundary、表示 | 標準入出力 | workflow |

domain層は副作用を持たず、`transition(state, event) -> (state, [command])`の形で「次に何をすべきか」をcommandとして**記述する**。commandの**実行**はworkflow層が行う。これによりstate遷移の全経路を純粋関数のtestで網羅できる。

### 3.2 package layout

```text
src/claude_code_codex_review_loop/
  __init__.py
  errors.py                 構造化error分類（P-003）
  config.py                 repository設定 -> user設定 -> 組込み既定値
  domain/
    states.py               State定義と遷移表
    events.py               入力event
    commands.py             副作用command（記述のみ、実行しない）
    machine.py              transition(state, event) -> (state, [command])
    ledger.py               finding / decision / clarificationのledger
    ids.py                  run ID、decision ID、candidate fingerprint
  schema/
    validate.py             標準libraryのみのvalidator（P-001）
    review.py               Codex reviewの入出力
    decision.py             decision request / verdict / brief
    followup.py             Approved follow-up候補
    report.py               final report
    merge.py                merge intentと承認record
  transport/
    gh.py                   gh CLI adapter（P-004、P-005、P-014）
    conversation.py         投稿 -> read-after-write -> 検証
    render.py               公開用render（発言者とmodelを明示）
    metadata.py             CC_REVIEW_META marker
    trust.py                authorAssociationによる信頼判定（P-007）
  runtime/
    process.py              process起動・停止のOS抽象（P-014）
    checkout.py             隔離checkoutの作成と破棄
    codex.py                fresh read-only reviewer runtime
    claude.py               active host adapter
    permission.py           permission profile（P-006）
    prompt.py               data fencing付きprompt構築（P-008）
  workflow/
    engine.py               単一core engine（P-002）
    pr_mode.py              PR mode
    issue_mode.py           Issue modeとIssue->PR handoff
    clarification.py        clarification protocol（D-011）
    decision.py             ユーザー判断フロー（D-010）
    followup.py             Approved follow-up（D-024）
    qualification.py        local test gateとCI確認
    reporter.py             final reporter
    merge_gate.py           human merge gate（D-013）
  state/
    checkpoint.py           checkpointの保存と読み出し
    resume.py               GitHubからのcanonical state再構築
    retention.py            保持期間とbounded cleanup（P-009）
  cli.py                    console entry point `cc-review`
```

`plugin/`にはSKILL.mdと薄いlauncherだけを置く。workflow判断を持たず、install済みCLIをprotocol version交換のうえ呼び出す（D-026）。

`wrappers/`にはWindows Terminalと`tmux`の任意wrapperを置く。wrapperなしでcore loopが動作することを設計条件とする（D-017）。

### 3.3 module size budget

P-002の再発防止として、CIで機械的に監視する。

| 対象 | 上限 | 超過時 |
| --- | --- | --- |
| `src/`配下の1 module | 600行 | CI fail。seamを見つけて分割する |
| `tests/`配下の1 file | 800行 | CI fail。対象moduleごとに分割する |

上限は絶対的な正しさではなく、分割の判断を後回しにしないための強制力として置く。上限の変更はPRで根拠とともに行う。

## 4. Component定義

[architecture/README.md](../architecture/README.md)が挙げる10項目に対応する。各componentについて責務、依存、主要な決定、受入条件を示す。API詳細は各子IssueのPRで設計する。

### C1. domain state machine、event、command

| 項目 | 内容 |
| --- | --- |
| 責務 | target-experience Section 7の17 stateと全遷移を、副作用のない関数として表現する |
| 主module | `domain/` |
| 依存 | なし |
| 主要な決定 | 遷移関数は`(state, event) -> (state, [command])`。commandは実行せず記述するだけとする。GitHub永続化gate（Section 5.3）を通過しない遷移は表現できないよう、投稿と検証の完了をeventとして要求する |
| 受入条件 | 全stateと全遷移が純粋関数のtestで到達可能。Section 7のmermaid図と遷移表が機械的に照合できる |
| test戦略 | 遷移表をdata drivenでtestする。到達不能stateと未定義遷移をtestで検出する |

### C2. GitHub canonical conversation transportとread-after-write

| 項目 | 内容 |
| --- | --- |
| 責務 | agent発言とユーザー決定をGitHubへ代理投稿し、再取得して一致を確認する（Section 5.3） |
| 主module | `transport/` |
| 依存 | C1 |
| 主要な決定 | 投稿後にcomment / review ID、URL、本文hash、対象head SHAを取得できるまでturnをcompletedにしない。timeout時は成否を推測せずidempotency markerでGitHubを検索してから再投稿する。取得は`--json`境界（P-004）、投稿は`--body-file`（P-005） |
| 主要な決定 | metadata markerは`CC_REVIEW_META`とし、HTML comment内のJSONとして格納する。単一行前提を置かない |
| 主要な決定 | ユーザー決定として受理するcommentを`authorAssociation`で制限する（P-007） |
| 受入条件 | fake `gh`に対し、投稿→再取得→hash一致→ID記録が検証できる。timeout後の再投稿で重複commentが発生しない |
| test戦略 | `gh`をprocess境界でfakeし、成功・timeout・rate limit・comment改変の各経路をtestする |

**Section 5.4の再利用方針について**: target-experience Section 5.4は既存実装のGitHub comment transportを再利用し、comment ID・URL・本文hashの返却をextensionとして追加する方向を`Proposed implementation direction`として示している。調査の結果、参考実装のhelper層は投稿結果のIDを一切返さず、本文を`--body`で直渡ししており、read-after-writeとWindows対応の両方について**extensionではなく作り直しが必要**であることが判明した。本書ではtransportを新規実装とし、Section 5.4の再利用表は事前承認ではなく調査対象として扱う（後述のD-030提案）。

### C3. Claude Code Plugin / active host adapter

| 項目 | 内容 |
| --- | --- |
| 責務 | 既存の対話型Claude Code sessionをhost / coderとして使用し、Skillからinstall済みController CLIを呼ぶ（D-014、D-026） |
| 主module | `plugin/`、`cli.py` |
| 依存 | C2 |
| 主要な決定 | PluginとCLIはprotocol versionを交換し、非互換時は処理を開始せず更新方法を表示する。対象repositoryはClaude Codeの現在directoryまたは明示`--repo`から解決し、Pluginのinstall directoryと分離する |
| 主要な決定 | Pluginはworkflow判断を持たない。SkillからCLIへ渡すのは対象、intent、session boundaryだけとする（P-002） |
| 受入条件 | 任意のrepositoryからSkillを起動でき、protocol version不一致を検出できる。repo-local Skillは開発・test専用であることが文書と構成の両方で明確 |

### C4. Codex fresh reviewer runtimeと隔離checkout

| 項目 | 内容 |
| --- | --- |
| 責務 | review turnごとにfreshなdurable read-only subprocessを起動し、exact headの隔離checkout内でのみ検証させる（D-015、D-025） |
| 主module | `runtime/` |
| 依存 | C1 |
| 主要な決定 | reviewerへwrite credentialを渡さない。隔離checkoutは対象headから新規作成し、review後にcheckoutごと破棄する。破棄前のdirty stateをevidenceとして記録する |
| 主要な決定 | permission bypass flagをcode上で構築しない（P-006）。promptへ埋め込むGitHub由来textはfenceする（P-008） |
| 主要な決定 | session memoryを引き継がず、GitHub canonical conversationとfinding ledgerからcontextを毎回再構築する |
| 受入条件 | reviewerがrepositoryとGitHubへ永続変更できないことを、権限構成のtestで確認できる。禁止flagがargvへ現れないことをcontract testで確認できる |

### C5. PR modeとIssue-to-PR handoff

| 項目 | 内容 |
| --- | --- |
| 責務 | Section 5.1の22 stepとSection 5.2のhandoffを、core engine上の流れとして実装する |
| 主module | `workflow/pr_mode.py`、`workflow/issue_mode.py`、`workflow/engine.py` |
| 依存 | C1、C2、C3、C4 |
| 主要な決定 | PR modeを先に構築し、その上にIssue取得・実装・handoffを載せる。両modeは同一のcore engineを共有し、差分はconversation sourceの解決とPR作成の有無に限る |
| 主要な決定 | 既存PRがある場合は新規作成せず、handoffを両側へ冪等に記録してからPR modeへ合流する。PR作成後の失敗でIssue実装をやり直さない |
| 受入条件 | 実PRに対しdry-runでreview→fix→re-reviewが1 round完走する。handoffの二重投稿が発生しない |

### C6. decision / clarification protocol

| 項目 | 内容 |
| --- | --- |
| 責務 | D-010のユーザー判断フローとD-011のclarification protocolを実装する |
| 主module | `workflow/decision.py`、`workflow/clarification.py` |
| 依存 | C2、C5 |
| 主要な決定 | clarification counterはGitHub上の`run ID + finding / decision fingerprint + turn` metadataから再構築し、head SHAだけが変わってもresetしない |
| 主要な決定 | clarification中は対象headを固定し、source変更・commit・pushを行わない。codeを変更した場合はclarificationを終了して新roundとする |
| 主要な決定 | review / fixの最大roundとclarification turnを別のcounterで管理する |
| 受入条件 | 5 turn上限と5つの早期終了条件が個別にtestできる。同一topicの判定がhead変更をまたいで維持される |

### C7. test・CI qualificationとfinal reporter

| 項目 | 内容 |
| --- | --- |
| 責務 | 承認headに対するlocal testとGitHub CIの確認、final reportの生成と投稿 |
| 主module | `workflow/qualification.py`、`workflow/reporter.py` |
| 依存 | C2、C5 |
| 主要な決定 | CI pendingは設定可能なbounded foreground wait（既定20分・30秒間隔）とし、上限後は`WAITING_CI`で終了する（D-020）。sleepは標準libraryで行い、外部binaryへ依存しない |
| 主要な決定 | final reportはschema検証済みのJSONを正とし、Markdownとterminal summaryをそこから決定論的にrenderする。言語はrepository設定→user設定→組込み既定（日本語）の順に解決し、未対応値はvalidation errorとする（D-019） |
| 受入条件 | CI timeout時に`WAITING_CI`のcheckpointが残り、明示resumeで再開できる。同一JSONから同一Markdownが再現される |

### C8. human merge gate

| 項目 | 内容 |
| --- | --- |
| 責務 | `READY_FOR_HUMAN_MERGE`の対話gateと、明示承認後のgated merge（D-013） |
| 主module | `workflow/merge_gate.py` |
| 依存 | C2、C7 |
| 主要な決定 | intentは`QUESTION` / `REQUEST_CHANGES` / `APPROVE_MERGE` / `CANCEL`。曖昧な肯定、過去の承認、別PRへの承認から`APPROVE_MERGE`を推論しない |
| 主要な決定 | 承認はrepository、PR番号、approved head SHA、merge methodへbindし、いずれかが変われば失効する |
| 主要な決定 | 自然言語を主経路とし、head SHAとmethodを引数で渡す固定commandを併用する（後述のD-028） |
| 受入条件 | 「問題なさそう」「OKです」等の入力が`APPROVE_MERGE`にならないことをtestで確認できる。head変更後に旧承認でmergeできないことを確認できる |

### C9. checkpoint、resume、artifact retention

| 項目 | 内容 |
| --- | --- |
| 責務 | Section 10.1のcheckpoint保存、GitHubからのstate再構築、artifactの保持と削除 |
| 主module | `state/` |
| 依存 | C1、C2 |
| 主要な決定 | resumeはGitHub canonical conversationからstateを再構築し、local checkpointはcacheとして照合する。GitHubで確認できないlocal出力を判断根拠にしない |
| 主要な決定 | artifactは正常run 30日、`FAILED` / `BLOCKED` / salvage 90日。active / lock保持中のrunを除外したbounded cleanupを起動時と明示commandで行う（D-023）。保存先はrunごとのdirectoryへ0700で作成する（P-009） |
| 受入条件 | 中断後のresumeが同じturn IDから再開する。partial turn（質問のみ投稿済み）で質問が重複投稿されない |

### C10. Windows PowerShell、Linux/SSH、`tmux` wrapper

| 項目 | 内容 |
| --- | --- |
| 責務 | OS差分を吸収したprocess抽象と、任意の監視 / 継続wrapper |
| 主module | `runtime/process.py`、`wrappers/` |
| 依存 | C4 |
| 主要な決定 | 子process treeの停止はWindowsとPOSIXで別実装とし、interfaceを共通化する。Ctrl+C 1回でgraceful cancellationを開始し、2回目を緊急強制停止とする |
| 主要な決定 | wrapperなしでcore loopが動作する。wrapperの起動失敗をrunの失敗にしない。同一run IDの監視paneを重複作成しない |
| 受入条件 | 両OSのCIでprocess停止testが通る。wrapper未導入環境でPhase 5のloopが完走する |

### 4.1 依存グラフ

```text
        C1 domain
       /    |     \
      /     |      \
  C2 transport   C4 runtime
    |    \        /    |
    |     \      /     |
  C9 state  C5 workflow(PR/Issue)  C10 platform
              /    |    \
             /     |     \
          C6     C7      C8
                  |
                 C3 plugin / entrypoint
```

## 5. `Proposed`の確定

target-experienceの`Proposed`項目について、採用可否を確定する。**採用**はtarget-experienceの記述をそのまま実装方針とすることを意味し、記述の変更を伴わない。

| Section | Proposed内容 | 判定 | 補足 |
| --- | --- | --- | --- |
| 3 | 実装順序はPR mode先行 | 採用 | Section 7のPhase構成へ反映 |
| 3 | Skill mode主経路、headless CLIは補助 | 採用（条件付き） | P-002により、両者が共有するcore engineを必須とする |
| 3 | terminalへstate、次action、URLを簡潔表示 | 採用 | render層で決定論的に生成 |
| 3 | Codex logを別paneで観測可能にする | 採用 | 任意wrapper。既定では起動しない |
| 3 | review / fix最大3 round | 採用 | 設定可能、既定3 |
| 3 | review承認後のCI pendingは`WAITING_CI` | 採用 | — |
| 3 | CI既定20分・30秒間隔のbounded wait | 採用 | D-020 |
| 3 | ユーザー判断とmerge gateは対話sessionで受け、Controllerが転記 | 採用 | — |
| 3 | GitHub comment回答は次の明示resume時に取得 | 採用 | D-021 |
| 3 | Codex reviewerはheadごとにfresh subprocess | 採用 | D-015 |
| 3 | `tmux`内SSH runは切断後も安全gateまで継続 | 採用 | D-018 |
| 3 | merge methodはrepository設定で選択 | 採用 | D-022 |
| 3 | artifact保持30日 / 90日 | 採用 | D-023 |
| 3 | Auto mode推奨とfallback階層 | 採用 | P-006のcontract testを追加 |
| 3 | Plugin配布とController CLI package | 採用 | D-026 |
| 3 | 既存GitHub transport等の再利用でControllerを最小化 | **修正** | C2に記載。transportは新規実装とし、再利用表は調査対象へ格下げ（D-030提案） |
| 5.1 | PR mode正常系の22 step | 採用 | core engineのstep定義として使用 |
| 6.1 | 通常表示 | 採用 | metadata markerは`CC_REVIEW_META`、logは`.cc-review-logs/` |
| 6.2 | 監視paneのwrapper実装 | 採用 | Phase 10 |
| 6.3 | merge判断gateの表示 | 採用 | 選択肢3の文言はD-028の承認形式へ合わせる |
| 6.4 | merge完了表示 | 採用 | — |
| 8 | intervention policyの表 | 採用 | 各行をstate遷移testの受入条件へ落とす |
| 9 | safety behavior | 採用（追加） | P-007、P-008、P-009を追加制約として加える |
| 10.1 | checkpointの保存項目 | 採用 | schema化してversion管理する |
| 10.2 | Ctrl+C 1回でgraceful、2回目で強制 | 採用 | C10で両OS実装 |
| 10.3 | resume手順 | 採用 | GitHubを正、localをcacheとして照合 |
| 10.5 | merge failureの実装詳細 | 採用 | — |
| 10.6 | SSH切断時のwrapper実装 | 採用 | Phase 10 |
| 10.7 | artifact retentionの実装詳細 | 採用 | P-009を追加 |
| 11 | final reportの出力4種 | 採用 | JSONを正、他をrender |
| 11 | report例 | 採用 | 例中のWindows Store版の記述はD-029と整合 |
| 12 | platform差分の実装詳細 | 採用 | — |
| 13 | later phases | 採用 | MVP対象外を維持 |

## 6. `Open`の決定

target-experienceに残っていた`Open` 2件を決定する。decision logへD-028、D-029として記録する。あわせて、Section 5.4の再利用方針の変更をD-030として記録する。

### D-028: merge承認の入力形式

**決定**: 自然言語入力を主経路とし、Claudeが必ず明示確認を行ったうえで`APPROVE_MERGE`へ構造化する。加えて、head SHAとmerge methodを引数で渡す固定commandを併用する。

```text
cc-review approve-merge <pr> --repo OWNER/REPO --head <sha> --method <merge|squash|rebase>
```

**根拠**: 承認はrepository、PR番号、approved head SHA、merge methodへbindすることがSection 5.5で必須とされている。自然言語のみの場合、bind対象はClaudeの解釈を経由するため、Controllerは「ユーザーがどのheadを承認したか」を検証ではなく信頼で扱うことになる。固定commandは引数として渡された値をそのまま検証できる。またheadless CLIとSSH再接続後のresumeでも同一形式が使えるため、経路ごとに承認手段を設計し直す必要がなくなる。

固定commandのみとしないのは、D-013が「既存の対話型Claude Code PowerShell sessionで対話するgate」を正常終了の形と定めているためである。commandは代替経路であり、必須経路ではない。

### D-029: Windows PowerShellの検証範囲

**決定**: MVPの正式検証対象をMSI / winget版PowerShell 7とする。Windows Store版は未検証と明記し、Approved follow-up候補として扱う。

**根拠**: Store版はapp execution aliasとpackage sandboxの扱いが異なり、GitHub-hosted runnerで再現できない。正式検証に含めると手動検証手順と定期実施が必要になり、MVPの完成が遅れる。target-experience Section 11のreport例は、まさに「Windows Store版PowerShellを検証する」をApproved follow-up候補`followup-001`の例として使用しており、この扱いと整合する。

未検証であることをREADMEとfinal reportのremaining risksへ明示し、利用者が判断できる状態にする。

### D-030: transportの再利用方針

**決定**: GitHub canonical conversation transportを新規実装とする。Section 5.4の再利用表は事前承認ではなく調査対象として扱う。

**根拠**: 再利用対象として挙げられていた既存実装のcomment投稿は、投稿結果のcomment ID、URL、本文hashを返さないため、Section 5.3が必須とするread-after-write確認を満たせない。また本文をcommand line引数で渡すため、Windowsのcommand line長制限にfinal reportが到達し得る。両方がtransportの中核要件であり、extensionではなく作り直しになる。

Controllerを最小化するという方針自体は維持する。最小化はcodeの再利用ではなく、責務の限定（Section 5.4末尾の列挙）によって達成する。

## 7. 実装順序と子Issue

dependency順に11 phaseへ分割する。各phaseを1つの子Issueとし、Issue #2から参照する。1 phaseは複数の小さなPRへ分けてよい。

| Phase | 内容 | 主component | 完了条件 |
| --- | --- | --- | --- |
| 0 | 基盤整備: `errors`、`config`、schema validator、ruff / mypy / coverage gate、module size check | — | CIでlint、type、coverage floor、size checkが動作する |
| 1 | domain state machine | C1 | 17 stateと全遷移が純粋関数でtestできる |
| 2 | GitHub transport、冪等性、read-after-write | C2 | fake `gh`で投稿→再取得→hash一致が検証できる |
| 3 | 最小checkpointとresume | C9 | 中断後に同じturn IDから再開できる |
| 4 | Codex fresh runtimeと隔離checkout | C4 | write credentialなしでtestを実行し、checkoutを破棄できる |
| 5 | PR modeのreview→fix→re-review loop | C5 | 実PRに対しdry-runで1 round完走する |
| 6 | decision / clarification protocol | C6 | 5 turn上限と早期終了条件が個別にtestできる |
| 7 | qualificationとfinal reporter | C7 | CI timeoutで`WAITING_CI`、同一JSONから同一Markdown |
| 8 | human merge gateとD-028の承認形式 | C8 | 曖昧入力が`APPROVE_MERGE`にならない |
| 9 | Issue modeとIssue→PR handoff | C5 | 既存PR再利用と冪等handoffが動作する |
| 10 | Plugin配布とplatform wrapper | C3、C10 | wrapperなしでcore loopが動作する |

### 順序の根拠

- Phase 0を先行させるのは、lintとtype gateを後から入れると既存code全体の修正が必要になるためである（P-010）
- Phase 3のcheckpoint / resumeを早い段階に置くのは、resumeを後から載せるとstate管理が全workflowへ散らばるためである。Phase 3では最小限（run ID、state、head SHA、GitHub cursor）だけを扱い、Section 10.1の全項目はPhase 7までに拡張する
- Phase 5でCLI経由のdogfoodingを開始する。この時点でPlugin配布は未完成でよい
- Phase 10を最後に置くのは、wrapperなしでcore loopが動作することが設計条件であり、wrapperの完成がMVPのcritical pathではないためである

### 初回release条件

D-016のとおり、PR modeとIssue modeの両方の受入条件を満たすまで初回releaseとしない。Phase 9完了が最小条件であり、Phase 10はrelease前に完了させる。

## 8. 品質ゲート

| ゲート | 内容 | 導入 |
| --- | --- | --- |
| test | `python -m pytest -q`。外部依存ゼロ。agent CLIと`gh`はprocess境界でfake（P-011） | Phase 0 |
| coverage | ratcheting floor。subprocess coverageを有効化し、helper相当のcodeも可視化する | Phase 0 |
| lint | ruff | Phase 0 |
| type | mypy。段階的にstrictへ寄せる | Phase 0 |
| CI matrix | Ubuntu / Windows × Python 3.11 | 導入済み |
| contract test | SPDX表示、repository参照、CLI名称、禁止flag（P-006）、module size（3.3節） | 一部導入済み |
| version | `pyproject.toml`のversionとgit tagを同期する（P-012） | Phase 0 |

参考実装のtest規律で有効だった点を採用する。CIは常に全testを実行し、test fileの追加漏れが構造的に起こらないようにする。regression testは対象のIssue番号を参照する。

## 9. 選択移植ポリシーの運用

[ADR-0002](../decisions/0002-independent-reimplementation.md)のSelective porting policyに従う。本書は移植候補を**調査対象として列挙するのみ**で、事前承認しない。

調査対象:

- 公開用comment renderの整形方針
- round metadataからのresume再構築の考え方
- Issue→PR handoffの冪等性の扱い
- 子process treeの停止におけるWindows / POSIX差分

実際に移植する場合は、対象file、source commit、理由、適用license、移植後testを、その移植PRへ記録する。参考実装のrepositoryが利用できなくなった場合も、本書とADR-0002だけで設計判断を再構築できる状態を維持する。

## 10. Riskと未決事項

| ID | Risk | 影響 | 緩和 |
| --- | --- | --- | --- |
| R-01 | Codex / Claude CodeのCLI interfaceが変わる | agent起動が失敗する | runtime層へ隔離し、versionを起動時に確認する。CLI固有の分岐をworkflow層へ漏らさない |
| R-02 | GitHub APIのrate limitがreview loopの実用性を下げる | 長時間runが停止する | 取得を差分cursorへ限定し、read-after-write以外のpollingを行わない |
| R-03 | 対象repositoryのCI所要時間がbounded waitを超える | `WAITING_CI`が常態化する | 待機上限をrepository設定で調整可能にする。resumeを軽量に保つ |
| R-04 | module size budgetが設計を歪める | 不自然な分割が起きる | 上限はPRで根拠とともに変更できる。分割は既存のseamに沿って行う |
| R-05 | 単一core engineの制約がSkillとCLIの要求差で崩れる | P-002が形骸化する | entry pointに置いてよい責務を3つへ限定し、逸脱をreviewで検出する |

未決事項:

- checkpoint schemaのversioning方式（Phase 3で決定）
- coverage floorの初期値（Phase 0で測定して決定）
- protocol versionの表現形式とPlugin / CLIの互換range（Phase 10で決定）
