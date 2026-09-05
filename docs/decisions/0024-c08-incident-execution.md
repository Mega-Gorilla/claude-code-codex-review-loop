<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0024: `RecordIntegrityIncident`の実行と、壊れたchainの上での監査記録

- Status: Accepted
- Date: 2026-08-29

## Context

PR-3c（#48）で`BLOCKED`からの介入が閉じ、Phase 8に残る行き止まりは1つになった。cancel中またはmerge outcome確定時にintegrity violationを検出したrunが、`RecordingIncidentProcedure`へ入ったまま`incident_executor_missing`で止まる。

**C-01側は完成している**。incident recordのPRODUCED（I-P）、検証後のterminal遷移（I-VC）、部分記録の直列化（I-VR）、記録中の追加検出（I-D1）、resume時の再発行（`_reissue_incident_request`）がすべてある（AC-C01-12）。無いのは**C-08の実行**で、ADR-0019 決定20が「実行はC-08の責務だが、まだ実装が無い（PR-3d）」と名指しした箇所である。

### 実行を足すだけでは閉じない

incident recordは**まさにchainが壊れているときに投稿するrecord**だが、violationは`verify_record_chain`がliveのGitHubから毎回再導出するpure関数の出力であり、記録しても消えない。そのため`is_intact`でgateする2箇所（`produce_record`と`persist`）がこのrecordを永久に拒み、runはterminalへ到達しない。ADR-0019のConsequencesが「代表cancelシナリオが完走するのは`deferred_integrity`が空の場合である」と書いていたのはこの状態である。

## Decision

### 監査記録そのものはchainのviolationで拒まない

1. **`produce_record`は`INTEGRITY_INCIDENT`かつ`RecordingIncidentProcedure`のときviolationで拒否しない**。それを記録するためのrecordを「chainが壊れている」という理由で作れないのは循環である。判定は`machine_state`と`kind`から自己完結し、新しい引数を増やさない。
2. **取りこぼしは別の2点が担保する**。`persist`が未知のviolationを投稿前にC-01へ渡すこと、そしてC-01が「全violationが検証済みrecordへ含まれるまでterminalへ進まない」こと（AC-C01-12）である。したがって決定1は**記録の網羅性を緩めていない**。
3. **`persist`が緩めるのは既知violationだけ**である。新しいviolationは従来どおり検出が優先され、投稿しない。記録漏れは行き止まりより悪い。
4. **incident record以外は1 bitも緩めない**。壊れたchainの上へ通常のrecordを積むことは引き続き禁止で、既存testがその不変を固定している。

### 記録済みviolationの台帳

5. **`incident_record.recorded_bindings` sectionをadditiveに追加する**（新しいoptional sectionのためversion bumpなし。ADR-0004 rule 2 / 10）。C-01は記録済みviolationを`deferred_integrity`から外すが、chainからは消えない。台帳が無いと、記録済みを次のcycleで「新しい検出」としてC-01へ再入力し、**部分記録（I-VR）のrunが記録と再検出を往復して終わらない**。
6. 台帳へ書くのは`IntegrityIncidentVerified.recorded_bindings`、すなわち**C-06が構成・検証した値そのもの**である。C-08は解釈せず写す。書く位置は投稿後の検証地点（`_verify_and_advance`）で、record が canonical record gate を通った後だけが台帳に載る。
7. これで`persist`の「既知」は`deferred_integrity`（未記録）と台帳（記録済み）の和になる。

### C-01の不変条件を重ねて検査しない

8. 次の2つはC-01が`MachineState`の構築時点で強制しており、C-08は**重ねて検査しない**。代わりに依存をtestで固定し、崩れたら気付けるようにする。
    - `INCIDENT_PENDING_SCOPE`: `INTEGRITY_INCIDENT`のpendingはincident記録中に限る（決定3の条件が`kind`だけで足りる根拠）
    - `INCIDENT_NEEDS_DEFERRED`: incident記録中にdeferred集合は空にならない（空recordを作る経路が無い根拠）
9. 重ねて検査すると**到達不能な分岐**になり、pragmaで覆うことになる。不変条件をtestで名指しするほうが、前提が変わったときに気付ける。

### 実行の形

10. `advance`は`IncidentRequired`を返し、step driverが`record_incident`を実行する。`HaltRequired` -> `halt`（ADR-0019）と同型で、`advance`へbody portを渡さずに済む。
11. **記録対象はC-01の状態から導く**。`deferred_integrity`と`procedure.audit`で、これはC-01自身が判断12で定めた決定論的な構成であり、resume経路の`_reissue_incident_request`と同じ導出である。
12. **payloadはportが供給し、engineが照合する**（`IncidentPayloadPort`）。内容の構成はC-06の責務だが（Phase 1計画の責務表）、記録範囲がC-01の指示と違うrecordを投稿するとcoverage判定が意図しない値になるため、engineが`violation_bindings`と`audit_reference`の完全一致を要求する。`default_ports`は`UnavailableIncidentPayload`で、`UnavailableActionPayload`と同じ扱いである。
13. **本文はControllerの発言として整える**（`prepare_controller_body`）。`prepare_public_body`はagent用でmodelを、`prepare_user_body`は転記用で入力経路を要求するが、incident recordを書いたのはController自身でどちらでもない。存在しないmodelや入力経路をheaderへ書かないために別の入口を持ち、sanitize / redactは同じ単一choke pointを通す。表示名は`session.json`の`controller_speaker`（optional + 宣言済み既定値。ADR-0004 rule 2 / 12）。
14. **`RESULT_VARIANTS`へは入れない**。同表は「agentまたはユーザーが**返す**結果」のregistryで、incident recordはengine自身が作るものである（`head_source`を持たない点も表の前提と合わない）。record -> eventの写像は`RecordEventPort`が担い、`recorded_bindings`は`report` / `head`と同じくportが供給する。

### 壊れた末尾の採番と連結（Issue #50）

15. **incidentのseqは、検証済み列の最大seq・観測最大seq・checkpointの`assurance_high_water`の最大値 + 1**とする。改変済みrecordは検証済み列から外れ、削除済みrecordは観測最大からも外れるため、検証済み列だけでは番号を再利用してしまう。既存番号を埋めず、元のgap / missingは監査記録を投稿しても検出され続ける。
    - **保証範囲**: 観測最大はmarker解析・producer allowlist検査を通った候補のseqに限る。actor違反・marker解析失敗のcommentの主張だけを根拠に番号を予約せず、後からallowlistを拡張したりmarkerの受理規則を変更したりした場合の遡及的なseq衝突回避は保証しない。
16. **incidentだけに構造key `audit_prev`（先行する検証済みrecordのseq）を追加する**。producerは検証済み末尾を選び、`prev`にその完成本文hashを置く。先行recordが無い場合は`audit_prev=0`を明示して`prev`を省略する。これはgenesisや新baselineではなく、欠落した過去を保持した監査記録である。通常recordのseq-1連結規則は変えない。
17. verifierは`audit_prev`をincident以外で拒否し、整数・`0 <= audit_prev < seq`・hashの有無を検査する。linkは検証済み先行recordの末尾へ限り、健全なrecordを飛び越せない。hash不一致は通常と同じchain violationとなる。gap / edited / missing等の既存検出条件は緩めない。
18. **投稿前にtransactionへ`audit_prev`と`audit_prev_hash`を保存する**（0の場合hashは省略）。完成本文hashは引き続き必須。C-07はその保存値から同じmarkerを再構成し、現在のanchor・hashおよび保存済み完成本文hashと照合する。anchorが後から改変・消失した場合は推測で連結し直さず停止する。同一bindingの本文を作り直すmigrationは行わない。
19. 投稿前に、incidentの番号が既知・観測済み範囲と重なり、同一bindingの検証済みrecordが無ければ`incident_sequence_occupied`で停止する。旧実装の誤採番transactionも再投稿せず停止する。既に汚染されたseqを本修正で消したり再利用したりしない。
20. checkpointはoptional fieldのadditive追加（ADR-0004）。markerもoptional構造keyの追加でv1を維持し、keyのない既存record・transactionは旧規則で読む。古い実装は新keyを拒否するため、新incidentを記録したrunを古い実装へdowngradeして継続できるとは保証しない。
21. 受入testは実際のfake gh上の改変・削除をC-06へ通し、通常実行と投稿前／投稿後・checkpoint消費前からの別process resumeで検証する。terminalだけでなく、元の違反集合・既存番号の不使用・監査台帳・canonical検証・重複なしを確認する（PR #51の18ケース）。
    - 実chainでの部分記録（I-VR）も確認し、2件目のincidentが1件目のseq・完成本文hashへ連結され、両方が検証済み列に残り、元の違反集合を消さないことを固定する。

## Consequences

- **Phase 8の行き止まりがすべて閉じた**。cancel起点（C-04）とMERGING起点（I-46 / I-47）の両方で、violationを抱えたrunがterminalへ到達する
- **部分記録の直列化が実際に回る**。記録中に新しいviolationが見つかっても、残余で次のcycleを回してからterminalへ進む（I-VR）。台帳（決定5）が無ければここは無限に往復する
- **`persist`のintegrity gateが1段深くなった**。判定は「pendingがincident recordか」「violationが既知か」の2つで、既存経路の挙動は変わっていない
- **checkpointに新しいsectionが1つ増えた**（`incident_record`）。読み書きはC-08だけで、C-01の状態ではない（記録の**事実**であって、判断の根拠ではない）
- `IncidentPayloadPort`の実装はC-06の領域である。Phase 8のfakeはその位置を示すもので、実装ではない
