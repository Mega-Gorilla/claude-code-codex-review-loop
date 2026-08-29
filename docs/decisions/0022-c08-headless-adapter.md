<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0022: headless adapterとactive / headlessの同値性

- Status: Accepted
- Date: 2026-08-29

## Context

PR-3b1（#45）でruntimeとentry pointが、PR-3b2（#46）で緊急停止の契約が入り、Phase 8で未充足の受入条件は**AC-C08-04（headless経路と主経路の同値性。MVP-06）だけ**になっていた。

3-bをspawn境界で3本へ分けた基準は「**spawnerと停止機構は同じPRに入るか、両方入らないか**」である（ADR-0020 決定3）。3-b2で停止機構がdurableになったので、**いま初めてspawnerを足せる**。逆順だけが禁じられていた。

同時に、ADR-0019 決定10が「`processes`台帳の書き手はtreeを起動するcomponent」と定めたまま、PR-3b1 / 3b2は**読み手だけ**を実装していた。treeを起動する最初のcomponentが本PRなので、書き手もここで入る。

## Decision

### 起動commandは呼び出し側が渡す

1. **adapterは既定値を持たない**。起動command・作業directory・env・timeoutはすべて必須fieldである。`identity.auto_mode`の`probe_auto_mode`が「実行commandとtimeoutの既定値は持たない（解決はC-12）」としているのと同じ形で、設定解決はC-12の領域である。
2. **`SessionConfig`（`session.json`）は変更しない**。portの束（`default_ports`）とhostは別物で、どのhostを使うかは呼び出し側が決める（今はtest、将来はC-12）。configへ足すと「headlessを使わないrunもheadlessの設定を持つ」ことになる。
3. **argvの先頭は絶対path必須**。`SpawnSpec`はenvを継承しないため、PATH解決に依存できない（auto_modeと同じ検査）。
4. argvは`ensure_argv_allowed`を通す（P-006のruntime choke point）。permission flagは`command`として渡す側の責任で、禁止flagはここで落ちる。**adapterがflagを組み立てることはない**。
5. **protocolは「prefix + envelope path」**。adapterはargv末尾へenvelope pathを足して起動し、子は結果を`result_path`へ書き、submit envelopeをstdoutへ出す。`gh_command` + subcommand、`claude_command` + `auto-mode config`と同じ形である。promptの組み立て（Claude CLIへの指示文）は実装planが`runtime/prompt`として別に置いており、本PRは搬送の形だけを持つ。

### `processes`台帳の書き手

6. **`run_tree`は使えない**。spawn -> wait -> closeを内包してhandleを返さないため、台帳へ載せる隙が無い。adapterが同じ保証（AC-C03-01: どちらの経路でもtreeを残さない）を保ったまま段階を開く。
7. **登録は待機より先**（ADR-0019 決定10）。待っている間にControllerが落ちたtreeを、次のprocessが台帳から止められる。
8. **read-modify-writeする**。`with_active_trees`はlistを全置換するので、自分のrefだけを渡すと他componentのtreeが消える（C-09のreviewerとheadless coderは並走し得る）。`with_tree_added` / `with_tree_removed`を`checkpoint_view`へ置き、登録も除去も**冪等**にする。
9. **除去は停止を確認してから**行う（決定11）。`close()`を`try/finally`で必ず呼び、その後に台帳から外す。残すとpidが再利用されたときに別treeへ到達し得る。
10. **spawn直後から登録までの窓は構造的に残る**。refはspawnしないと決まらないためである。ADR-0019 決定10が前提にしていた形で、この窓で落ちたtreeは台帳に載らず、次のresumeが拾えない。**本PRはこの窓を閉じない**。
11. 台帳を更新できない場合（checkpointが読めない、sectionが壊れている）は**推測せず失敗させる**。treeを起動したのに台帳へ載せられない状態で待ち始めない。

### stdoutはデータ、stderrはlog

12. stdoutは**submit envelope**である。`host.stdout`へ受け、adapterが読んでbytesで返す。読む前にsizeを検査する（entry pointの`MAX_SUBMIT_BYTES`と同じ理由・同じ値）。
13. stderrは**log**である。`host.stderr`（raw）へ受け、子の終了後に`policy.redaction.redact`を通して`host.log`へ書く。redaction moduleは「C-08のlog」を消費者として名指ししており、その実装地点である。
14. **CIのartifactが集めるのは`host.log`だけ**。`host.stdout` / `host.stderr`は`result.json`と同じ扱いで収集しない（file名のallowlistとcontract testで固定。ADR-0020 決定30-a / 30-c）。これでADR-0021 決定26が本PRへ割り当てた「redact済みlog」が入り、**残るのはcanonical recordのlocal artifactだけ**（C-09以降）になる。
14-a. **子が書いたresult fileはadapterが作成者限定へ揃える**。engineはresult fileが作成者限定であることを検証してから読む（`read_result` -> `verify_private_file`。AC-C06-05: artifactにはprivate repositoryのdiffとreview内容が入る）。子は外部programで自分のumaskで書くため、POSIXでは既定`0o644`になり、**揃えないとheadless経路はPOSIXで一切通らない**。外部programと私有state directoryの境界はadapterなので、ここが責務である。
14-b. 揃えるときは**bytesとして読み直してから書き戻す**。`result_hash`は子が同じbytesで計算しており、text経由の改行変換で1 byteでも変わるとhash照合が落ちる。UTF-8でない出力は構造化して落とす。
14-c. 揃えられない場合（読めない・UTF-8でない・権限を付け替えられない）は**推測せず`HeadlessError`へ写す**。決定15と同じ扱いで、engineの検証まで持ち越さない。
14-d. `host.log`も**作成者限定で書く**。CIのartifactが集めるfileで、private repositoryの内容が写り込み得る。C-03はredirect先を`0o600`で開くので`host.stdout` / `host.stderr`は既に作成者限定である。
15. **例外を呼び出し側へ飛ばさない**。起動失敗・timeout・非0 exit・出力不正はすべて`HeadlessError`へ写す。`HostPort.execute`はbytesを返す契約なので、失敗は例外で明示するしかないが、**種類は1つに絞って理由をdetailへ入れる**。

### AC-C08-02のcontract testを絞る

15-a. ADR-0020は「`runtime` packageがprocess起動もキー入力注入も**構造的に持たない**」というAST contractを置き、「spawnerを足すときはこの契約を明示的に更新することになる」と書いていた。**本PRがその更新地点である**。
15-b. AC-C08-02が禁じるのは**主経路でのsubprocess起動**であって、headless経路の起動ではない（implementation plan Section 4:「Claude coder（headless経路）はControllerがsubprocessとして起動するadapter」）。そこでspawnの検査対象から`host_headless.py`だけを外し、**他のどのmoduleにも起動手段が無い**ことを固定する。
15-c. 絞ったぶんを**別の検査で補う**。「`spawn_tree`をimportするmoduleは`host_headless.py`ただ1つ」を固定するので、起動が主経路のmoduleへ漏れた瞬間にfailする。ADR-0020の版より**強い**主張になっている。
15-d. **キー入力注入はheadless経路でも禁じる**（対象外にしない）。起動と入力注入は別の話で、後者に例外は無い。
15-e. `subprocess`の直接importは全moduleで禁じたままにする。起動も停止も**C-03経由**である。

### 同値性（AC-C08-04）の観測

16. `drive`や`DriveResult`を増やさず、**testが同じloopを2回まわして比較する**。`HostPort`が同一interfaceであることの意味は、engineから見た振る舞いが一致することだからである。
17. 比較対象は**state遷移列**（`step` / `submit_result`の各呼び出し後のcheckpoint）と**canonical record列**（fake GitHubのchainをseq昇順）の2つ。driverは同じ関数で、違うのは`HostPort`の実装だけである。
18. **fake headless hostは実行file**にする（fake ghと同じ置き方）。境界が実行fileなので、spawn・待機・stdout回収・台帳・redactionはすべて製品codeが走り、fakeなのは「何を返すか」だけになる。何を返すかはplan file（JSON）で与え、子が自分のstate fileで消費順を進める。
19. **台帳の登録順序も子から観測する**。子は自分が生きている間のcheckpointの`processes` sectionを書き出す。親は`wait`で止まっているので、そこに自分のtreeが見えれば「登録は待機より先」が成り立っている。

## Consequences

- **AC-C08-04 / MVP-06が充足した**。Phase 8のAC-C08-01〜07がすべて揃った
- **`processes`台帳が読み書き揃った**。PR-3a（読んで停止）、PR-3b2（緊急停止で読んで停止）に対し、本PRが書き手を入れた
- **CI artifactは3/4種になった**。checkpoint / envelope / redact済みlogを集め、canonical recordのlocal artifactだけがC-09以降に残る
- **主経路は変わっていない**。active hostは`HostPort`を実装せず、Controllerがsubprocess化することもない（AC-C08-02）。同値性testはその対比として、active側のspawn回数が0であることも観測する
- spawn直後から台帳登録までの窓は残る（決定10）。閉じるにはC-03がrefを予約する仕組みが要り、C-03の責務範囲を広げる判断になるため本PRでは行わない
- **子の出力の権限はadapterが引き受ける**（決定14-a）。C-09がCodex reviewerを起動するときも同じ問題が出るので、同じ手順を踏むことになる
- `HeadlessError`は`HostPort.execute`の契約上の例外である。`drive`はこれを捕まえないので、呼び出し側（C-12）がheadless経路の失敗をどう扱うかはそのPhaseで決める
