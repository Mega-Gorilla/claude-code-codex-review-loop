<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0019: procedure / blockのcheckpoint表現と`HaltRun`の実行

- Status: Accepted
- Date: 2026-08-26

## Context

PR-2c（ADR-0018）で`AWAIT_USER`搬送路が入り、`advance`の3 outcomeは閉じた。しかし**PR-2cが開いた経路の一部は行き止まりだった**。

```
USER_CANCEL を submit -> persist -> UserCancelVerified
-> C-01が CancellingProcedure + HaltRun を返す
-> with_verified_machine_state が読み戻せず EngineStopped("state_not_persistable")
```

同じことが`EXTERNAL_DEPENDENCY`（`APPLY_FINDINGS`のresult variant）とbounded progressの`ProgressBlock`でも起きる。いずれもPhase 8のregistryから到達できる経路で、**checkpointが状態を表現できないために止まっていた**。ADR-0017 決定8がhalt gateと`RECORD_INTEGRITY`までしか広げていなかったためである。

本ADRは残りのvariantを表現できるようにし、C-01が発行する`HaltRun`を実行して`USER_CANCEL -> CANCELLED`を正規のterminal経路として通す。Phase 8で正当に到達できるterminalは`CANCELLED`だけである（`MERGED`へ至る`MergeConfirmed`系eventはC-13の責務で、engineに入口が無い）。

## Decision

### 継続はIDで保存する

1. **`BlockedContinuation`はcommand列ではなくIDで保存する**。`ProgressBlock` / `ExternalDependencyBlock`は「本来の継続」としてcommand列を持つが、commandはpayloadを持つ11種の直和であり、直列化すると他で使わない大きなformatを新設することになる。
2. 実測: ruleが構築する継続は**6つだけ**である。`BlockedContinuation`のdocstringも「registry由来の**有限値**」と定めていた。そこで`BLOCKED_CONTINUATIONS`をC-01の公開registryとし、ruleは定数ではなくこの表を引く（挙動不変のmechanical refactor。外部依存のinline分も表へ寄せた）。
3. **IDは永続値**である。entryを消す・意味を変えるにはversion bumpとmigrationが要る。表に無いIDは**fail closed**で、command列を推測しない。表の網羅（ruleが表外の継続を作らない）はcontract testが固定する。
4. **逆引きが引けない継続は例外にせず保存を拒否する**。writerが壊れたときも、round-trip検証という既存の検出経路へ合流させる（例外がsubmit / persistの構造化outcomeを飛び越えない）。

### checkpointの表現範囲

5. **procedure 4種・block 3種をすべて表現する**（additive追加。version bumpなし。ADR-0004 rule 10）。`state.procedure`へ`target` / `audit`を、`state.block`へ`binding` / `head` / `continuation` / `reason` / `budget` / `counter_snapshot` / `fingerprint` / `evidence`を足した。
6. **kindごとの必須検査はreaderが行う**。必須fieldの組がkindごとに違うためschemaは`kind`以外をoptionalにしており、欠けていれば既定値で埋めず停止する。停止手続きの途中で中断したrunを「手続き中でない」と誤って復元すると、走り続けるprocessを誰も止めない。
7. **到達可能な全非terminal `MachineState`のround-trip一致をtestで固定する**。到達可能性は`MachineState`を構築できるかどうかで判定する（C-01の組合せ不変条件が構築時に検証されるため、構築できない組合せは存在しない）。手で列挙すると不変条件が緩んだときに気づけない。
8. `RecordingIncidentProcedure`は**表現するが、まだ駆動しない**（`incident_executor_missing`で停止する）。`RecordIntegrityIncident`のpayload構築はC-06 / C-07へ委ねるが、**駆動はC-08の責務**である。この手続きへは2経路から入る: MERGINGのoutcome確定（I-46 / I-47）と、**cancel中のintegrity検出**（I-D2で`deferred_integrity`へunion -> C-04）。後者はMERGINGを含まない非terminal全stateで起き、C-08のcancel経路そのものにある。実行はPR-3dが追加する。

### process tree台帳

9. **停止対象の`TreeRef`をcheckpointへ持つ**（`processes` section）。C-03の`TreeRef`は「元のhandleを持たない**別process**がtreeへ到達するためのidentifier」として設計され、永続化を後続Phaseへ委ねていた。`HaltRun`の実行は中断後に別processから再開されるため、ここに無いと停止対象へ到達できない。
10. 書き手は**treeを起動するcomponent**（C-09、PR-3bのheadless adapter）で、C-08は読んで停止し、停止できたものを台帳から外す。listは部分更新せず**全体で置き換える**（追加と削除が同じ経路を通り、「消したつもりが残る」形を作らない）。
11. **停止できたtreeは台帳から外す**。残すとpidが再利用されたときに別treeへ到達し得る（`ProcessGroupRef`の既知limitation）。**止められなかったtreeは残す**（次のresumeが同じrefで再試行する）。
12. kindと必須fieldの対応（`JOB_OBJECT`は`job_name`、`PROCESS_GROUP`は`pgid`）は**readerがfail closed**にする。片方で代用すると停止対象を推測して別treeへ到達し得る。

### `HaltRun`の実行

13. 順序は`load -> 完了eventを決める -> **受理可否の確認** -> tree停止 -> checkpoint保存`。
    - **停止する前にC-01が受理できるかを確かめる**。`transition`は純粋関数（R2）なので副作用なしに判定でき、拒否される状態でtreeを止めると「processは死んだがstateは走行中」という不整合が残る。台帳が読めない場合も同様に、止める前に停止する
    - **保存より先に停止する**。先に保存して停止前に落ちると、stateだけが「停止済み」になり、走り続けるprocessを誰も止めない。停止してから落ちた場合は、resumeがC-01のX系列ruleで`HaltRun`を冪等に再発行し、停止をやり直す（`stop_tree_by_ref`は冪等）
14. **完了eventは手続きから決める**（推測しない）。`CancellingProcedure` -> `CancellationCompleted(attempt_binding)`、`HaltingForBlockProcedure` -> `BlockHaltCompleted(attempt_binding)`の2つだけで、手続きが無ければ停止したことにしない。この2つはC-01が当該手続きの成立する**全state**で受理することをcontract testで固定する（決定13の事前確認が意味を持つ前提）。
15. **停止対象が無い場合も正常完了**である。C-01の横断規則は「手続き中の失敗・明示resumeは停止commandの冪等再発行のみ」を前提にしており、台帳が空でも手続きは完了へ進める。
16. **1つでも止められなければstateを進めない**。`RunFailed`をC-01へ入力し、X系列ruleが`HaltRun`を再発行する（`HaltFailed`は「次のresumeでやり直す」を意味する）。
17. **緊急停止（Ctrl+C）はPR-3aで扱わない**。C-01は緊急停止を`NormalProcedure`のまま完了させる（C-05 rule）ため、停止に失敗したときの`RunFailed`はF-01で`FAILED(recovery_to)`へ進み、**`HaltRun`を再発行しない**（`command_names=()`）。つまり停止意図がcheckpointへ残らず、次のresumeが再停止しない。durableに表現する方法（C-01へprocedureを足すか、C-08側に停止要求の台帳を持つか）はsignal handlerの設置と同じPRで決める必要があるため、entry pointを持つ**PR-3b**へ送る。

### `advance`の分岐

18. **procedure分岐はpending recordより前**に置く。cancelの経路2はstale pendingを監査参照として保持するため（C-01のC-02 rule）、順序を逆にすると監査参照を永続化しようとする。
19. `BLOCKED`は汎用の`no_awaiting`ではなく**block情報を添えた`Blocked`**を返す。解消はC-08の外から来る（limit引き上げ・ユーザー介入・integrity復旧）ため、runが壊れたのかblockで待っているのかをhostが区別できる必要がある。
20. `RecordingIncidentProcedure`でpendingがあれば`PersistRequired`（incident recordの投稿はC-08が駆動する）。pendingが無い場合は`incident_executor_missing`で止める — 実行はC-08の責務だが、まだ実装が無い（PR-3d）。

## Consequences

- PR-2cが開いた`USER_CANCEL`経路の行き止まりが解消し、Phase 8で`CANCELLED`まで到達できるようになった
- `ProgressBlock` / `ExternalDependencyBlock`へ入る既存の経路（bounded progress、外部依存record）も`state_not_persistable`で止まらなくなった
- **AC-C08-06（別processからのresume）の前提**が揃った。停止手続きの途中で中断したrunを復元できる
- **`RecordIntegrityIncident`の実行は未割当のまま残る**。cancel中にintegrity violationが検出されると`RecordingIncidentProcedure`へ入り、そこで停止する（`USER_CANCEL`がPR-3a前に止まっていたのと同じ形）。**PR-3dが実行を追加する**まで、この経路は完走しない。代表cancelシナリオが完走するのは`deferred_integrity`が空の場合である
- `BLOCK_INTERVENTION`の搬送路は**PR-3c**が扱う。この「待ち」は`Awaiting`ではなくblock自身であり、request identityが`awaiting` + `since_seq`ではなくblock bindingになるため、`USER_REQUEST` / `USER_SUBMIT`へ別のdiscriminatorを足すschema変更を伴う
- adapterとprocess境界（AC-C08-01 / 02 / 03 / 04）と、**緊急停止のdurableな表現 + signal handler**は**PR-3b**が扱う
