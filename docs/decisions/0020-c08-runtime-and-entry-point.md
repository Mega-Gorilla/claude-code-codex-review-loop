<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0020: runtimeとprocess entry point

- Status: Accepted
- Date: 2026-08-26

## Context

PR-3a（ADR-0019）でPhase 8のengineは`advance` / `submit` / `persist` / `halt`が揃い、`USER_CANCEL -> CANCELLED`まで通った。しかし**engineを呼ぶ側が存在しなかった**。

- `advance`が`PersistRequired` / `HaltRequired`を返しても、それを実行して次へ進める駆動codeがどこにも無い（testが手で`persist` / `halt`を呼んでいた）
- AC-C08-06（別processからのresume）は、同一process内でengine関数を呼び直すだけでは証明にならない。**process境界を跨ぐentry point**が要る
- AC-C08-01 / 02 / 03も、adapterが無いため未充足だった

本ADRはその駆動と境界を決める。CLI本体（`cc-review`）はC-12の正本なので先取りせず、安定したmodule entry pointだけを置く（Issue #13の案B）。

## Decision

### 分割: spawn境界でPR-3bを2本に分ける

1. **PR-3b1（本ADR）はprocessを一切起動しない**。runtimeとstep driver、`HostPort` protocol、module entry point、別process resume、CI artifact収集までを含む。
2. headless adapter（spawner）、緊急停止のdurableな表現、signal handler、active / headlessの同値性（AC-C08-04 / MVP-06）は**PR-3b2**へ送る。
3. 理由: **spawnerをsignal handlerなしで出すと、Ctrl+Cでprocess treeがorphanになる**。PR-3aで緊急停止経路を外したのと同じ基準（「durable化するPRまで公開しない」。ADR-0019 決定17）を適用すると、spawnerと停止機構は同じPRに入るか両方入らないかのどちらかである。PR-3b1は後者を選び、**起動しないので停止の穴も作らない**。

### 駆動は1つだけ（P-002）

4. **step driverは`runtime/session.py`の`step`1つ**である。P-002は「entry pointごとにround orchestrationを実装すること」を禁じるため、active経路もheadless経路も同じ`step`を通す。
5. `step`は`advance`を呼び、**engine側の作業（`PersistRequired` -> `persist` / `HaltRequired` -> `halt`）をその場でこなして再度`advance`**し、host側の作業（`HostActionIssued` / `AwaitUser`）か終端（`Terminal` / `Blocked` / `EngineStopped`）に達したら返す。`persist` / `halt`は「`advance`が返した作業の実行」であって独立した制御経路ではない（ADR-0017 / ADR-0019）という位置づけが、これでcodeの形になる。
6. その結果、**entry pointが呼ぶengine経路は`step`と`submit`の2つだけ**になる（AC-C08-03）。ASTのcontract testで固定する。
7. **1 stepあたりのengine側作業に回数上限を置く**（`MAX_ENGINE_WORK`）。C-01が同じ作業を返し続けるのは不変条件の破れであり、推測して回し続けずに停止する。上限は**ちょうどその回数まで**を許し、次の副作用を起こす**前に**停止する（超過して1回多く実行しない）。
8. **停止に失敗したら同じstepで回さない**。`HaltFailed`はC-01が停止commandを再発行することを意味するが、同じ理由で失敗し続けるため、呼び出し側へ返して次のresumeへ委ねる。
8-a. **submitのchain gateは冪等判定より後に置く**。受理済みsubmitの再送は「以前と同じ結果」でなければならず（ADR-0015）、後からchainが壊れたかどうかで結果が変わってはならない。exact replayとduplicate mismatchの判定を先に済ませ、**未受理のsubmitを消費する直前**にgateを置く。stale / binding不一致の診断も`chain_violation`で潰さない。
9. **portの例外を呼び出し側へ飛ばさない**。`PortUnavailableError`は担当componentを名指しした`EngineStopped`へ写す。例外が飛び越えると「進退は構造化outcomeで決まる」という前提が壊れる。

### `HostPort`: engineから見たhostの同一interface

10. hostは「1つの作業を実行してsubmit envelopeを返すもの」であり、それがactive sessionでもheadless subprocessでも同じである。この同一性が**active / headlessの同値性（AC-C08-04）を実装の一致ではなく構造で担保する**。
11. `drive`が`step` -> `host.execute` -> `submit`を繰り返す。**round orchestrationはここ1箇所**で、`max_rounds`は呼び出し側が決める（engineは既定値を持たない = C-12の領域を侵さない）。
12. **主経路のactive hostはこのprotocolを実装しない**。Claude Code sessionは我々のprocessの外にあり、`HOST_ACTION`を返した時点で制御が一度戻るためである（AC-C08-02: subprocess化もキー入力注入もしない）。entry pointの`advance`は`step`を1回だけ実行して結果を表示し、終了する。
13. `drive`を通るのはheadless経路（PR-3b2）と、同一sessionで複数roundを確かめるtestのfake active hostである。**同じ`drive`を両者が通る**。

### session config: run directory内に置く

14. engineは既定値を持たないため、entry pointは全設定を受け取る必要がある。20項目超をCLI引数にすると扱えないので、**run directory内の`session.json`**へ置く。
15. **全fieldを必須にする**。既定値の補完はC-12の領域であり、C-08が「無い設定を埋める」経路を作らない。
16. checkpointと同じrun directoryに置くため、**別processが同じportを再構成できる**。cross-process resumeの前提そのものである。
17. 読み書きはC-06の`write_private_text`と権限検証を通す。書き手は現時点ではtest（将来はC-12）。
18. **durationはms整数で持つ**。JSONの浮動小数で秒を表すと、丸めの差がtimeoutの意味を変える。秒への換算は読み出し側の`SessionConfig`が1箇所で行う。

### port: 導出できるものは製品として実装する

19. `workflow/ports.py`が定めた6 portのうち**4つは今日導出できる**。基準は既存の「C-10 / C-11のdomain形状を先取りしない」ことで、**既存componentの出力と既存registryの宣言だけから決まるもの**に限る。

| port | 導出元 | 実装 |
| --- | --- | --- |
| `RecordSourcePort` | C-05の取得 + C-06の`read_chain_checkpoint` -> `probe_known_records` -> `verify_record_chain` | `ChainRecords` |
| `ProcessStopPort` | C-03の`stop_tree_by_ref` | `TreeStopper` |
| `EvidencePort` | registryの`evidence_kinds` × 検証済みchain（DOD-02の選択規則そのもの） | `ChainEvidence` |
| `RecordEventPort` | registryの`build_event`（record kindとeventの1対1対応） | `RegistryRecordEvents` |
| `RecordBodyPort` | 転記recordの本文選択のみ | `UserInputBody` |
| `ActionPayloadPort` | — | 未実装（fail closed） |

20. `EvidencePort`は**対象headかつregistryが宣言したkind**をseq昇順で返す。engineの`_evidence_of`が同じ条件を検査するため、この選択がそのまま契約になる。
20-a. **chainのviolationは`advance`が`_chain_gate`で止める**。`verify_record_chain`はviolationがあってもrecordsを返す契約（差分表示のため）なので、**consumerが`is_intact`を確かめる**必要がある。PR-3b1以前は`_await_user`と`persist`だけが確かめており、`HOST_ACTION`の発行経路が素通しだった。gateは`advance`が1箇所で行い、検証済みchainを`_await_user`へ渡す（従来の二重fetchも解消する）。
20-b. **`ChainEvidence`も`is_intact`を確かめる**。gateからportまでの間にchainが壊れる窓があり、両方がfail closedなら**どちらかが観測した時点で次のturnは起きない**。`ChainNotIntactError`は`step`がengineのgateと同じ`chain_violation`へ写す。gateとportで別々にchainを読むためfetchは2回になるが、**正当性を優先する**（request scopedなcacheはC-12の設定解決と併せて別途検討する）。
20-c. **checkpointのchain部分とprobe結果を渡す**。渡さないと、取得窓に現れなかった既知recordを「元から無かった」と区別できず、削除と巻き戻しを検出できない（AC-C06-09）。なお`conversation` sectionの**書き手はまだ存在しない**（C-06 / C-07の領域）ため、この検出は書き手が入った時点で自動的に有効になる配線であり、現時点では常にfresh扱いになる。regression testはcheckpointを直接seedして検出を確かめている。
21. `RecordEventPort`は`extra_event_inputs`が空のkindだけを扱う。`ProgressReport`（progress判定）や`head`はC-10 / C-11が決める値で、ここで作ると**判定を偽装する**ことになる。
22. `RecordBodyPort`は**user-input recordに限る**。転記recordの本文はユーザーが書いた文そのもので、C-08は選ぶだけで文面を作らない（`BODY_VALUE_FIELDS`がkindごとのfieldを宣言する）。宣言の無いkind——agent recordと、自由記述を持たない`MERGE_APPROVAL`——は表現を**構成**する必要があり、C-10 / C-11 / C-13の領域である。
23. `ActionPayloadPort`（`round` / `finding_ids`等）はfinding ledger（C-10）とdecision（C-11）由来なので**名指しでfail closed**にする。「無いものを既定値で埋める」経路を作らない。
24. この非対称の帰結: **cancel完走シナリオは実portだけでentry point経由に通る**。`HOST_ACTION`を含むroundだけがaction payloadとagent record本文のfakeを要し、testが`step`へ注入する。**fakeの範囲がそのまま「まだ実装が無い範囲」**である。

### entry point

25. `python -m claude_code_codex_review_loop.runtime <advance|submit>`。持つのは**P-002の3責務だけ**である: 引数解析、session boundaryの受け渡し、表示。
26. **終了codeは構造化outcomeから決める**（P-003。出力文字列の部分一致で分類しない）。`0` = 進んだ、`2` = 引数の誤り、`3` = 停止。
26-a. **submit envelopeの読込失敗も構造化結果にする**。`OSError`でtracebackになると、呼び出し側が終了codeと標準出力だけで進退を決められなくなり、process境界の契約が崩れる。読む前にsizeも検査する（envelopeはbinding echoとhashだけで、結果本体は`result_hash`が指すfileにあり、その上限は`max_result_bytes`がsubmit側で検査する）。
27. **出力の非ASCIIはJSONのescapeで閉じる**。この出力は人向けの表示ではなく呼び出し側が解釈する構造化結果であり、stdoutのencodingはhostのlocale（Windowsのconsole code page等）で決まる。日本語のdetailをそのまま書くとlocale次第で読めなくなる。
28. **entry pointはloopを持たない**（contract testで固定）。round orchestrationは`drive`に1つだけである。

### CI artifact

29. **失敗したjobのcheckpointとenvelopeだけを収集する**。何が起きたかはC-08のstateに現れるが、CIのlogには残らない。
30. **収集対象はfile名まで指定して限定する**（Issue #13: 未redact入力を含めない）。`checkpoint.json`の`transaction.body`はredact済みのrender出力（ADR-0015）、`action.json` / `request.json`はbinding・hash・comment ID・action payloadで自由記述を持たない。**`result.json`は含めない**——hostが返した実行結果とユーザーの入力そのものが入り、redactを通っていない。session config（`gh_env`を持つ）とfake GitHubのstateも外す。
30-a. **Issue #13が挙げる4種のうち、PR-3b1が収集するのは2種である**。残り2種は**まだproducerが存在しない**ため、収集範囲を狭めたのではなく対象物が無い。

| 対象 | PR-3b1 | 備考 |
| --- | --- | --- |
| checkpoint | 収集する | `checkpoint.json` |
| envelope | 収集する | `action.json` / `request.json` |
| canonical record | **未収集** | 正本はGitHub上のcomment。localの`artifact_records`（C-07が定義）へ書くcomponentがまだ無い。`checkpoint.transaction`は永続化で消費されるため常設の代替にならない |
| redact済みlog | **未収集** | logging機構自体が未実装 |

30-b. **残り2種の収集は後続PRが扱う**。producerと同じPRで収集経路を入れる（対象物が無いまま収集stepだけ増やしても、`if-no-files-found: ignore`で黙って0 fileになる）。それまでPR-3bのartifact受入は**未完**として扱う。

> **訂正（ADR-0021 決定26）**: 当初「PR-3b2」と書いたが、3-bをさらに分割したため、redact済みlogの収集は**PR-3b3**（headless adapterのstdout / stderrがそのproducer）が扱う。canonical recordのlocal artifactはC-09以降のままである。
30-c. **file名の対応をcontract testで固定する**（`tests/test_c08_artifact_contract.py`）。収集file名を製品定数（`CHECKPOINT_FILE_NAME` / 両`ENVELOPE_FILE`）と突き合わせ、`*.json`のようなwildcardへ広がった瞬間にfailさせる。実行するのはfake ghだけなので実credentialは存在しないが、**収集範囲を絞ることを既定にしておかないと実transportを使うPhaseで漏れる**。
31. artifact名へOSを含め、Ubuntu / Windowsの失敗を区別する。tmp directoryは**workspace外**へ置く（`tests/test_c06_isolation.py`がreviewer用git呼び出しのconfig originを検査しており、repository配下だとrepository localの`.git/config`が混ざる）。`.`始まりの名前も使わない（`include-hidden-files`が既定falseで、hidden pathは黙って外れる）。

## Consequences

- **AC-C08-01 / 02 / 03 / 06が充足した**。AC-C08-06はIssue #13が挙げる3つの中断点——pending user request / pending `HOST_ACTION` / 停止procedureの途中——すべてを別processからresumeして固定した
- **`HOST_ACTION`と停止procedureのcross-process testはtest所有のdriver processを使う**。`python -m ...runtime`は`default_ports`を使うため、まだ実装の無い2 port（action payloadとagent recordの本文）を要する経路を通せない。driverはその2つと停止portだけをfakeにして**同じ製品関数**（`step` / `submit_result`）を呼ぶ。resume機構そのものは製品codeである
- AC-C08-02は「fakeのcounterが0」だけでなく、`runtime` packageがprocess起動もキー入力注入も**構造的に持たない**ことをAST contractで固定した。PR-3b2がspawnerを足すときはこの契約を明示的に更新することになる
- **AC-C08-04（active / headlessの同値性）は未充足**である。`HostPort`と`drive`という構造は用意したが、headless側の実装はPR-3b2が入れる
- **緊急停止のdurableな表現とsignal handlerも未着手**である（ADR-0019 決定17がPR-3bへ送った項目のうち、PR-3b1は扱わない）
- `session.json`の書き手は当面testだけである。C-12が実CLIを持つときに書き手を引き取る。全field必須なので、C-12がどの値をどこから解決するかはそのPhaseの設計として明示的に決まる
- **未実装portの範囲がtestのfakeとして可視**になった。`ActionPayloadPort`とagent recordの本文の2つだけがfakeで、それ以外はentry pointから実物が走る
- CIの`--basetemp`が**workspace外の固定位置**（`${{ runner.temp }}/pytest-tmp`）へ移った。ローカル実行の既定は変えていない
- **CI artifactの収集は2/4種にとどまる**（決定30-a）。canonical recordとredact済みlogはproducerが未実装で、収集は後続PRが担当する（ADR-0021 決定26でPR-3b3へ確定）。PR-3bのartifact受入は未完である
