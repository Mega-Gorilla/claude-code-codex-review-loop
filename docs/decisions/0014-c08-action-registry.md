<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0014: C-08 action registryとprotocol schemaの確定

- Status: Accepted
- Date: 2026-08-24

## Context

Phase 8（C-08）はstep engine（`advance`で次の`HOST_ACTION`を返し、active hostが実行して`submit`で結果を返す制御反転）を実装する。engineを書く前に、**語彙と契約が3箇所で食い違ったまま**だった。

1. **action一覧の不一致**（実測）: `domain.HostAction` **6** / `schema.HOST_ACTION_KINDS` **12** / implementation plan Section 2.3 **9**（重複3で 6 ∪ 9 = 12）。`schema/action.py`のdocstringも「暫定。Phase 8で確定する」のまま
2. **actionごとのpayload / resultが未定**。`HOST_ACTION.payload`は任意objectで、**result pathのfieldが無い**（plan L295は「result pathはControllerがrun directory内に払い出す」と要求）
3. **actionとC-01 eventの対応が無い**。submit結果をeventへ組み立てるのはC-08の責務（Phase 1計画の責務表）
4. checkpointに**未完了actionを保持するfieldが無い**（AC-C08-06の別process resumeに要る）

## Decision

### action registry

1. **registryはC-01の`HostAction`（6値）に限る**。C-01は`AWAITING_COMMANDS`で`Awaiting.HOST_*`と`RequestHostAction`を1対1に対応させており（`domain/_ruledefs.py`）、**engineが受け取り得るactionはこの6つだけ**である。Section 2.3の残りを実装しても到達不能なcodeになる
2. **これは正本の変更ではなく、正本が委ねた確定である**。Section 2.3は「初期案。Phase 8で確定する」と明記している。残り6項目の帰着先を記録する:
    - `ASK_CLARIFICATION` / `ANSWER_CLARIFICATION`: clarificationはC-01が`RequestCodexReview(CLARIFICATION)`で表現する。host側のactionが必要になればC-11が追加する
    - `DRAFT_FOLLOWUP_CANDIDATES`: follow-upはPhase 11（C-11）
    - `STRUCTURE_USER_INTENT`: user intentの構造化はC-08の**受信側責務**（PowerShell / Skill入力をintentへ写す）であり、engineが発行するactionではない
    - `RUN_LOCAL_TESTS`: `APPLY_FINDINGS`が「修正・test・commit・push」を含む（Section 2.3の定義そのもの）
    - `IMPLEMENT_ISSUE`: Issue modeはPhase 15（C-14）
3. **C-01へactionが追加された時点でregistryへ入る**。registryとC-01の全単射（`HostAction` ↔ `Awaiting.HOST_*` ↔ `AWAITING_COMMANDS` ↔ schemaのenum）はcontract testで常設検証し、片側だけの追加をfailにする
4. **結果payloadは既存のrecord schemaを再利用する**。host actionの成果物は、そのままGitHubへ投稿するrecordのpayloadだからである（新しいresult schemaを作らない）。`result_schema`と`record_kind`が一致することもtestで固定する
5. **1つのactionは複数の正規なresult variantを持ち得る**。C-01は同じawaitingに対して複数のrecord kindの`RecordProduced`を受理する。実測では`HOST_APPLY_FINDINGS`が5種（`FIX_RESULT` / `CLARIFICATION_QUESTION` / `DECISION_REQUEST` / `EXTERNAL_DEPENDENCY` / `PERMISSION_BLOCK`）で、hostが修正中に質問・判断依頼・外部依存・tool permission停止へ到達するのは**既存state machineが意図した正常経路**である。単一の結果へ固定すると4経路が到達不能になる
6. **registryの結果集合はC-01の`PRODUCED_RULES`が許可するkind集合と完全一致させる**。contract testで両者を突き合わせ、片側だけの変更をfailにする
7. **hostが選んだvariantはsubmitへbindする**（`SUBMIT.result_kind`）。engineは`result_kind`からvariantを引き、結果schemaでの検証・投稿するrecord・組み立てるeventを決定論的に決める。当該actionで許可されない種別は`variant_for`がNoneを返し、engineが拒否する
8. **record kindとC-01 eventは1対1**とする。8つのvariantすべて`_VerifiedEvent.EXPECTED_KIND`と一致する単一eventへ写り、値によるdiscrimination（`REVIEW_RESULT`の2値、`CLARIFICATION_ANSWER`の5値等）を含まない。多値のものはCodex由来recordであり、**record -> eventの対応表を持つC-10 / C-11の領域を侵さない**
9. **eventが`evidence`以外の入力を要する場合は`extra_event_inputs`で宣言する**。`FixResultVerified` / `ClarificationQuestionVerified`は`ProgressReport`、`ExternalDependencyVerified`は`head`を要するが、これらは**C-10 / C-11由来の値でC-08は作らない**。宣言とeventのdataclass fieldの一致をtestで固定する

### `HOST_ACTION` / `SUBMIT`のv2

10. **`action_kind`の値域をC-01の`HostAction`から導出する**（`HOST_ACTION_KINDS`）。schema側に値を書き写さないため、drift自体が起こらない
11. **`result_path`を必須にする**。Controllerがrun directory内へ払い出すpathで、呼び出し側から任意pathを受理しない（plan L295）。canonical path・containment・所有者権限・size limitの検証はPR-2のengineが行う
12. **submitの結果fieldを排他にする**。`COMPLETED`は`result_kind`必須・`error_category`禁止、`FAILED`はその逆とする。どちらでもない組み合わせ（成功なのに失敗分類、失敗なのに結果種別）を受理すると、engineがsubmitを「どのvariantか」「どの失敗分類か」へ決定論的に写せない
13. **失敗分類はP-003の`ErrorCategory`をそのまま使う**（`TRANSIENT` / `NOT_FOUND` / `AUTH` / `PERMANENT`）。自由文字列だと`TRANSIET`のようなtypoや未知分類を受理し、構造化分類の境界が成立しない。component専用のvocabularyを増やさない
14. **actionごとの入力payloadをschemaとして宣言する**（`HOST_ACTION_PAYLOADS`）。cross-field ruleで`action_kind`に対応するspecを適用し、検証は`validate.structural_errors`を再利用する（独自validatorを書かない）。payloadは**そのactionを実行するために必要な識別子だけ**を持ち、成果物の形はresult schemaが定める
15. **enumの縮小と必須fieldの追加は非互換変更なのでversionをbumpする**（ADR-0004 rule 2）。`SUBMIT`は同じenumを共有するため同時にv2へ揃える
16. **v1 -> v2のmigrationは登録しない**。`result_path`は捏造できず（rule 6の「意味的fieldの捏造禁止」）、損失のない変換が存在しない。v1入力は`migration_unavailable`の構造化error（rule 8）になる。v1 envelopeを生成したcodeは存在しないため実害はない
17. **v1のspecは残す**。過去versionを未知扱いにすると`unknown_version`になり、「既知だが持ち上げられない」ことを表現できない。意図的なchainの穴は`INCOMPLETE_MIGRATION_CHAINS`として理由つきでtestへ登録し、将来のsilentな穴を防ぐ

### checkpointの`host_action` section

18. **未完了actionをcheckpointへ保存する**。ADR-0004 rule 10のadditive追加でversionは上げない
19. **binding 8項目だけでは足りない**。`action_id` / `action_kind` / `nonce` / `expected_head_sha` / `result_path`が同じでも、`payload`や`verified_records`が違う2つの有効なactionが存在し得る（実測で確認）。そのため**envelope全体のcanonical hash**（`envelope_hash`）と、実体を読み直すための`envelope_path`（run directory相対）を保存する。resumeはfileを読み直してhashを照合し、一致した場合だけ同じactionを再提示する
20. **受理済みsubmitも全体のcanonical hashで識別する**（`submit_hash`）。`outcome`と`result_hash`だけでは、同じresult hashで`error_category`だけが違う失敗が同じ値へ潰れ、AC-C08-05の「同一内容の再送だけ冪等、異なる重複は停止」を判定できない。`result_kind`と`error_category`はeventの組み立てと診断のために併せて保持する

### PR-2以降が守るproducer規則（ここで固定する）

21. **新規transactionでは`body_hash`を必須にする**。schema上optionalなのは既存fieldの制約を強化しないため（ADR-0013 決定9）であり、producerが省略してよいという意味ではない。marker付加後の完成本文hashは投稿前に計算できる。順序は render -> projection -> binding導出 -> marker作成 -> marker付加 -> 完成本文hash -> transaction保存 -> 投稿 -> read-after-write -> transaction消費
22. **resumeは同じactionを再提示する**。action発行後に中断した場合、resumeは同じ`action_id` / `nonce` / `result_path`を返し、**新しいactionを生成しない**。未完了actionがある間は`advance`が新規発行しない

## Consequences

- PR-2（step engine）はregistryとschemaを疑わずに書ける。`advance`は`RequestHostAction`をregistry経由で`HOST_ACTION` envelopeへ写し、`submit`は`result_kind`からvariantを引いて結果を検証し、そのvariantのrecordを組み立てる
- envelope / submitのcanonical hashはC-02の`canonical_payload_hash`を使う（新しいhash規約を作らない）
- C-10 / C-11がactionを追加するときは、**C-01のHostActionとAwaitingを先に追加**する。registryのcontract testが片側だけの追加をfailにする
- `ProgressReport`を要する`APPLY_FINDINGS`のevent構築は、C-10がprogress判定を持ち込む形になる（C-08はevidenceまでを用意する）
- 本PRはschemaとregistryのみで、AC-C08-01〜07の充足はPR-2 / PR-3が行う
