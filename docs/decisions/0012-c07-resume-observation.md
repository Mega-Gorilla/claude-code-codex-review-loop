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
11. **旧世代の承認はsupersessionで扱う**。canonical recordはappend-onlyなので、旧headの承認は履歴に残り続ける。同種の承認が現headにも存在する場合、旧世代は`SUPERSEDED`（診断用に保持し判定へ影響させない）とし、現headに同種の承認が無い場合だけ`VOIDED`として失効させる。単純に「1件でも旧headの承認があればfallback」とすると、fresh reviewで再承認しても旧recordは履歴から消えないため、そのrunは**以後永久にfallbackし続ける**
12. **merge gateへの復帰はGitHub上の承認を必須にする**。`SAME_HEAD_VALIDATED`（C-01のM-SH「同一head・全条件再確認」）は、現headへbindされた**merge承認とreview承認の両方**が確認できる場合だけ返す。local checkpointの`approved_sha`だけでmerge gateへ復帰させない（GitHub canonical）。M-SH自体は承認を検査しないため、**発行してよいかの判断はC-07側にある**
13. **判定を返せない場合は`ReconciliationStopped`で停止する**（verdictにしない）。承認を確認できない`MERGE_FAILED`は、C-01に受理される遷移が無い（bareな`MERGE_FAILED` + `ResumeValidated`は`TransitionRejected`、M-HCはhead変更時）。合法な遷移が無い状況をverdictで表すと、消費側が「前進してよい」と誤読するか実行時に拒否される。停止結果は理由（`ReconciliationStop`）と不足している承認種別を持つ
14. **`VALIDATED`は「head照合が前進を妨げない」ことだけを意味する**。どのresume eventで再開するかはstateに応じてC-10が選ぶ（`R-P` / `R-A1` / `R-A2` / `R-D` / `R-B` / `R-CI` / `R-RT`）。head照合が下せる判断の範囲を超えてstateごとの再開手段を決めない
15. **chain recordを伴わない承認経路（D-021）は注入で受ける**。GitHub直接commentの承認はC-06が`AcceptedUserDecision`として受理し`PersistRecord`を発行しない = chainに現れない。`reconcile_head`は`external_approvals`として同じ`ApprovalEvidence`の形で受け取り、判定を一本化する（組み立てはPR-4）
16. **`ResumeVerdict`は判定であってevent名ではない**。C-01のどのeventへ写すかは現在のstateにも依存する（例: `FALLBACK_REQUIRED`はFAILED / BLOCKEDでは`ResumeFallbackRequired`、MERGE_FAILEDではM-HCの`HeadChangedExternally`）。**eventの構築はC-10**が行い、C-07はenumを返すに留める（Phase 7がevent対応表を持ち込まない）
17. **観測が成立しない場合は判定しない**（`HeadUnobservable`）。head / base SHAが40桁小文字hexでない応答は、推測して判定へ進めずに停止する
18. **PRのclosed / mergedは判定へ同梱して返す**（`HeadReconciliation.observation`）。stateの決定はC-10の責務だが、消費側が観測事実を見落とさないよう結果に含める

### C-05への追加

19. **追加は`get_pull_request` 1つに限る**。戻り値は`UnverifiedPullRequest`で、**未検証metadataのまま返す**（AC-C05-05を変えない）。`head.repo`がnullの場合だけ空文字列へ写す。これはC-04の`TrustInput`が「head repositoryが空ならforkとして扱う」（fail closed）という既存規約と一致する
20. **隔離checkoutのHEADとの照合はC-09（AC-C09-04）へ残す**。C-07が見るのはadvertised headだけで、fetch / refspec / checkout側の照合を持ち込まない

### artifact binding（AC-C07-05）

21. **確認できないartifactはcacheとして破棄する**（`STALE_HEAD` / `UNBOUND_RECORD` / `RECORD_MISMATCH` / `MISSING` / `CONTENT_MISMATCH`）。GitHub側が常に上位であり、artifactの不一致でGitHub側を疑わない。破棄した事実は結果として返し、silentに直さない
22. **record bindingを持たないartifactは破棄し、持つartifactは参照先recordの実体と突き合わせる**。AC-C07-05は「GitHub recordとlocal artifactが、**いずれも**approved head SHAへbindされている」ことを要求するため、artifact側の主張だけでは足りない。照合には検証済みrecord（`VerifiedRecord`）そのものを渡し、**recordの`head_sha`がapproved headと一致すること**と、artifactが`comment_id`を記録している場合は**それが参照先recordのcomment IDと一致すること**を要求する（不一致は`RECORD_MISMATCH`）。artifactの`kind`は種別を表すfree-form fieldでrecord種別に限定されないため照合しない。record同一性はbinding・comment ID・headで一意に定まる
23. **head不一致はfileを読む前に落とす**。古いheadのartifactを読み出す理由が無く、I/Oを減らすほどcacheがcanonicalへ昇格する余地も減る
24. **content hashの読み出しは注入する**（判定を純粋に保つ）。実I/O側の`artifact_content_hash`は、base外を指すpath・絶対path・不在・作成者限定でないfileをすべて「読み出せない」へ倒す（fail closed。AC-C06-05の契約を再利用する）

## Consequences

- PR-4は本PRの部品を配線してresume contextを組み立てる: 候補列挙 -> 各候補のchain検証 -> `select_run` -> `reconcile_head` -> artifact照合 -> pending recordの再発行directive -> 直接回答候補
- C-10はresume時に`ResumeVerdict`と現在のstateからC-01 eventを構築し、`ReconciliationStopped`はeventを発行せず理由を提示する（AC-C10-03のlock拒否と同じ「停止して提示する」経路）。`FALLBACK_REQUIRED`はFAILED / BLOCKEDでは`ResumeFallbackRequired`（R-F / R-FB: 継続破棄・承認失効 + fresh review）、MERGE_FAILEDでは`HeadChangedExternally`（M-HC）、`SAME_HEAD_VALIDATED`は`ResumeSameHeadValidated`（M-SH: merge gateへの復帰）へ対応する
- 承認の`SUPERSEDED`はPhase 7では判定から外すだけだが、C-10がledger表示（どの世代の承認が現在有効か）へ再利用できる
- C-04はfork判定の入力（base / head repository、author login）を`get_pull_request`から得られるようになり、独自のPR取得を持たない
- Phase 14のcleanupは`discover_local_runs`をrun列挙の入口として再利用できる（active / lock保持中の除外判定はlock側の情報と組み合わせる）
