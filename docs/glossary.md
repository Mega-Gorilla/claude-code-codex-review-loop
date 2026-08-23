<!-- SPDX-License-Identifier: Apache-2.0 -->

# Glossary

| Field | Value |
| --- | --- |
| Status | **Accepted**（PR #4のユーザー承認とmergeにより確定） |

本projectの文書で使う用語を定義します。各文書は独自用語の初出時にここへlinkします。

## 実行の単位

| 用語 | 定義 |
| --- | --- |
| run | 1回の`cc-review`実行。開始から終了stateまでを1 run IDで追跡する |
| round | reviewとfixの1往復。Codexが現在headをreviewし、必要な修正を経て新headになるまで。既定の上限は3 |
| turn | agentまたはユーザーの1つの論理的な発言。GitHubへ1 commentとして永続化する |
| action | active hostが1回で実行する作業単位。`HOST_ACTION`として構造化する |
| clarification turn | ClaudeからCodexへの1回の質問と、Codexからの1回の回答の一往復。同一topicあたり最大5回 |

## 記録と状態

| 用語 | 定義 |
| --- | --- |
| canonical record | GitHub Issue / PR上に永続化され、read-after-writeで確認済みの正式な記録。未永続化の内部出力は次工程の根拠にしない |
| canonical conversation | canonical recordの集合。Claude、Codex、ユーザーが共有する唯一の正式な会話履歴 |
| checkpoint | resumeのためにlocalへ保存する状態。GitHubがcanonicalであり、checkpointはcacheかつ診断情報 |
| checkpoint envelope | checkpointを格納するversion付きの外枠。version、migration policyを持つ |
| ledger | findingやdecisionの一覧と、それぞれの現在の扱い（disposition）を追跡する記録 |
| idempotency marker | 投稿のtimeout時に、同じturnが既に投稿済みかをGitHubから検索するための識別子 |
| fingerprint | 同一のfinding、decision、follow-up候補を、head変更をまたいで同一と判定するための識別値 |
| head binding | 承認やreview結果を特定のhead SHAへ結び付けること。headが変われば失効する |

## transport

| 用語 | 定義 |
| --- | --- |
| read-after-write | 投稿後にGitHubから再取得し、comment / review ID・URL・本文hash・対象head SHAを確認してからturnをcompletedにする手順 |
| 予約marker | Controllerだけが本文末尾へ付加する機械metadata（`CC_REVIEW_META`のHTML comment）。agent生成本文中の同tokenは投稿前にescapeされる（ADR-0007） |
| conversation cursor | 差分取得の起点となるcursor（updated_atのinclusive filter）。境界のcommentは再配送されるため呼び出し側がdedupeする |
| review thread | PRのfile / line固有のfinding議論のthread。解決状態（isResolved）を持ち、replyはthread先頭commentへ行う |
| fallback comment | threadへのreplyが恒久的に不可能な場合に、元comment URLを前置して投稿するconversation comment |

## identity（canonical record検証）

| 用語 | 定義 |
| --- | --- |
| record chain | 同一run内の内部recordがmarker payloadの`seq`（1始まり通し番号）と`prev`（直前recordの本文hash）で連結された系列。specの正本はADR-0008 |
| high-water mark | checkpointへ保存する確認済み最大sequence番号`N`。`N`以下の欠落は検出でき、`N`より後のtail truncationは検出できない残存risk（AC-C06-09） |
| violation binding | integrity violationの決定論的識別子（`iv:{条件}:{run}:{対象}`）。同一違反の再検出は同一bindingになり（冪等）、C-01のcanonical orderと両立する |
| producer allowlist | 内部record（chain）の正当な投稿者login集合（通常はControllerの認証login）。承認受理用のallowlistとは別の集合 |
| canonical projection | markerへ載せる、検証済みpayloadからのscalar射影（結果値・round / turn・fingerprint・対象binding・payload hash等）。GitHubだけでrecordの意味を復元するための最小情報で、規約の正本はADR-0010 |
| record binding | canonical recordの決定論的識別子（`cr:{run}:{seq}:{hash}`）。markerの`key`（idempotency key）と`PersistRecord`のbindingは同一値で、marker付加前の入力だけから導出する |
| semantic payload hash | 正規化済み公開本文・検証済みpayloadのhash・`pay`を除く射影を覆うSHA-256（markerの`pay`）。marker付加前の入力だけで決まり、local artifactをGitHub上のrecordへbindし、binding導出の材料にもなる |
| external evidence | ユーザーがGitHubへ直接記入したcommentを、C-06がcomment ID・body hash・actor（allowlist完全一致）・対象headで検証して受理したevidence。再投稿（PersistRecord）を伴わない |

## security policy

| 用語 | 定義 |
| --- | --- |
| redaction | credential等の秘密情報を公開前に`[REDACTED:<種別>]`へ置換する変換。patternはC-04が一元管理し、投稿本文・prompt・log・artifactへ共通適用する（ADR-0006） |
| trust rule | fork PR・trusted author集合外のauthor・agent設定file変更に対する、入力データだけで再現できる純粋な判定。判定結果は「目立つ表示」と「実行の既定拒否」の2用途を持つ |
| permission profile | Claude Code等へ指定する権限presetの選択（Auto / acceptEdits / default / dontAsk）。bypass系は値域に存在しない（P-006）。承認受理用のallowlist（C-06）とは別のauthority |
| fork PR | head repositoryがbase repositoryと異なるPR。headを特定できない場合もforkとして扱う（fail closed） |

## process

| 用語 | 定義 |
| --- | --- |
| process tree | C-03が起動する子processとその全子孫。POSIXはprocess group、WindowsはJob Objectで1単位として捕捉・停止する |
| grace period | graceful停止の要求から強制停止までの待機時間。C-03は既定値を持たず、既定値の解決はC-12の設定解決で行う |
| graceful要求（requested） | graceful停止の要求がOSに受理されたこと。配送保証ではなく、停止の成立はtree生存の観測でのみ確認する（ADR-0005） |
| tree ref | 元のhandleを持たない別processがtreeを再停止するためのidentifier。Windowsは(pid, job name)、POSIXは(pid, pgid) |

## 役割

| 用語 | 定義 |
| --- | --- |
| Controller | LLMを内包しない決定論的なstate machine。GitHub、process、state、mergeを調整する |
| active host | 既存の対話型Claude Code session。会話contextを維持したまま実装・test・説明を担当する |
| headless adapter | 対話sessionが存在しない復旧経路で、Claude coderをsubprocessとして起動するadapter |
| fresh reviewer | review turnごとに新規起動するCodex subprocess。前roundのsession memoryを引き継がない |
| final reporter | 承認済みheadの変更と検証履歴をread-onlyで説明するCodexの役割 |

## 権限と隔離

| 用語 | 定義 |
| --- | --- |
| durable read-only | 隔離checkout内でのtest / build / 再現に必要な一時書込は許可し、実repositoryとGitHubへの永続変更は禁止する状態 |
| isolated checkout | 対象headから新規作成し、review終了後に破棄する検証専用のcheckout |
| tool permission | Claude Code等が個々の操作を許可するかどうかの判定。workflow承認とは別のauthority |
| workflow承認 | merge、follow-up Issue作成、仕様判断に対するユーザーの明示的な承認。tool permissionで代替できない |
| allowlist | ユーザー判断を受理できるGitHub loginの明示的な一覧。完全一致を必須とする |
| reviewer home | reviewerへ差し替えるHOME相当の一時領域。`GH_CONFIG_DIR` / `XDG_*` / git global configの探索先をここへ閉じ込める（ADR-0009） |
| private directory | 作成者のみがアクセスできるdirectory。POSIXは`0o700`、Windowsは現userの単一ACE。排他作成し、実効権限を読み戻して検証する |
| resume ticket | 停止したtool操作**だけ**の再実行許可。Permission ID・head・tool・scopeの完全一致で発行され、workflow承認のevidenceにはならない |

## review

| 用語 | 定義 |
| --- | --- |
| blocking finding | mergeの前に解決が必要なreview指摘 |
| Approved follow-up | 現在のIssue / PRの完了には必須でないが、別scopeで追跡する価値がある改善候補。ユーザーが候補ごとに許可した場合だけIssue化する |
| decision request | Claudeが実装中に、要件だけでは一意に決められない選択に到達したときに作成する判断依頼 |
| decision brief | Codex reviewを反映した最終的な判断資料。候補、利点・欠点、推奨、意見の相違を含む |

## 文書のauthority

| Status | 意味 |
| --- | --- |
| Agreed / Accepted | normative。要件および合意済みの制約 |
| Draft | review中であり未確定 |
| Research | informative。判断材料であり要件ではない |
| Non-normative example | 例示。仕様と食い違う場合は正本を優先する |
