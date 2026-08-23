<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0011: C-07 checkpoint storeとPR lockの配置

- Status: Accepted
- Date: 2026-08-23

## Context

C-07は「GitHub canonical conversationからstateを再構築し、local checkpointはcacheとして照合する」（implementation plan C-07節）。PR-1（ADR-0010）でGitHub側からrecordの意味をdecodeできるようになったが、local側は**schemaが存在するだけで読み書きするcodeが無い**状態だった。

resumeが成立するために足りていないものが3つある。

1. **中断点の表現**: checkpointの`state` sectionは`state` / `round` / `agent_role` / `session_id`の4項目だけで、`awaiting`（次に受理してよい応答）も、投稿途中のrecordも表現できない。AC-C07-01（同じturn IDから再開）とAC-C07-02（質問を重複投稿しない）が成立しない
2. **crash windowのtransaction値**: ADR-0010 決定13のとおり、同一`seq`で再composeした結果がbyte一致しないとC-06のseq conflictで`BLOCKED`になる。同一keyで再発行するには中断時の値を保存しておく必要がある
3. **安全なI/O**: `identity/fs_permissions.py`は排他作成しか持たず、繰り返し更新するcheckpointを安全に置き換えられない。またPR lockは同一マシンの全worktreeから見える場所に無ければ同時runを検出できない

## Decision

### checkpoint I/O

1. **atomic replaceを採る**（`identity/fs_permissions.replace_private_text`）。同一private directory内へ一時fileを排他作成し、書込 -> `fsync` -> `os.replace`で差し替える。どの時点で中断しても、pathには置換前か置換後の内容だけが見える（truncateされた中間状態を作らない）。置換後は権限・link数1・実体を読み戻して検証する
2. **世代は保存しない**。GitHubがcanonicalでlocal checkpointはcacheであり、世代保持は保持期間・cleanup対象の議論をPhase 7へ持ち込む。salvage用の世代が必要ならPhase 14で判断する（file配置はschema fieldではないため後から追加できる）
3. **保存は「先にschema検証 -> 書込」**。不正なcheckpointをfileへ落とさない。既存checkpointは置換前に権限を検証し、作成者限定でないfileを上書きしない
4. **読込結果は構造化直和**: `CheckpointLoaded` / `Missing` / `Unreadable` / `PermissionViolation` / `SchemaInvalid` / `MigrationUnavailable`。version・migration判定はC-02の`load_with_migration`をそのまま使い、構造化errorを握り潰さない。**壊れたcheckpointを「無いもの」として黙って上書きしない**（silent repair禁止）。GitHubとの不一致は検証済みrecordとの照合結果であり、resume（PR-3）が加える
5. **全byteを書き切ってから確定する**。`os.write`は例外を出さずに要求より少ないbyte数を返し得るため、戻り値を無視すると短いfileがそのままfsync・`os.replace`され、「旧内容か**完全な**新内容」という保証が破れる。書き切るまでloopし、書き進められない場合はerrorにして**部分的なfileを残さない**（両backendが共通helper`write_all`を使う）
6. `write_private_text`にも`fsync`を入れ、作成と置換の耐久性を揃える。`os.replace`後のdirectory entryはPOSIXで`sync_directory`（directory fdの`fsync`）により確定させる。Windowsはdirectory handleへのfsyncができないためno-opとし、契約だけを揃える

### checkpointへ追加するfield（ADR-0004のadditive規則）

7. **Phase 7が使う分だけ追加する**。`state`へ`awaiting` / `return_to` / `recovery_to` / `pending_record`（kind・binding・source_state）、新設の`transaction`（binding・kind・seq・head_sha・payload_hash・body・body_hash）、新設の`artifact_records`（path・kind・content_hash・approved_head_sha・record_binding・comment_id）。語彙は`Awaiting` / `State` / `RecordKind`のenumをそのまま使う
8. **`procedure` / `block` / `deferred_integrity`は追加しない**。Phase 7は`MachineState`の完全replayを行わず、整合性blockはresumeのたびにC-06のchain検証で再導出する。これらを使うPhase（C-08 / C-10）が、使う時点で追加する
9. **既存`artifacts`のitem型は変えない**。array itemの型変更は非互換でversion bumpとmigrationを要するため、bind情報は`artifact_records`としてoptionalに並べる
10. `transaction.body`は**redact済みのrender出力**（marker付加前）だけを保持する。上限はC-05の`MAX_COMMENT_CHARS`と同じ65,536字

### PR lock

11. **lockはcheckpoint fieldにしない**。lockの取得はcheckpointの更新とは別のoperationであり、混ぜると「lockのためにcheckpointを書く」逆転が起きる。TE 10.1の18項目にもlockは無い
12. **配置はper-user state root配下の`locks/<repository digest>/<number>.lock`**。run directory配下へ置くと、worktreeごと・run IDごとにlockが分裂して同一PRへの同時runを検出できず、AC-C10-03の前提が壊れる。repository slugは`/`を含みpath要素にできないためdigest化し、可読性はlock file本体の`repository` fieldで担保する
13. **lock fileの内容はC-02の`RUN_LOCK` schemaで検証する**（独自parseを持たない）。解釈できないlockは`LockCorrupt`として提示し、「無いもの」として上書きしない
14. **stale lockの回収は3条件がすべて揃った場合のみ**: 記録されたpidが生存していない、hostが一致する、再開しようとするrunのIDが一致する。1つでも欠ければ理由つきで停止する
15. **pid生存の判定は曖昧さを「生存」へ倒す**（`process.is_process_alive`）。権限不足（POSIXの`PermissionError` / Windowsの`ERROR_ACCESS_DENIED`）は「存在するが触れない」として生存扱いにし、pid再利用も生存と誤判定し得る。いずれも**回収しない**側へ働くため安全側である。Windowsはexit code 259の曖昧さを避けるため`WaitForSingleObject`で判定し、**明確にsignal済み（`WAIT_OBJECT_0`）の場合だけ**「不在」とする（`WAIT_FAILED`や未知の戻り値は生存扱い。handleには`SYNCHRONIZE`が要る）
16. **回収は排他guardの下で直列化する**。単なる置換 + 読み戻しでは、同じstale lockを読んだ2 processが順に置換して**両方が成功**し得る（読み戻しは「自分の確認より前に奪われた」場合しか検出できない）。一方、lock fileを一時退避してから確認する方式は**生きているlockを一度動かす**ため、差し戻しに失敗すると真の保持者のlockが消える。したがって:
    1. `os.mkdir`（存在すれば失敗する原子的な排他）で回収guardを取る。取れなければ`LockUnavailable`（推測して進まない）
    2. **guardの下で読み直し**、3条件と「判断根拠にしたlockと同一内容であること」を再判定する。生きているlockには一切触れない
    3. 条件を満たす場合だけ`replace_private_text`で置き換える
    guardを保持したままprocessが落ちるとguardが残り、以後の回収は`LockUnavailable`になる（fail closed。復旧はguard directoryの削除で、これは提示するdetailに含める）。guardの解放失敗は`LockGuardError`として表面化させる（残ると以後の回収が止まるため、silentに続行しない）
17. **lockの作成は原子的かつ排他的に行う**（`publish_private_text`）。`O_CREAT | O_EXCL`での直接作成は「作成」と「書込」の間に空のfileが見え、別processがそれを読むと破損と区別できない。一時fileへ全内容を書いてから`os.link`で公開すれば、他processからは「存在しない」か「完全な内容」のどちらかしか見えず、既存pathがあればlinkが失敗するので排他性も保てる
18. **保存前にowner payloadを検証する**。`RUN_LOCK` schemaに加え、回収判定に使う`pid` / `number`の下限（1以上）を入力検証で保証する。consumerが受理できないlockをproducerが作れると、silent repair禁止の下でそのpathを回復できなくなるため、不正入力は`LockInputError`で**書き込む前に**止める
19. **解放は自runのlockに限る**（run IDとpidの一致）。他runのlockは削除しない

### path規約

20. **state rootは注入された正規化済み絶対path**だけを受け取る。既定値の解決はC-12。相対pathはcwd依存でstate rootが動くため受理しない
21. **containmentは作成より前に判定する**（state root外へdirectoryを作らない）。run IDの文字集合はADR-0010の`validate_run_id`と**同一の定義を共有**し（`.` / `..` / 先頭`-`にならない）、bindingとpathで二重定義を持たない

## Consequences

- C-08はturnの節目で`save_checkpoint`を呼び、投稿前に`transaction`を保存してから`PersistRecord`を発行する。resume（PR-3）は`transaction`があれば同一keyでsearch-first -> 再投稿する
- C-10はPR modeの開始時に`acquire_pr_lock`を呼び、`LockHeld` / `LockCorrupt` / `LockUnavailable`をworkflow上の拒否（AC-C10-03）へ写す
- Phase 14（retention / cleanup）はstate root配下の`runs/`をcleanup対象treeとして扱える。lockは別treeにあるため、active / lock保持中のrunの除外判定が単純になる
- OS専用backendの追加分（`process/liveness.py`は共通facade、実装は既存の`job_object.py` / `process_group.py`）はCIの既存omit規則にそのまま乗る
