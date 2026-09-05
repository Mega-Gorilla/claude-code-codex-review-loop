<!-- SPDX-License-Identifier: Apache-2.0 -->

# Architecture overview

| Field | Value |
| --- | --- |
| Authority | **Accepted**（PR #4のユーザー承認とmergeにより確定）。詳細の正本は[implementation plan](../plans/implementation-plan.md) |
| 対象読者 | 本projectへ初めて参加する開発者 |

最初に読む1ページです。用語は[glossary](../glossary.md)を参照してください。

## 何をするものか

GitHubのIssueまたはPRを指定して起動すると、選択したcoderが実装し、reviewerがread-onlyでreviewし、両者の発言をGitHubへ記録しながら、人間が明示的にmergeを承認するまで進む設計です。

coder / reviewerにはClaude Code / Codexを独立設定し、同一provider同士を含む4組み合わせへ拡張します（D-032、[implementation plan](../plans/implementation-plan.md) Section 2.5）。**この拡張は未実装でIssue #52が追跡します**。Phase 8までの実行基盤の完成と、両providerの実接続完了は区別します。

## 役割

| 役割 | 担当 | できないこと |
| --- | --- | --- |
| ユーザー | 実行開始、判断、mergeの明示承認 | — |
| Controller | 決定論的なstate machine。GitHub投稿、process起動、schema検証、merge実行 | LLMを内包しない。意味的な要約や推奨を作らない |
| coder（Claude Code / Codex、active host） | 実装、test、commit、push、説明、ユーザー入力の構造化 | GitHubへのworkflow記録を直接投稿しない。mergeしない |
| reviewer / final reporter（read-only役割） | 隔離checkout内でのreview、test、再現、read-only Web調査、承認済みheadのfinal report | 実repositoryとGitHubを変更しない。coderと同一providerでもsession・権限を共有しない |

## 主経路: advance / submit

Controller CLIはactive coder sessionの外部toolとして呼ばれ、親のLLM turnを呼び戻せません。そのため主経路のcore engineはcoderを起動せず、**次に何をすべきかを返す**step engineとして動きます。

```mermaid
sequenceDiagram
    actor User as ユーザー
    participant Host as active coder session
    participant Ctrl as Controller CLI
    participant GH as GitHub
    participant Reviewer as fresh reviewer

    User->>Host: /cc-review pr 512 --repo OWNER/REPO
    loop 1回のadvanceで1 action
        Host->>Ctrl: cc-review advance
        Ctrl->>GH: canonical conversationからstate再構築
        opt reviewが必要なstate
            Ctrl->>Reviewer: 選択providerのfresh read-only subprocessを起動
            Reviewer-->>Ctrl: findings
            Ctrl->>GH: 投稿してread-after-write確認
        end
        Ctrl-->>Host: HOST_ACTION / AWAIT_USER / TERMINAL
        Host->>Host: actionを自身のcontextで実行
        Host->>Ctrl: cc-review submit
        Ctrl->>GH: 投稿してread-after-write確認
    end
    Ctrl-->>Host: READY_FOR_HUMAN_MERGE
    Host-->>User: state、次action、GitHub URLを表示
```

主経路でControllerが直接起動するagentはreviewerとfinal reporterだけです。coderは主経路ではactive host側で動作し、会話contextを保ちます（headless復旧経路ではControllerが選択providerのcoder adapterをsubprocessとして起動します）。provider別CLIの入出力変換と権限制御はadapter側へ置き、round loopを複製しません。

## Component map

15 componentの一覧と依存とPhaseは[implementation plan](../plans/implementation-plan.md) Section 4、主要な決定と受入条件はSection 5にあります。依存の方向は次のとおりです。

```text
基盤        domain -> protocol schema -> security policy
            process abstraction
永続化      GitHub transport -> canonical record検証 / credential隔離 -> resume / retention
実行基盤    active host protocol -> fresh reviewer runtime（provider別adapter）
workflow    PR mode -> decision / clarification / follow-up
                    -> qualification / final reporter -> human merge gate
                    -> Issue mode
配布        Plugin / 任意wrapper
```

## 変えてはならない不変条件

| 不変条件 | 意味 |
| --- | --- |
| GitHub canonical | workflowへ影響する各turnは、次のagentを起動する前にGitHubへ投稿しread-after-writeで確認する。未永続化の出力を根拠にしない |
| fresh reviewer | providerを問わずturnごとに新規起動し、前roundやcoderのsession memoryへ依存しない |
| active coder | 主経路では選択coderをsubprocess化せず、対話sessionのcontextを維持する |
| durable read-only | reviewerは隔離checkout内の一時書込だけを行い、実repositoryとGitHubを変更しない |
| head binding | review承認とmerge承認は特定のhead SHAへ結び付き、headが変われば失効する |
| human merge gate | mergeはユーザーの明示承認後にControllerだけが実行する。曖昧な肯定を承認と解釈しない |

## 次に読む文書

| 目的 | 文書 |
| --- | --- |
| 完成像と合意済み制約 | [target experience](../plans/target-experience.md) |
| component、依存、Phase、受入条件 | [implementation plan](../plans/implementation-plan.md) |
| 重要な判断の理由 | [decisions](../decisions/) |
| 参考実装との比較 | [reference implementation assessment](../research/reference-implementation-assessment.md) |
| 開発への参加方法 | [CONTRIBUTING](../../CONTRIBUTING.md) |
