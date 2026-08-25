<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0015: C-08 step engineのadvance / submitとretry契約

- Status: Accepted
- Date: 2026-08-25

## Context

ADR-0014（PR-1）でaction registryと`HOST_ACTION` / `SUBMIT` v2を確定した。本PRはengineを実装する。着手前のIssue #13レビューで、engineを書く前に決めるべき穴が4件出た。

1. **`AWAIT_USER`の応答経路が未定義**。正本は`advance -> HOST_ACTION | AWAIT_USER | TERMINAL`だが、確定したのは`HOST_ACTION`側だけで、ユーザー入力をengineへ戻すenvelopeが無い
2. **action payloadとevent追加入力の供給元が無い**。`RequestHostAction`はaction enumしか持たないが、payloadは`round` / `finding_ids` / `decision_id`を要する
3. **`FAILED` submitの意味が未定義**。`RunFailed`へ写すのか、retryの主体は誰か、result fileは要るのか
4. **retry済みsubmitの冪等性が成立しない**（blocking）。checkpointの`host_action.submit`はreceiptを1件しか持てず、attempt 2を発行するとattempt 1の同一再送を判別できない

## Decision

### PRの分割（本PRの範囲）

1. **C-01が持つ`RecordProduced` -> `PersistRecord`の分割を、そのままPRの継ぎ目にする**。本PRは結果を検証して`RecordProduced`まで状態を進め、record transactionを保存するところまでを担う。GitHubへの投稿・read-after-write・C-06検証・`*Verified` eventは`PersistRecord`の実行として後続PRが行う。発明した継ぎ目ではなく、state machineが元から持つ境界である
2. `advance`はpending recordがある間`PersistRequired`を返す。`AWAIT_USER`は搬送路（envelopeとuser-input submit）が未実装のため、awaitingを載せた`AwaitUser`を返すところまでとする（穴の1は本PRでは閉じない。実装は次PR）

### pure step engineの意味

3. **「pure」はprocess / CLIに依存しないという意味で、I/Oが無いという意味ではない**。engineはcheckpointとresult file、後続PRでは投稿を扱う。path・ID・時刻・上限値・retry budgetは**すべて引数**で受け取り、engine自身は既定値を持たない（既定値の解決はC-12）
4. **stateを進める公開経路は`advance`と`submit`だけ**にする（AC-C08-03）。他のpublic関数はreaderとwriter（pure）に限る

### port

5. 値の供給元を4つのtyped portで切る: action payload / evidence（同梱する検証済みrecord）/ 検証済みrecord列（binding採番とprev body hash）/ record本文。Phase 8はfake実装で満たし、C-10 / C-11が本実装へ差し替える
6. **portの戻り値は既存の型に限る**。C-10 / C-11のdomain形状（finding ledger、decision registry）を先取りしない
7. **C-08はrecordの文面を書かない**。schema検証済みJSONを正とし、そこから決定論的にrenderする（final reportと同じ方針）。kindごとの表現はC-10 / C-11の領域なので、本文はportから受け取り、engineはC-05の`prepare_public_body`（改行正規化 -> sanitize -> redact -> header）へ通すだけにする
8. **checkpoint I/Oはportにしない**。C-07の`load_checkpoint` / `save_checkpoint`が既にpath注入で、schema検証とatomic replaceを担う。ここへ層を挟んでも得るものが無い

### evidence（AC-C08-07）

9. **`verified_records`は対象headの全recordではなく、actionごとに選ぶ**（DOD-02）。`ActionSpec.evidence_kinds`を正本とし、engineは許可kindと**seq昇順**を検査する。違反は停止する（順序が崩れた根拠を渡さない）
10. **根拠recordの対象headが、そのactionの`expected_head_sha`と一致することを検証する**。headを見ないと、あるheadへbindしたenvelopeへ別headの根拠を同梱でき、head bindingを迂回できる。fail closedで停止し、head跨ぎの根拠を要するactionが現れた場合はregistryの明示的な規則として追加する（暗黙に通さない）
11. 再提出（`REVISE_DECISION_REQUEST`）だけは、差し戻し対象そのものである同種recordを根拠に含む。他のactionでは根拠集合と結果集合が交わらない（contract testで固定）

### 結果recordの対象head

12. **recordのmarkerに載るheadはpayloadのhead fieldが正本**（`PROJECTION_SPECS`の`head_source`）。`FIX_RESULT`は`pushed_head_sha`＝新しいheadを対象にするため、actionの`expected_head_sha`へ縛れない
13. ただし**`target_head_sha`を持つrecordは、そのactionが束ねられたheadと一致しなければならない**。head bindingを迂回して別headのrecordを作らせないための構造的な制約である。head自体の正当性（PRのadvertised headとの一致）はC-06 / C-10が検証する

### result fileの受理

14. result pathは**engineがrun directory内へ払い出す**（plan L295）。受理時に6点を検証する: relative path / run directory配下 / path上にsymlinkと`..`が無い（`path == path.resolve()`の実体判定）/ regular fileとして実在 / **読み込む前に**size上限（`stat`）/ 作成者限定かつ実体を共有しない（`verify_private_file`）
15. 内容は当該result variantの**既存record schema**で検証し、`result_hash`と実file内容のhashが一致しない場合は停止する

### `FAILED` submitの意味

16. **`FAILED`は「hostが結果を出せなかった」に限る**。permission停止・外部依存・質問・判断依頼は`COMPLETED`のresult variant（ADR-0014 決定5）であり、`FAILED`へ落とさない。混ぜるとC-01の構造化blockが失われる
17. `RunFailed`は「**bounded retry後の失敗**」（`domain/events.py`）である。したがって`FAILED` = 即`RunFailed`ではない。`TRANSIENT`かつretry budget残なら同じlogical actionの次のattemptを発行できる状態にし、それ以外（`PERMANENT` / `AUTH` / `NOT_FOUND`、またはbudget尽き）は`RunFailed`をC-01へ入力する
18. **retry budgetの管理主体はengine**（次のattemptを発行するか否かを決める）。ただし**「hostが黙ってretryしてもnonceで弾ける」は誤りなので撤回する**。nonceが観測できるのはsubmitだけで、hostが1回のaction実行の内部で何回試したかはprotocol上observableではない
19. `result_hash`は`FAILED`でも必須のまま、**失敗詳細fileのhash**と定義する。空の失敗を許すとsubmit hashだけが残り診断できない。失敗詳細は`HOST_FAILURE` schema（action kind / error category / summary / detail）で、submitの`error_category`と一致しなければ停止する。run directory内のartifactなのでC-04のredaction対象であり、engineが外へ渡すsummaryにはredactionを適用する

### retry済みsubmitの冪等性（blockingへの対応）

20. **attemptごとに新しいaction IDとnonceを発行し、logical actionは`correlation_id`で結ぶ**。binding 8項目（plan L295）を変えずに済み、「1 action ID = 1 nonce = 1 receipt」という単純な不変条件が保てる。`attempt`をbindingへ足す案は正本が定めた8項目を変えることになり、`HOST_ACTION` v3を要するため採らない
21. **receiptはledgerとして複数保持する**。過去attemptのreceiptを捨てると、遅れて届いた同一submitを判別できず、停止すれば「同一submitの再送は冪等」に反し、受理すればone-time nonceの境界が壊れる。判定は`submit_hash`（submit envelope全体のcanonical hash）で行う: 一致は冪等replay、同じattemptで不一致は停止、ledgerにも未完了actionにも無いattemptはstaleとして停止
22. **ledgerはlogical action 1件分だけを保持する**。retry attemptの発行時は保持し、fresh actionの発行時は**入れ替える**（writerを`with_retry_attempt`と`with_new_logical_action`に分け、boolean flagで切り替えない）。持ち越すとrun全体で単調に増え、checkpointのsize上限へ向かって伸びる。入れ替えの代償は「前のlogical actionへの遅れた再送がstaleになる」ことだが、その時点で結果は永続化済みでworkflowは次の作業へ進んでいるため、冪等の適用範囲外として扱う
23. **上限を構造として持つ**。`receipts`にschemaの`max_items`（`MAX_SUBMIT_RECEIPTS`）を課し、engineは同じ境界でretryを打ち切る。呼び出し側のretry budgetが大きくても、checkpointが書けなくなる方向へは伸ばさない
24. **未完了actionにreceiptが付いているとき、次のattemptを発行してよいのは`FAILED` + `TRANSIENT`の場合だけ**とする。この不変条件はengineのwriterだけが保つものではなく、v1 -> v2 migrationは`COMPLETED` receiptを持つ未完了actionを作り得る。receiptの内容を見ずに再発行すると**完了済みactionを再実行する**ため、他の組合せは停止する（fail closed）
25. **CHECKPOINTをv2へbumpし、migrationを登録する**。`host_action`の未完了actionを`pending`へ、単一の`submit`を`receipts`配列へ移す。receiptが要る`action_id` / `nonce`は**同じsection内**の値で捏造がなく、ADR-0004 rule 6を満たす**損失のない変換**である（`HOST_ACTION` v1 -> v2と違いchainを張れる）。`host_action`を持たないcheckpointの変換はidentity

### 順序（crash windowで重複を作らないための不変条件）

26. `advance`: **action envelopeの保存 -> checkpointの保存 -> hostへ返却**。envelopeは実体を読み直してhash照合できるようにrun directory内へ置く
27. `submit`: **受理 -> submit receiptとrecord transactionを`同じ`checkpoint更新で保存 -> （後続PR）投稿 -> read-after-write -> transaction消費**。receiptとtransactionを別々に書くと、その間のcrashでnonceだけが消費され投稿対象が失われる。checkpointはatomic replaceの単一fileなので、1回の更新で両方書ける
28. **新規transactionでは`body_hash`を必須にする**（ADR-0014 決定21の実装）。発行した値をC-07の`read_transaction` -> `evaluate_pending`がそのまま読めることをtestで固定し、producerとresumeの契約を片側だけ変えられないようにする

### 復元と拒否

29. `state` sectionからMachineStateを復元できない場合（`BLOCKED`のblock context等、保存していない付随値を要するstate）は**構造化errorで停止する**。既定値で埋めるとC-01の組合せ不変条件が壊れる。blockはC-06のchain検証で毎回再導出する値である（ADR-0011）
30. checkpointの`host_action`を解釈できない場合も「無い」へ丸めない（silent repair禁止）。schema検証を通る値でも意味的に不正なもの（`attempt`が0等）は停止する

## Consequences

- 本PRでAC-C08-05（stale action / 異なるhead・run・action kind / path traversal / symlink / size超過 / hash差分の重複submit）とAC-C08-07（検証済みrecordの同梱）を充足する。AC-C08-01 / 02 / 03 / 04 / 06はadapterとprocess境界を持つ後続PRが扱う
- `AWAIT_USER`搬送路（user-input submitのvariantと転記順序）は次PRで実装する。C-13が所有するのは意味解釈とgate semanticsで、Phase 8が定めるのは搬送路に限る
- C-10 / C-11はportの本実装を差し替えるだけでよい。engineはregistryとschemaを疑わずに動く
- `HOST_FAILURE`はC-02のschema registryへ加わった（30 -> 31 kind）
