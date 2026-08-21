<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0005: C-03 process tree停止機構（Job Object / process group）

- Status: Accepted
- Date: 2026-08-21

## Context

implementation planはC-03の要件を「POSIXはprocess group、WindowsはJob Object相当で子孫を停止する。Windows側は新規実装とし、移植元を前提にしない」と定め、AC-C03-01（両OSでtimeoutとCtrl+C後に孫processが残らない）とAC-C03-02（Ctrl+C 1回でgraceful cancellation、2回目で強制停止）を完了条件とする。参考実装の該当処理はPOSIX専用（`start_new_session=True` + `os.killpg`）で、Windows分岐は存在しない（research assessment）。責務分担はIssue #8の実装checklistで固定した: C-01が停止commandを決定し、C-08がCtrl+C受理と停止・checkpoint保存を調整し、C-03は指定されたtreeをOSごとに確実に停止する実行層に徹する。

## Decision

### tree捕捉

1. **Windows**: 子processを`CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP`で起動し、**孫を起動する前に**named Job Objectへ所属させてからmain threadをresumeする（後付け所属のrace windowを作らない）。breakaway許可flagを設定しないことで、子孫はjobから離脱できない
2. **Windows**: jobへ`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`を設定する。jobへの最後のhandleが閉じるとtreeは自動で全滅し、起動元processの急死に対する安全網になる
3. **POSIX**: `start_new_session=True`で起動し、child = session / process group leader（pgid == pid）としてtreeをprocess groupで捕捉する

### 停止の段階と意味論

4. 停止は**graceful要求 -> grace period -> 強制停止**の段階とする。gracefulはPOSIXが`killpg(SIGTERM)`、Windowsが`CTRL_BREAK_EVENT`（best-effort）。強制はPOSIXが`killpg(SIGKILL)`、Windowsが`TerminateJobObject`
5. **graceful要求の戻り値は「要求が受理されたか（requested）」であり配送保証ではない**。WindowsのCTRL_BREAKは成功してもeventがqueueされただけである。graceful成立の判定はtree生存の観測（WindowsはActiveProcesses == 0、POSIXは`kill(-pgid, 0)`）へ一本化する
6. `GRACEFUL`という結果は「grace期間内にtreeが消滅した」ことを意味する。並行するforce要求（2回目のCtrl+C等）との競合の最終確定はC-08が行う
7. 停止は**冪等**とする。対象treeが存在しなければ即時に`ALREADY_EXITED`を返す（C-01のcancellation契約「実行中processが無ければ完了eventは即時返る」）。強制停止後もtreeが残存する場合は構造化error（`StopError`）とし、停止commandの再発行に耐える
8. timeoutとgrace periodの**既定値をC-03は持たない**（必須引数）。既定値の解決はPhase 12（C-12設定解決）で行う。強制停止の完了確認に使う内部上限のみ実装定数とする

### 別processからの再停止（ref）

9. treeの再停止identifierとして、Windowsは`(pid, job_name)`（named jobを`OpenJobObjectW`で開く）、POSIXは`(pid, pgid)`を用いる。Windowsはname不在（`ERROR_FILE_NOT_FOUND`）を「停止済み」として扱える（KILL_ON_JOB_CLOSEの帰結）。POSIXはleader pidの`getpgid`照合でpid再利用を緩和する

### 既知limitation（OS非対称）

10. **POSIXでは孫が自ら`setsid()`等で別groupへ移るとkillpgの射程外になる**。stdlibのみでは原理的に防げない（WindowsはJob Objectのbreakaway拒否で封じられる）。また、group全滅後のpgid再利用は検出できない。いずれもdocstringへ明記し、C-07のresume設計で再訪する
11. WindowsのCTRL_BREAKはconsole構成に依存するbest-effortであり、受理されない場合は即時に強制停止へ進む。またSIG_IGNを設定したprocessもCTRL_BREAKで終了し得る（CRT既定handlerの挙動）

### 品質ゲートとの両立

12. OS専用module（`job_object.py` / `process_group.py`）は先頭で異OSを`ImportError`により拒否し（mypyのplatform narrowingで異OS解析を対象外にする）、OS分岐は`spawn.py`のconditional import 1箇所へ閉じる。CIのcoverage floor stepは**異OS側moduleだけをreport対象からomit**する（floor値100は変更しない）。環境依存で通る枝が変わる失敗経路は、testがmonkeypatch注入で両方向を決定的に踏む
13. mypyの`--warn-unreachable`を将来有効化する場合、platform guard行が`unreachable`警告になるため、guard方式の再設計が必要である

## Consequences

- C-08は「1回目のCtrl+C = `stop_tree`（graceful -> grace -> force）、2回目 = `force_stop`即時」のwiringだけを実装すればよい。grace待機はKeyboardInterruptで中断可能であり、その後の`force_stop`は冪等である
- C-09は環境変数を完全制御したspawn（explicit env）とtree全滅の保証を前提にでき、隔離checkoutの破棄（AC-C09-01）がfile handle残留で失敗しない
- WindowsのgracefulのE2E検証は、pytest自身のconsole有無に依存しないconsole-owner pattern（`CREATE_NEW_CONSOLE`の中継process）で行う

## 実装への反映

`src/claude_code_codex_review_loop/process/`（spawn / terminate / job_object / process_group）と`tests/test_c03_*.py`、CI workflowのcoverage floor step、CONTRIBUTING「品質ゲートの運用」が本ADRを実装する。
