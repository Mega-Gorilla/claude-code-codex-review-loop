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
5. `write_private_text`にも`fsync`を入れ、作成と置換の耐久性を揃える。`os.replace`後のdirectory entryはPOSIXで`sync_directory`（directory fdの`fsync`）により確定させる。Windowsはdirectory handleへのfsyncができないためno-opとし、契約だけを揃える

### checkpointへ追加するfield（ADR-0004のadditive規則）

6. **Phase 7が使う分だけ追加する**。`state`へ`awaiting` / `return_to` / `recovery_to` / `pending_record`（kind・binding・source_state）、新設の`transaction`（binding・kind・seq・head_sha・payload_hash・body・body_hash）、新設の`artifact_records`（path・kind・content_hash・approved_head_sha・record_binding・comment_id）。語彙は`Awaiting` / `State` / `RecordKind`のenumをそのまま使う
7. **`procedure` / `block` / `deferred_integrity`は追加しない**。Phase 7は`MachineState`の完全replayを行わず、整合性blockはresumeのたびにC-06のchain検証で再導出する。これらを使うPhase（C-08 / C-10）が、使う時点で追加する
8. **既存`artifacts`のitem型は変えない**。array itemの型変更は非互換でversion bumpとmigrationを要するため、bind情報は`artifact_records`としてoptionalに並べる
9. `transaction.body`は**redact済みのrender出力**（marker付加前）だけを保持する。上限はC-05の`MAX_COMMENT_CHARS`と同じ65,536字

### PR lock

10. **lockはcheckpoint fieldにしない**。lockの取得はcheckpointの更新とは別のoperationであり、混ぜると「lockのためにcheckpointを書く」逆転が起きる。TE 10.1の18項目にもlockは無い
11. **配置はper-user state root配下の`locks/<repository digest>/<number>.lock`**。run directory配下へ置くと、worktreeごと・run IDごとにlockが分裂して同一PRへの同時runを検出できず、AC-C10-03の前提が壊れる。repository slugは`/`を含みpath要素にできないためdigest化し、可読性はlock file本体の`repository` fieldで担保する
12. **lock fileの内容はC-02の`RUN_LOCK` schemaで検証する**（独自parseを持たない）。解釈できないlockは`LockCorrupt`として提示し、「無いもの」として上書きしない
13. **stale lockの回収は3条件がすべて揃った場合のみ**: 記録されたpidが生存していない、hostが一致する、再開しようとするrunのIDが一致する。1つでも欠ければ理由つきで停止する
14. **pid生存の判定は曖昧さを「生存」へ倒す**（`process.is_process_alive`）。権限不足（POSIXの`PermissionError` / Windowsの`ERROR_ACCESS_DENIED`）は「存在するが触れない」として生存扱いにし、pid再利用も生存と誤判定し得る。いずれも**回収しない**側へ働くため安全側である。Windowsはexit code 259の曖昧さを避けるため`WaitForSingleObject`のtimeoutで判定する（handleには`SYNCHRONIZE`が要る）
15. **回収後に読み戻して確認する**。`os.replace`で置換した後、自runのrun ID / pid / hostであることを再取得して確かめる。同時に回収を試みた別processに奪われていれば`LockHeld`として停止する。完全な排他はAC-C10-03（C-10）が扱い、本Phaseは永続表現と照合までを担う
16. **解放は自runのlockに限る**（run IDとpidの一致）。他runのlockは削除しない

### path規約

17. **state rootは注入された正規化済み絶対path**だけを受け取る。既定値の解決はC-12。相対pathはcwd依存でstate rootが動くため受理しない
18. **containmentは作成より前に判定する**（state root外へdirectoryを作らない）。run IDの文字集合はADR-0010の`validate_run_id`と**同一の定義を共有**し（`.` / `..` / 先頭`-`にならない）、bindingとpathで二重定義を持たない

## Consequences

- C-08はturnの節目で`save_checkpoint`を呼び、投稿前に`transaction`を保存してから`PersistRecord`を発行する。resume（PR-3）は`transaction`があれば同一keyでsearch-first -> 再投稿する
- C-10はPR modeの開始時に`acquire_pr_lock`を呼び、`LockHeld` / `LockCorrupt` / `LockUnavailable`をworkflow上の拒否（AC-C10-03）へ写す
- Phase 14（retention / cleanup）はstate root配下の`runs/`をcleanup対象treeとして扱える。lockは別treeにあるため、active / lock保持中のrunの除外判定が単純になる
- OS専用backendの追加分（`process/liveness.py`は共通facade、実装は既存の`job_object.py` / `process_group.py`）はCIの既存omit規則にそのまま乗る
