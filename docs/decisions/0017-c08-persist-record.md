<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0017: C-08の`PersistRecord`実行と6つのcrash window

- Status: Accepted
- Date: 2026-08-25

## Context

PR-2（ADR-0015）は、C-01が持つ`RecordProduced` -> `PersistRecord`の境界でstep engineを切った。`submit`は結果を検証して`RecordProduced`まで状態を進め、record transactionを保存するところで止まる。したがって`advance`はpending recordがある間`PersistRequired`を返すだけで先へ進めない。

本PRは`PersistRecord`を実行する。着手前レビューの指摘どおり、これはhost actionの結果専用ではない。C-01は`REVIEW_RESULT`（C-09）や`FINAL_REPORT`（C-12）にも同じ`PersistRecord`を発行するため、**汎用の境界**として実装する必要がある。

## Decision

### 汎用境界

1. **`persist`は`pending_record`に置かれた任意のrecordを扱う**。record kindでの分岐をhost action側へ寄せない。`advance` / `submit`と同じrun load経路（`run_context`）を共有する
2. **record -> eventの写像はportが担う**（`RecordEventPort`）。registryが1対1で持つのはhost actionの結果8種だけで（ADR-0014 決定8）、`REVIEW_RESULT`の2値や`CLARIFICATION_ANSWER`の5値のような**値によるdiscrimination**はC-10 / C-11の領域である。さらに`extra_event_inputs`（`ProgressReport` / `head`）もC-08が作らない値であり、同じportから来る。host actionの結果についてはregistryの`build_event`をそのまま使えばよい（Phase 8のfakeがその形を示す）
3. `build_event`は**宣言された入力と実際の入力の一致**を要求する。過不足があれば構築しない（C-08が作らない値をNoneで埋める経路を作らない）

### chain gate

4. **`RecordSourcePort`はC-06の`ChainVerification`をそのまま返す**。従来は`records`だけを返して**violationsを捨てて**いたため、壊れたchainの上でもseqとprevを決めてtransactionを発行できた。`is_intact`をgateにし、`persist`と`submit`の両方で使う
5. violationがあれば、**推測して投稿せず**C-01の`RecordIntegrityViolationDetected`へ1件ずつ入力する。集合へのunionと停止gateはC-01が扱う（I3 / I5）。C-01が受理しない位置ではstateを動かさず停止する

### 読み戻せない状態を書かない

6. **次の状態に無い付随値は必ず消してから書く**。`awaiting` / `pending_record`と同じく、`procedure` / `block` / `deferred_integrity`も書き込み前に削除する。残すと前の状態の値が混ざり、halt完了・block解消・deferred消費のような**正当な遷移まで保存できなくなる**
7. **保存する前にround-tripを検証する**（`with_verified_machine_state`）。C-01が返す状態には、checkpointがまだ表現しない付随値（`CancellingProcedure`、`ProgressBlock`等）を持つものがある。黙って落として保存すると、次の`load_run`が復元できず**runが再開不能になる**。書く前に読み戻して一致を確認し、一致しなければ**保存せず停止する**
8. **integrity遷移が返す状態を表現できるようにする**。`state.procedure`（halt gate）/ `state.block`（RECORD_INTEGRITY）/ `state.deferred_integrity`をadditiveに追加した（ADR-0004 rule 10。version bumpなし）。未対応のprocedure / block種別は**readerがfail closed**にし、`NORMAL`へ丸めない
9. violation集合はC-06のchain検証で再導出できる値だが（ADR-0011 決定8）、**readerは純粋関数でGitHubへ問い合わせられない**。ここに保存するのは**状態復元のためのcache**であり、検出の正本は常にchain検証である（保存値が古くても、再検証が違反を再び検出する）。この非対称はproject全体の「GitHubがcanonical、checkpointはcache」と同じ形である
10. **複数violationのcommandは順に蓄積する**。halt gateへ入るのは最初の検出だけで`HaltRun`もそこで一度だけ発行されるため、上書きすると停止命令が呼び出し側へ届かない

### 順序と失敗の扱い

11. 順序は `pending_record -> transaction読込 -> chain gate -> evaluate_pending -> 投稿 -> chain再検証 -> event -> transition -> transaction消費`。**投稿済みかの判定はC-07の`evaluate_pending`へ委ね**、C-08側で独自判定しない
12. **`ensure_comment_posted`の戻り値で分岐しない**。read-after-writeで本文hashが違う場合（`PostHashMismatch`）も、改変の疑いとしてC-06のchain検証へ委ねる。検証済みchainに現れないか、本文がtransactionと一致しない形で必ず捕まる
13. **`body_hash`が無いtransactionは投稿する前に拒否する**。schema上optionalなのは既存fieldの制約を強化しないためで（ADR-0013 決定9）、producerが省略してよい意味ではない（ADR-0014 決定21）。無いまま投稿すると、投稿後の照合が必ず失敗して外部commentだけが増える
14. **transactionは検証が終わってから消す**。消した時点で再発行できなくなるため、投稿・検証・stateの前進が済むまで残す
15. bounded retryが尽きた投稿失敗は`RunFailed`をC-01へ入力する（ADR-0015 決定17と同じ規則）。**transactionは消さない**（投稿できていない以上、次のresumeが再発行する）
16. 確認後に本文が改変された場合（再検証で`body_hash`が違う）は、そのrecordをevidenceにせず停止する

### crash window

17. 永続化の中断窓は**6つ**とし、どこで落ちても**GitHub上の当該recordが1件**であることをtestで固定する: W1 transaction保存後・投稿前 / W2 投稿の成否不明 / W3 投稿後・確認前 / W4 確認後・検証前 / W5 検証後・checkpoint前 / W6 checkpoint後
18. W2は**中断位置に依存させない**。C-05は成否不明をidempotency markerの検索で解決し、C-08は再開時に`evaluate_pending`で投稿済みかを判定するため、どちらの層でも重複を作らない。testはtimeout位置を動かして同じ結論になることを確かめる

## Consequences

- C-09以降は`PersistRecord`の実装を持たず、portへrecord -> eventの写像を与えるだけでよい
- 本PRはAC-C08-05 / 07（PR-2で充足）に加えて、canonical record persistenceの経路を完成させる。AC-C08-01 / 02 / 04 / 06はadapterとprocess境界を持つPR-3が扱う
- `AWAIT_USER`搬送路（ユーザー入力の取り込み）は本PRに含めない。outbound（recordを出す）とinbound（入力を取り込む）で分け、PR-2cが扱う
