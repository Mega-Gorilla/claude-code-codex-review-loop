<!-- SPDX-License-Identifier: Apache-2.0 -->

# 参考実装の調査と選択移植の評価

| Field | Value |
| --- | --- |
| Authority | **Research（informative）**。要件ではない |
| 関連決定 | D-030（[target experience](../plans/target-experience.md) Section 14） |
| Last updated | 2026-08-19 |

この文書は、[ADR-0002](../decisions/0002-independent-reimplementation.md)に出典を記録した参考実装を調査した結果をまとめたものです。**要件ではありません。** 設計の正本は[implementation plan](../plans/implementation-plan.md)であり、本書はその判断材料です。

D-030のとおり、本書の判定は移植の事前承認ではありません。実際に移植する場合は、対象file、source commit、理由、適用license、移植後testを、その移植PRへ記録します。

## 1. 参照の扱い

調査対象のrepositoryは削除予定です。本repositoryはその識別子とcommit SHAを正式な参照として記録しません。本書では観測事実だけを記述し、module名は基準となるfile名で示します。

参考実装内には第三者名義ではないaudit文書があります。次の理由から、その総合評価を保証として扱いません。

| 項目 | 内容 |
| --- | --- |
| 所在 | 参考実装repository内の文書 |
| Auditor表記 | Anthropic Claude（Claude Code） |
| 対象 | 現行より前の時点のcommit |
| 現行との乖離 | audit本文が最大moduleとして挙げる規模に対し、本調査時点の作業copyでは同moduleが1.5倍以上へ増加していた |

引用するのは、**本調査で個別に再現確認できた観測事実だけ**です。

## 2. 設計原則の根拠になった観測

implementation planの設計原則は本projectの要件から導出しています。以下は、同種の設計でどの失敗が実際に起きるかを示す傍証です。

| 観測事実 | 対応する原則 |
| --- | --- |
| CLI用とSkill用でround orchestrationが二重実装され、protocol変更のたびに両方へ反映する必要が生じていた | P-002 |
| `quota` / `timeout` / `overloaded`等の語をfree-textから拾って一時障害と判定していた。HTTP statusも部分一致で判定していた | P-003 |
| comment本文を`--jq`で取得して改行分割しており、複数行bodyが1行ずつ別fragmentになっていた | P-004 |
| Skill helper層が本文をcommand line引数で渡していた（core投稿関数は既にfile経由を使用） | P-005 |
| skill modeがpermission bypass flagを無条件付与し、文書上の説明と実挙動が矛盾していた | P-006 |
| 本文末尾の署名行だけで「人間の要求」と判定しており、書込可能repositoryでは誰でも詐称できた | P-007 |
| commentにmarkerがあればauthorを確認せずrecordを復元していた | P-007 |
| Issue本文やcommentを区切りなくpromptへ埋め込んでいた | P-008 |
| 予測可能な共有pathへdefault umaskでagent responseを書いていた | P-009 |
| 2万行規模に達した時点でlint / type gateを持たなかった | P-010 |
| agent CLIと`gh`をprocess境界でfakeすることで、live serviceへ接続せず全testが動作していた | P-011 |
| 長期間versionが固定でtagもなかった | P-012 |
| helper群がpackage外にあり、`sys.path`操作でpackage内部のprivate関数を参照していた | P-013 |
| 全subprocess呼び出しがlist形式で、shell injection面が存在しなかった | P-014 |
| state fileやlogへ秘密情報が入らない設計だった | P-015 |

## 3. component別の選択移植inventory

各行は「参照するsource slice」「観測事実」「再利用するkernel」「明示的に再利用しない挙動」「必要なextension」「移植時に必要なtest」を示します。license / provenanceは移植PRで記録します。

### C-02 agent protocol schemaとcheckpoint envelope

| 項目 | 内容 |
| --- | --- |
| source slice | `protocol.py`、`repair.py`、`validate_response.py` |
| 観測事実 | 構造化protocolが約3,000行、repair処理が約1,500行まで成長している。validationとrepairの責務が複数moduleへ分散している |
| 再利用kernel | 未評価。malformed応答の分類とrepair戦略には検討価値がある |
| 再利用しない | 単一の巨大protocol module。repair結果を再validationしない経路があれば採用しない |
| 必要extension | schema version、未知versionの扱い、size limit、cross-field invariant、checkpoint envelopeとの統合 |
| 移植時test | 代表schema corpusとmalformed corpusの両方 |

### C-03 process abstraction

| 項目 | 内容 |
| --- | --- |
| source slice | `runner.py` |
| 観測事実 | `start_new_session=True`と`os.killpg`によるPOSIX process group処理が中心。**Windows Job Object相当の実装、およびWindows / POSIXの分岐は確認できなかった** |
| 再利用kernel | POSIX側のprocess group停止手順（TERM -> grace -> KILL）とtimeout時の扱い |
| 再利用しない | Windows対応があるという前提。`sleep`を外部binaryのspawnで実装する方式 |
| 必要extension | **Windows側は新規実装・新規test**。Job Object相当で子孫を確実に停止する |
| 移植時test | 両OSで孫processが残らないこと。Ctrl+Cの2段階 |

### C-05 GitHub canonical conversation transport

| 項目 | 内容 |
| --- | --- |
| source slice | `github.py`、`round_transport.py`、`round_state.py`、`comment_rendering.py`、`gh_ops.py` |
| 観測事実 | core投稿関数は一時fileと`--body-file`を使用し、長大bodyのsidecar分割と上限checkを持つ。ただし戻り値がなく、comment ID / URL / hashを返さない。Skill helper側は本文を引数で渡し、IDも返さない |
| 観測事実 | round metadataからのresumeは、compression、bounded decompression、sidecar分割、hash検証を備える。一方で**author検証がなく**、payloadへ大きなresponse本文を格納する |
| 再利用kernel | transport mechanics（sidecar分割、bounded decompression、上限check、hash検証）。公開用renderの発言者・model明示 |
| 再利用しない | 投稿helperのinterface（ID非返却）。Skill helper層の引数渡し。author検証のないrecord復元。payloadへ大きな本文を格納するmetadata設計 |
| 必要extension | comment ID / URL / body hashの返却、read-after-write確認、producer認証、予約markerのescape、record間のhash chain |
| 移植時test | 偽marker、本文とmarkerの同時編集、record削除・並べ替え、sidecar欠落・差替えのnegative test |
| 注意 | transport mechanicsとpayload schemaは別々に判定する。前者は候補、後者は本projectの「metadataは必要最小限」と方針が異なる |

### C-06 actor認証とcredential隔離

| 項目 | 内容 |
| --- | --- |
| source slice | `protocol.py`の署名解析、`agents/`のpermission構成 |
| 観測事実 | 署名行だけで人間の要求と判定していた。skill modeがpermission bypass flagを無条件付与していた |
| 再利用kernel | なし |
| 再利用しない | 署名ベースの人間判定。bypass flagの構築経路 |
| 必要extension | 全体を新規設計（login allowlist、fail closed、credential隔離、OS別file権限） |
| 移植時test | 該当なし（新規実装のnegative testで担保） |

### C-07 resumeとartifact retention

| 項目 | 内容 |
| --- | --- |
| source slice | `round_state.py`、`salvage.py`、`migrations.py` |
| 観測事実 | GitHub commentからround stateを復元する処理、salvage、crash-window recoveryの実績がある |
| 再利用kernel | crash-windowの扱いとsalvageの考え方 |
| 再利用しない | author検証のないrecord復元 |
| 必要extension | `CC_REVIEW_META`への置換、envelope versioningとの統合 |
| 移植時test | 中断後の別processからのresume |

### C-09 Codex fresh reviewer runtimeと隔離checkout

| 項目 | 内容 |
| --- | --- |
| source slice | `workdir_guard.py`、`workdirs.py`、`checks.py` |
| 観測事実 | remote検証、PR ref fetch、advertised head照合が存在する。ただし**persistent workdirを同期する方式**であり、review turnごとに作成・破棄するlifecycleではない |
| 再利用kernel | remote検証、PR ref fetch、advertised head照合、test evidenceの境界検証 |
| 再利用しない | persistent workdirの同期方式 |
| 必要extension | turnごとのcheckout作成・破棄、破棄前dirty stateのevidence記録、credential隔離との統合 |
| 移植時test | checkoutが残らないこと。reviewerからのGitHub mutationが失敗すること |

### C-11 decision / clarification / follow-up

| 項目 | 内容 |
| --- | --- |
| source slice | `followups.py`、`unresolved_items.py`、`evidence_reconciliation.py` |
| 観測事実 | 候補抽出、deduplicate、上限、Issue作成処理が存在する。未解決事項の追跡とevidence照合も実装されている |
| 再利用kernel | 候補のdeduplicateと上限処理、未解決事項のledger |
| 再利用しない | 許可前にIssueを作成し得る経路 |
| 必要extension | Codex評価schema、candidate fingerprint、本文hash、候補別許可gate |
| 移植時test | 許可のない候補がIssue化されないこと |

### C-12 test・CI qualificationとfinal reporter

| 項目 | 内容 |
| --- | --- |
| source slice | `ci_health.py`、`managed_ci.py`、`github.py`のbranch protection取得 |
| 観測事実 | CI待機とbranch protection required checksの取得が存在する。ただしHTTP statusを文字列の部分一致で判定する箇所がある |
| 再利用kernel | required checksの解決、CI結果の集約 |
| 再利用しない | 文字列部分一致によるHTTP status判定（P-003） |
| 必要extension | bounded waitの設定、`WAITING_CI` checkpoint、report JSONからの決定論的render |
| 移植時test | timeout時のcheckpointと明示resume |

### C-14 Issue modeとIssue-to-PR handoff

| 項目 | 内容 |
| --- | --- |
| source slice | `issue_pr_handoff.py`、`decomposition.py` |
| 観測事実 | Issue側markerと既存PRの再発見は存在する。ただし**投稿helper自体は冪等ではなく**、二重回避は大きなorchestratorのcall-site順序に依存する。Issue / PR両側のhandoff recordを揃える設計にはなっていない |
| 再利用kernel | handoff metadataのcodec、既存PR再発見のalgorithm |
| 再利用しない | call-site順序に依存する二重回避 |
| 必要extension | **bilateral handoffは新規設計。** 片側だけ成立した状態からの再開 |
| 移植時test | 片側投稿済みからの再開で重複しないこと |

### 横断: failure taxonomy

| 項目 | 内容 |
| --- | --- |
| source slice | `errors.py`、`transient.py`、orchestrator内の失敗分類 |
| 観測事実 | 失敗分類とrecovery ladderは充実しているが、一部がfree-textの部分一致に依存する |
| 再利用kernel | 失敗categoryの分類軸とrecovery ladderの構造 |
| 再利用しない | 文字列部分一致による分類（P-003） |
| 必要extension | exit code、構造化出力、`gh api` statusを根拠にする |
| 移植時test | 「innocentなtext」corpusで誤分類しないこと |

### 再利用しないと判定した領域

| 領域 | 理由 |
| --- | --- |
| orchestrator | 単一moduleが大規模化し、CLIとSkillで二重実装されている。P-002およびSection 3の制御構造と異なる |
| Skill helper層の投稿経路 | P-004とP-005の両方に反する |
| permission / sandbox構成 | P-006に反する |
