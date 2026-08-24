<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0012: C-07 resumeの観測と承認失効

- Status: Accepted
- Date: 2026-08-24

## Context

Phase 7はPR-1（ADR-0010: recordのcanonical projection）とPR-2（ADR-0011: checkpoint storeとPR lock）で「GitHubからrecordの意味を読める」「localのcacheを安全に読み書きできる」までを実装した。残るのは**再開の判断そのもの**で、次の3つが揃っていない。

1. **どのrunを再開するのか**を決める経路が無い（state root配下のrun列挙も、GitHub側のrun候補列挙も無い）
2. **headが動いたかを観測する経路が無い**。C-05の公開APIはcomment / thread / 汎用`run_gh_api`だけで、PRのadvertised headを構造化して取れず、AC-C07-03（外部からheadが更新された場合に旧承認が失効する）の入力が作れない
3. **local artifactがapproved headへbindされているかを照合する経路が無い**（PR-2でfieldは追加したが読み手がいない）

Issue #12の実装契約（着手前レビューで確定）が前提: **Phase 7の成果物はresume contextであり`MachineState`の完全replayではない** / 判断根拠は検証済みrecordに限る / 曖昧なら停止 / C-05への追加はPR metadataのread primitive 1つだけ。

本PR（PR-3）は観測と純粋判定に限定し、resume contextの組み立てとpending recordの再発行はPR-4へ分ける。**本PR単体ではresumeはend-to-endで動かない**（AC-C07-01 / 02 / 06はPR-4で満たす）。

## Decision

### 候補列挙と判断根拠の分離

1. **run候補の列挙と、候補の判定を分ける**。列挙（`enumerate_run_candidates`）はstate root配下のrun directoryとGitHub側markerが名乗るrun IDを集めるだけで、**未検証のmarkerを候補の名前にしか使わない**。判定（`select_run`）の入力は検証済みchain（`ChainVerification`）とcheckpointの読込結果に限る。「C-07以降は検証済みrecordだけを入力にする」を、候補の名前と判断根拠を分けることで満たす
2. **marker解析はC-06の`parse_record_marker`へ委譲する**（`_parse_chain_payload`を公開名へ改名）。marker意味論の定義をC-06 1箇所に保ち、C-07側でparseを複製しない。公開関数のdocstringに「構造parseのみでactor / chainを検証していない = evidenceではない」を明記する
3. **候補は「GitHub側markerが名乗るrun」と「checkpointが対象repository / 番号を指すrun」の和集合**にする。どちらにも該当しないlocal runは件数を残すだけで候補にしない。state rootは全worktree・全repositoryで共有するため（ADR-0011 決定12）、無関係なrunの壊れたcheckpointが別repositoryのresumeを止めてはならない
4. **checkpointが読めない候補は選択せず停止する**（`RunUnavailable`）。読めないcacheを「無いもの」として扱うのはsilent repairであり、ADR-0011 決定4と矛盾する
5. **選択は非terminalな候補がちょうど1つの場合だけ**。0件は`RunNotFound`、2件以上は`RunAmbiguous`で候補を提示して停止する（推測して前進しない）

### terminal判定

6. **構造的signalだけで判定する**: checkpointの`state`が`TERMINAL_STATES` / 検証済みrecordに`USER_CANCEL`がある / 検証済みrecordの最大seqが`FINAL_REPORT`。eventへ変換して状態機械を回さない（record -> eventの対応表はC-10 / C-11の所管）
7. **violationのあるchainのsignalは終端判定に使わない**。壊れた系列を根拠に候補を消すと、偽のmarker 1件でrunを隠せてしまう。この場合はcheckpointのstateだけで判定し、判定できなければ非terminal（= 候補に残す）とする

### head照合と承認失効（AC-C07-03）

8. **承認の失効はGitHub由来の値だけで決める**: 承認recordのmarkerが持つ`head`（`MERGE_APPROVAL`は`approved_head_sha`、`REVIEW_RESULT`は`target_head_sha`が射影元。ADR-0010の`PROJECTION_SPECS`）と、PRが現在advertiseしているheadの一致を見る。checkpointは**変化の分類にしか使わない**。分類を誤っても失効した承認が甦らない構造にする
9. **承認とみなすrecordは2種のみ**: `MERGE_APPROVAL`と、結果が`APPROVED`の`REVIEW_RESULT`。不変条件「review承認とmerge承認は特定のhead SHAへ結び付き、headが変われば失効する」に対応する2種で、それ以外のrecordを承認として扱わない
10. **coder pushでも承認は失効する**。head bindingはpushの主体に依存しない。`HeadChange`（`UNCHANGED` / `CODER_PUSH` / `EXTERNAL_UPDATE` / `UNKNOWN`）は観測事実として返すが、失効判定には使わない。coder更新と外部更新の最終的な区別はAC-C10-06（Phase 10）
11. **`ResumeVerdict`は3値**（`VALIDATED` / `FALLBACK_REQUIRED` / `SAME_HEAD_VALIDATED`）で、C-01のresume event（`ResumeValidated` / `ResumeFallbackRequired` / `ResumeSameHeadValidated`）へ1対1で対応する。**eventの構築はC-10**が行い、C-07はenumを返すに留める（Phase 7がevent対応表を持ち込まない）
12. **観測が成立しない場合は判定しない**（`HeadUnobservable`）。head / base SHAが40桁小文字hexでない応答は、推測して判定へ進めずに停止する
13. **PRのclosed / mergedは判定へ同梱して返す**（`HeadReconciliation.observation`）。stateの決定はC-10の責務だが、消費側が観測事実を見落とさないよう結果に含める

### C-05への追加

14. **追加は`get_pull_request` 1つに限る**。戻り値は`UnverifiedPullRequest`で、**未検証metadataのまま返す**（AC-C05-05を変えない）。`head.repo`がnullの場合だけ空文字列へ写す。これはC-04の`TrustInput`が「head repositoryが空ならforkとして扱う」（fail closed）という既存規約と一致する
15. **隔離checkoutのHEADとの照合はC-09（AC-C09-04）へ残す**。C-07が見るのはadvertised headだけで、fetch / refspec / checkout側の照合を持ち込まない

### artifact binding（AC-C07-05）

16. **確認できないartifactはcacheとして破棄する**（`STALE_HEAD` / `UNBOUND_RECORD` / `MISSING` / `CONTENT_MISMATCH`）。GitHub側が常に上位であり、artifactの不一致でGitHub側を疑わない。破棄した事実は結果として返し、silentに直さない
17. **record bindingを持たないartifactは破棄する**。AC-C07-05は「GitHub recordとlocal artifactが、いずれもapproved head SHAへbindされている」ことを要求するため、record側の対応が無いartifactはbindの主張として不完全である
18. **head不一致はfileを読む前に落とす**。古いheadのartifactを読み出す理由が無く、I/Oを減らすほどcacheがcanonicalへ昇格する余地も減る
19. **content hashの読み出しは注入する**（判定を純粋に保つ）。実I/O側の`artifact_content_hash`は、base外を指すpath・絶対path・不在・作成者限定でないfileをすべて「読み出せない」へ倒す（fail closed。AC-C06-05の契約を再利用する）

## Consequences

- PR-4は本PRの部品を配線してresume contextを組み立てる: 候補列挙 -> 各候補のchain検証 -> `select_run` -> `reconcile_head` -> artifact照合 -> pending recordの再発行directive -> 直接回答候補
- C-10はresume時に`ResumeVerdict`をC-01のresume eventへ写す。`FALLBACK_REQUIRED`は継続破棄・承認失効 + fresh review（R-F / R-FB）、`SAME_HEAD_VALIDATED`はmerge gateへの復帰（M-SH）に対応する
- C-04はfork判定の入力（base / head repository、author login）を`get_pull_request`から得られるようになり、独自のPR取得を持たない
- Phase 14のcleanupは`discover_local_runs`をrun列挙の入口として再利用できる（active / lock保持中の除外判定はlock側の情報と組み合わせる）
