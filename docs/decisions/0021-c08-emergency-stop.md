<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0021: 緊急停止のdurableな表現とsignal handler

- Status: Accepted
- Date: 2026-08-26

## Context

ADR-0019 決定17は、緊急停止（Ctrl+C）のdurableな表現とsignal handlerの設置をPR-3bへ送っていた。PR-3b1（#45）はprocessを起動しない範囲に留めたため、この2つは未着手のまま残っていた。

穴は2つある。

1. **signal handlerが無い**。Ctrl+Cを受けても停止経路へ入らず、`KeyboardInterrupt`がtracebackになる。PR-3b1が`OSError`について直した「process境界は構造化結果で返す」がsignalでは成立していない
2. **停止に失敗すると停止意図が消える**。C-01は緊急停止を`NormalProcedure`のまま完了させる（C-05 rule: `CancellationCompleted(emergency_evidence=...)` -> `CANCELLED`）。停止失敗時の`RunFailed`はF-01で`FAILED(recovery_to)`へ進み、`command_names=()`なので**`HaltRun`を再発行しない**。checkpointに「止めるつもりだった」が残らず、次のresumeが再停止しない

PR-3b1で入れた`processes`台帳（停止対象の`TreeRef`）は1を解決しない。台帳は「treeが在る」ことしか言わず、「止めるべき」とは言わないためである。

## Decision

### 分割: 3-bをさらに2本へ分ける

1. **PR-3b2（本ADR）は緊急停止の機構だけを入れる。processは起動しない。** headless adapter（spawner）、`processes`台帳の書き手、active / headlessの同値性（AC-C08-04 / MVP-06）は**PR-3b3**へ送る。
2. 理由: 3-b1を分けた基準（「spawnerと停止機構は同じPRに入るか、両方入らないか」。ADR-0020 決定3）をそのまま適用すると、**停止機構が先**でなければならない。3-b2で停止機構をdurableにしておけば、3-b3がspawnerを足す時点で穴は既に塞がっている。**逆順だけが禁じられる**。
3. Issue #13の受入項目「停止失敗 -> 別process resume -> 再停止」は3-b2単体で満たせる（PR-3aのhalt testと同じく、台帳をseedしfake stop portで失敗させる）。

### C-01へprocedureを足さず、C-08側の台帳で表す

4. ADR-0019 決定17が残した2択のうち**後者（C-08側の台帳）**を選ぶ。
   - C-05は「緊急停止は**手続きを持たない**」ことを前提に、`attempt_binding`ではなく`emergency_evidence`で設計されている（`_derive_binding`が`emergency_evidence is not None`だけを見る）。procedureを足すとC-05が到達不能になるか、同じ意味の経路が2つになる
   - 停止意図はGitHubへ載る事実ではなく**C-08自身の作業台帳**である。`processes`（停止対象）と同じ層に置くのが素直である
   - C-01（normativeなstate machine）を触らずに済む
5. `stop_request` sectionをcheckpointへadditive追加する（version bumpなし。ADR-0004 rule 10）。持つのは`requested_at` / `evidence` / `source_state`の3つ。
6. **kindごとの必須検査はreaderが行う**。schemaはsectionごとoptionalなので全fieldがoptionalになる。欠けたfieldを既定値で埋めると、停止意図の**内容**を推測することになる。
7. **`evidence`は要求時に一度だけ導出して保存し、resumeはそのまま再生する**。再導出すると値がぶれ、同じ要求が別のeventになる。導出はrun / repository / 番号 / 要求時刻からのcanonical digest（`intent_key`と同じ、sorted keysのcompact JSON）。C-01は値の**存在**しか見ないので、正当性はC-08が担保する。

### 順序: 記録が停止より先

8. **要求の記録を停止より先に行う**。`halt`は「手続きが既にcheckpointへ在る」ことを前提にできるので停止 -> 保存でよいが、緊急停止には先行するdurable markerが無く、書く前に落ちると停止意図が消える。
9. 停止の実行そのものは`halt`と同じ順序原則（**受理可否の確認 -> tree停止 -> checkpoint保存**）に従う。`transition`は純粋関数なので、treeを止める前に受理可否を確かめられる。
10. **既に要求が在れば上書きしない**。`requested_at`と`evidence`が変わると同じ要求が別のeventになる。

### 停止に失敗しても`RunFailed`を入力しない

11. F-01で`FAILED(recovery_to)`へ進むと、**まさに残したい停止意図が消える**。stateは変えず要求を残し、構造化結果（`EmergencyStopFailed`）で返す。次のresumeが同じ要求から再停止する（`stop_tree_by_ref`は冪等）。
12. 失敗時は**保存もしない**。残すべき値が現在のcheckpointと同じであり、書く理由が無い。

### 手続き中と終端ではC-01へeventを入力しない

13. `CancellingProcedure`中の`emergency_evidence`はbinding不一致として拒否される（C-01のX3。`test_c01_cancellation.py`が固定済み）。終端stateはC-05の対象外である。
14. いずれの場合も**treeだけ止めて要求を消す**。stateは既存経路（procedureの`HaltRun` / terminal）へ委ねる。走っているtreeを止める必要は状態に依らないが、状態を進める権利は既存経路が持つ。
15. そのため`advance`は`EmergencyStopRequired`を**terminal判定より先**に返す。後回しにすると、停止意図を持ったまま素通りする。

### signal handlerは「flagを立てるだけ」

16. handlerの中では**flagを立てるだけ**にする。signal handlerは任意のbytecode境界で走るため、そこでcheckpointを書いたりprocessを止めたりすると、書きかけのfileやhandleを残し得る。
17. **設置するのはentry pointだけ**である。signal dispositionはprocess全体の状態で、library codeが勝手に変えてよいものではない。設置は文脈managerにして退出時に必ず元へ戻す。C-03は「signal handlerの設置はC-08が担う」と定めており、本moduleがその実装である。
18. 受け取るsignalはPOSIXが`SIGINT` / `SIGTERM`、Windowsが`SIGINT` / `SIGBREAK`。Windowsは`SIGTERM`をconsoleから配送できず、C-03の`job_object`が停止要求に`CTRL_BREAK_EVENT`を使うのと対の関係になる。
19. 設置できないsignal（platformに無い、main thread以外）は**黙って飛ばす**。signalを受け取れないことは停止機構の不在ではなく、台帳経由の停止経路は動く。

### 2回目のCtrl+Cは即時forceへ昇格する（AC-C03-02）

19-a. **2回目のsignalは`KeyboardInterrupt`として送出する**。1回目をflagだけにしたのは安全のためだが、それを2回目にも適用すると**AC-C03-02（1回目でgraceful、grace待機中の2回目で即時force）が成立しなくなる**。grace待機はC-03の停止primitiveの中にあり、flagでは中断できない。ADR-0005はこの中断を`KeyboardInterrupt`で行うことを前提に設計されており（Consequences:「grace待機はKeyboardInterruptで中断可能であり、その後の`force_stop`呼び出し（C-08が行う）は安全である」）、昇格のwiringはC-08の責務と定めている（同 決定6）。
19-b. **2回目で例外にするのは新しいriskではない**。本PR以前はすべてのCtrl+Cが`KeyboardInterrupt`だった。1回目が構造化経路になったぶん安全側へ動いており、2回目は「待たずに殺せ」という要求そのものである。checkpointの書き込みはatomic replaceなので、中断されても既存のcheckpointは壊れない。
19-c. **昇格はC-03のAPIを増やさずに行う**。`stop_tree_by_ref(ref, 0.0)`は「graceful要求 -> 待たずにforce」になる（両backendの`_drain_*`がgrace 0で即座にfalseを返す）。force専用のby-ref APIを足す必要は無い。
19-d. 捕捉と再実行は`workflow.halt`の`stop_trees`が引き受ける。**停止を始める前にforce要求が在れば最初からgrace 0**で呼び、**grace待機中に届いた場合は`KeyboardInterrupt`を捕まえてgrace 0でやり直す**（`stop_tree_by_ref`は冪等）。どちらの順序で届いても同じ結果になる。
19-e. **force要求を伴わない中断は握り潰さない**。別の理由の`KeyboardInterrupt`を停止要求へ読み替えると、中断の意味を偽装することになる。
19-f. engineはsignalの受け取り方を知らないので、必要な事実だけを`StopEscalation` port（`force_requested`のみ）で受け取る。`ProcessStopPort`と同じ層の定義である。
19-g. entry pointは`KeyboardInterrupt`を**最後の網**として構造化結果へ写す。tracebackで落ちるとprocess境界の契約が崩れる。
19-h. **2回目が`stop_trees`の内側に届くとは限らない**。要求を保存する前・保存中・台帳の読込中・`drive`のhost作業中にも届く。最外層で報告するだけでは**controllerが終了してもtreeが残り**、保存前なら次のresumeが再停止する根拠すら無い（AC-C03-01 / 02に反する）。そこで**`step`が捕捉して停止をやり切る**（`_forced_stop`）: 要求を（未保存なら）durableにしてから、`grace = 0`で台帳のtreeを止める。
19-i. **forced pathは`advance`を通さない**。要求が在るときの`advance`は`EmergencyStopRequired`を返すだけだが、その手前でchain gate（GitHub取得）を通る場合がある。forceは「待たずに殺せ」という要求なので、local I/Oだけで完結する2手——要求の保存とtreeの停止——へ絞る。
19-j. **最後の網は`StopSignal`を見て分類する**。handler設置前や`submit`中に届く通常の（1回目の）`KeyboardInterrupt`まで`forced_stop`と報告すると、停止の昇格が起きたように読める。force要求が無ければ`interrupted`とする。

### signal経路とresume経路を台帳で合流させる

20. **signalは要求を作るだけ**で、実行は必ず`advance` -> `EmergencyStopRequired` -> `emergency_stop`の1経路を通る。signal経路とcross-process resume経路が同じcodeを通り、片方だけ壊れる形を作らない。
21. flagを見る安全点は`step`のengine作業の境目と、`drive`の各roundの前後である。`host.execute`は最も長い区間で、その最中のsignalは**戻り直後**が最初の安全点になる。
22. **signalから要求への変換は1回だけ**行う（`StopSignal.recorded`）。flagは立ったままになるので、毎回変換すると停止を完了した直後にまた要求を作り、要求と完了を交互に繰り返す。書いた時点で持ち主は台帳へ移る。
23. host作業中にsignalを受けた場合、**未submitの結果は捨てる**。hostへ出したactionはcheckpointに未完了として残り、resumeが同じactionを再提示する（ADR-0014 決定22）。結果だけ先に受理すると、停止要求を挟んだ状態遷移がsignalの有無で変わる。
24. entry pointが増やす制御経路は無い（`step` / `submit`のまま。AC-C08-03）。`step`がsignal objectを受け取るだけである。
25. `EmergencyStopRequired`も1 stepあたりのengine側作業の上限に数える。上限から漏らすと、flagが立ったままの状態で要求と完了を無限に回し得る。

### ADR-0020の訂正

26. ADR-0020 決定30-bとIssue #13コメントで「artifactの残り2種の収集はPR-3b2」と書いたが、3-bを分割したため**redact済みlogはPR-3b3**（headless adapterのstdout / stderrがそのproducer）が扱う。canonical recordのlocal artifactはC-09以降のままである。
27. ADR-0020 決定31の「tmp directoryはworkspace外」は現行workflowと一致している（PR-3b1のCI修正commitで直した）。

## Consequences

- **ADR-0019 決定17が送った2項目が解消した**。停止意図はcheckpointへ残り、停止に失敗したrunを別processが引き継いで完走できる
- **Ctrl+Cがtracebackにならなくなった**。実signalを別processへ送るtestで、構造化結果と終了codeで返ることを固定した
- **C-01は無変更**である。緊急停止はC-05をそのまま使い、`emergency_evidence`の意味も変えていない
- `stop_request`の書き手は現時点でC-08だけである。将来ユーザー起点の停止要求が増えても、同じsectionを使うかどうかはその時点で決める（今は緊急停止の1種類しか無く、`kind`のような値域を先取りしない）
- **AC-C08-04（active / headlessの同値性）は未充足**のままで、PR-3b3が扱う。`HostPort`と`drive`という構造はPR-3b1で用意済みである
- **`processes`台帳の書き手はまだ存在しない**。本PRは読んで停止するだけで、testが台帳をseedする。書き手はtreeを起動するcomponent（PR-3b3のheadless adapter、C-09）である
- **AC-C03-02のwiringが初めて成立した**。ADR-0005はC-03に停止primitiveだけを置き、2段階Ctrl+Cのwiringを「C-08が実装すればよい」として保留していた。signal handlerを設置する本PRがその実装地点である
- AC-C03-02のE2E testは**実際に終了しないtreeへ実signalを2回送る**。treeを起動するのはtestで（C-03の公開API）、**製品codeはprocessを起動しない**という本PRの範囲は変わらない
- 2回目が届く窓は**grace待機中**と**最初の安全点より前**の2つがあり、E2Eは両方を覆う。後者はdriverが「1回目を観測した直後・要求を保存する前」で待つ継ぎ目を持つ（待ち時間の代わりになるのは、実際にはcheckpointのI/Oと`drive`のhost作業である）
