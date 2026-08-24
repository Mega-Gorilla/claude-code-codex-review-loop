<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0013: C-07 resume contextとpending recordの再発行

- Status: Accepted
- Date: 2026-08-24

## Context

Phase 7はPR-1（ADR-0010: recordのcanonical projection）、PR-2（ADR-0011: checkpoint storeとPR lock）、PR-3（ADR-0012: resume観測と承認失効）で部品を揃えた。**部品は揃っているが配線が無い**状態で、次の3つが欠けている。

1. **resume contextを組み立てる経路が無い**。discovery / reconcile / artifactsを呼ぶ側が存在せず、AC-C07-01（同じturn IDから再開）を満たす出口が無い
2. **中断したturnを再開できない**。checkpointの`transaction`は保存できるが読み手がおらず、同一keyでrecordを再発行できない（AC-C07-02）。markerの再構成にprojectionが要るが、`transaction`にprojection fieldが無い
3. **GitHub直接回答の取得経路が無い**（AC-C07-06）

前提はIssue #12の実装契約: Phase 7の成果物は**resume context**であり`MachineState`の完全replayではない／判断根拠は検証済みrecordのみ／曖昧なら停止／record -> C-01 eventの対応表はC-10 / C-11。

## Decision

### 再発行directive（AC-C07-02）

1. **C-07は投稿しない**。resumeは「この本文をこのkeyで投稿する」というdirectiveまでを返し、実際の再投稿はC-01のR-P（pending保持中の明示resume -> `PersistRecord`）を経てC-08が実行する。C-07が独自の投稿経路を持つと、state machineの外側にGitHub mutationが1つ増える。**「resumeは投稿しない」という以前の誤った主張とは別物**で、再発行そのものは必須（禁止されるのは重複投稿と別keyでの再投稿）
2. **投稿済み判定は検証済みrecordで行い、bindingの一致だけで確定しない**。marker `key` = `PersistRecord.binding`（ADR-0010 決定7）だが、**C-06はkeyを本文から再導出しない**ため、「同一key・別本文」のrecordはintactなchainとして通ってしまう。これを投稿済みと受理すると、中断したturnの内容が永久にGitHubへ載らないままtransactionが消費される（AC-C07-02の「同一key ⇒ 同一本文」に反する）。占有recordがある場合も期待する完成形を再composeし、**body hashの一致まで確認する**。body hashはmarker行（kind・head・seq・prev・projection）を覆うため、この1回の照合で全要素の一致を判定できる。C-05の`ensure_comment_posted`も投稿直前にsearch-firstを行うため、重複防止は二重に効く
3. **markerは製品関数で再構成する**（`compose_record_marker_payload` -> `attach_marker`）。`prev`は検証済みchainの`seq-1`のbody hash（**GitHub由来**）で、checkpointに保存した値を使わない。chainが進んでprevが変わっていれば、次の決定4で検出される
4. **byte一致を検証する**。`transaction.body_hash`が記録されていれば、再compose結果の`body_hash_of`と一致することを要求する。不一致は`PendingUnavailable`で停止する（同一seqで異なる本文を投稿すると、C-06のseq conflictで`BLOCKED`になる。ADR-0010 決定13）
5. **推測して再投稿しない**。直前seqがchainに無い（gap）／同一seqを別bindingのrecordが占有している（seq conflict）／composeがADR-0007の上限に触れる、はいずれも理由つきで停止する
6. **解釈できない`transaction`を「中断中のrecordは無い」へ丸めない**。`PendingUnavailable`として提示する（ADR-0011 決定4のsilent repair禁止と同じ方針）

### `transaction.projection`（C-02のadditive追加）

7. **projectionをcheckpointへ保存する**。markerの再構成にはprojectionが要る（検証済みpayloadは保存していないため、その場では再導出できない）。ADR-0004の「fieldは利用するPhaseで追加する」に従い、Phase 7 PR-4で追加する
8. **key集合はC-02の`PROJECTION_KEYS`と同じ**にし、`obj`が未知keyを拒否する性質に合わせて9 keyを列挙する。`pay`のみ必須。宣言集合と`PROJECTION_KEYS`の一致をtestで常設する（drift防止）
9. **`body_hash`はoptionalのまま**にする。requiredへの変更は既存fieldの制約強化でADR-0004の非互換変更にあたるため行わない。契約は「記録されていれば照合する」で、directiveは常に再compose結果のhashを返す

### 直接回答（AC-C07-06 / D-021）

10. **受理検証はC-06の`accept_user_decision`をそのまま使う**。allowlist完全一致・編集・消費済み・観測元・埋め込みtokenの判定をC-07で再実装しない
11. **C-07が持つのは構造的な絞り込みだけ**: 予約markerを持つcomment（Controllerのrecord）を除く／`after`（既定は**最新の検証済みrecordのcreated_at**、排他）より後／消費済みIDを除く。「どの質問への回答か」という意味解釈はC-11の責務なので、`DecisionContext`は呼び出し側が注入する
12. **有効な候補が2件以上なら停止する**。「最新を採る」等の推測をしない（不変条件「曖昧な肯定を承認と解釈しない」の具体化）
13. **allowlistが取得不能なら候補を評価しない**（`DirectAnswerUnavailable`。AC-C06-02のfail closed）。受理されなかった候補は理由つきで保持し、silentに捨てない

### resume contextの組み立て（AC-C07-01）

14. **head照合に依存しない観測を先に集める**。pending評価と直接回答の列挙はhead照合に依存しないため、head段階の停止より前に実行し、**停止結果にも同梱する**。そうしないと、(a) C-01のR-P（pending保持中の明示resumeは永続化確認を優先）へ渡すdirectiveが停止で失われ、(b) `MERGE_FAILED`で「merge承認を確認できないので停止 -> その承認が居る候補列挙へ到達できない」という循環が生じる
15. **C-07は直接回答を承認へ変換しない**。`accept_user_decision`はactor / allowlist / 観測元 / 編集 / consumed / markerを検証するだけで、**本文の意味を判定しない**（受理は「ユーザー判断のexternal evidenceとして扱える」までの確定）。`DecisionContext.kind`は期待する入力種別へのbindingであって「本文が賛成である」というverdictではない。ここで承認へ変換すると、**明示的な反対commentがmerge gateを開けてしまう**（不変条件「曖昧な肯定を承認と解釈しない」に正面から反する）。C-07は候補を`direct_answer`として提示するに留める
16. **意味解釈済みの承認は注入口で受ける**（`ResumeObservation.interpreted_approvals`）。D-021の承認はchain recordを伴わないため、`reconcile_head`へ渡さないと存在しないものとして扱われる。そこで**二段階経路**にする: (1) resumeは候補を同梱した停止を返す -> (2) C-11が明示的な`APPROVE_MERGE`と解釈した`AcceptedUserDecision`だけを注入し、resumeを再実行する（`ApprovalEvidence`への変換は、C-07が決定17の再検証を通した後に行う）。`MERGE_FAILED`の循環（承認を確認できないので停止 -> 承認候補へ到達できない）は、停止結果へ候補を同梱すること（決定14）で解消しており、C-07が意味解釈する必要はない。**ADR-0012 決定15の「組み立てはPR-4」はここで更新する**（**意味解釈はC-11**、PR-4が用意するのは注入口と、注入値を再検証して承認evidenceへ変換する経路）
17. **注入された承認を現在のcommentへ再検証してから使う**。受理時の値をそのまま信用すると、二段階経路の1回目と2回目の間にcommentが**編集・削除**されても古い承認でmerge gateが開く（D-031 / ADR-0008 決定20の「編集・削除で判断を失効」に反し、human merge gateを取り消せない）。注入するのは`ApprovalEvidence`ではなく元の`AcceptedUserDecision`とし、**同じ観測窓の現在のcomment**に対してC-06の`revalidate_user_decision`を通し、`VALID`のものだけを承認evidenceへ変換する。取得窓に現在のcommentが無い場合は削除として扱う（fail closed）。失効した注入は`voided_approvals`として理由つきで提示し、silentに捨てない
18. **pure coreとI/O収集を分ける**（`build_resume_context` / `observe_resume`）。C-06の`verify_record_chain` + `probe_known_records`と同じ構造で、判定はfixtureだけで決定論的に検証できる
19. **段階ごとに停止する**（`ResumeStage`: `RUN_SELECTION` / `INTEGRITY` / `HEAD` / `PENDING` / `DIRECT_ANSWER`）。`ResumeStopped`は理由に加えて**原因の値そのもの**（`RunAmbiguous`や`ChainVerification`等の直和）を持ち、detail文字列だけで区別させない
20. **chain violationは停止**（C-06の契約どおり`is_intact`でgateする）。一方、**artifactの不一致は停止ではなくcacheの破棄**として結果に載せる（GitHub側が常に上位。ADR-0012 決定19）
21. **pendingとheadの優先順位を決めない**。C-01のR-Pがpendingを優先するため、順序の決定はC-10に属する。C-07は両方を観測結果として返す
22. **artifactのhash読み出しは注入する**（(run ID, 記録path) -> hash）。coreを純粋に保ち、I/O側は`artifact_content_hash(paths.runs_dir / run_id, path)`を渡す（`run_directory`はdirectoryを作成するため使わない）
23. **既定値を持たない**。`max_pages`等の設定はすべて引数で受け取る（既定値の解決はC-12）

## Consequences

- C-08はturnの節目で`save_checkpoint`し、投稿前に`transaction`（projection込み）を保存してから`PersistRecord`を実行する。resume時は`PendingReissueRequired.body`をそのまま`ensure_comment_posted`へ渡せばよい
- C-10は`ResumeContext`をstateと組み合わせてC-01 eventを構築し、`ResumeStopped`はeventを発行せず理由を提示する。停止結果に同梱された`pending`（R-P用のdirective）と`direct_answer`は、停止の提示と同時に扱える。直接回答の`DecisionContext`（期待するuser-input record種別・head・fingerprint）と、候補本文の意味解釈（`APPROVE_MERGE`か否か）はC-10 / C-11が決め、解釈結果だけを`external_approvals`として注入する
- Phase 7の受入条件（AC-C07-01 / 02 / 03 / 05 / 06）が揃う。AC-C07-04（retention / cleanup）はPhase 14（#19）が扱う
