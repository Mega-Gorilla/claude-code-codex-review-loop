<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0010: C-07 canonical record projectionとbinding導出

- Status: Accepted
- Date: 2026-08-23

## Context

resumeは「GitHub canonical conversationからstateを再構築し、local checkpointはcacheとして照合する」（implementation plan C-07節）。しかしPhase 6完了時点でGitHubへ残る機械情報は`CC_REVIEW_META` markerの構造key 6種（`key` / `kind` / `run` / `head` / `seq` / `prev`）だけで、schema検証済みpayload（C-02）はどこにも永続化されていない。公開本文は人間可読テキストであり、free-textの判定は行えない（P-003）。

このままではGitHubから復元できない情報が3種類ある。

1. **結果の分岐**: `RecordKind.REVIEW_RESULT`はC-01の`ReviewApprovedVerified` / `ReviewBlockingVerified`へ、`CLARIFICATION_ANSWER`は4種のeventへ分岐する。kindだけでは承認が成立しているかを判定できず、AC-C07-03（外部head更新による旧承認の失効）が成立しない
2. **counterとfingerprint**: 正本は「clarification counterはGitHub上の`run ID + fingerprint + turn` metadataから再構築する」（implementation plan C-11節）ことを要求するが、fingerprintとturnはmarkerに無い
3. **対象への参照**: AC-C01-11は、block解消evidenceが「record自身のbinding」と「解除対象blockへの参照」を分離して持ち、blockと完全一致することを要求する。`schema/records.py`の`BLOCK_INTERVENTION`も`target_block_binding`を「canonical本文へ含め、C-06が検証する」と定義しているが、含める規約が無い

また、ADR-0007の決定6 / 7はidempotency keyを「本文markerから取り出す」「検索predicateはkey一致 AND body hash一致」と定めるだけで、**keyの導出規則が未定**である。body hashはmarkerを含む全body（`transport/conversation.py`の`body_hash_of`）で、chainの`prev`も同じ定義であるため、keyをbody hashから導出すると`key -> marker -> body hash -> key`の循環になる。

## Decision

### canonical projection

1. **markerへprojection keyを追加する**。projectionは**検証済みpayloadからの決定論的な射影**であり、新しい値を作らない。marker keyの値はすべてC-02 schemaに実在するfieldの写しで、語彙もschemaの`enum`をそのまま使う（`schema/projection.py`の`result_vocabulary`）。specの参照先が実在fieldであることと、C-02 registryとの一致はtestで常設検証する
2. 追加するkeyは9種: `res`（結果値）/ `round` / `turn` / `fp`（fingerprint）/ `sid`（subject ID: decision ID・permission ID等）/ `tgt`（対象binding・対象finding）/ `pay`（payload hash、**全kind必須**）/ `dig` + `cnt`（list値のdigestと要素数）。kindごとの必須・任意はProjection specが持ち、そのkindが持たないkeyの出現は非正規形式とする
3. **list値は内容を載せない**。正規化（sorted unique）した集合のdigestと要素数だけを載せ、内容は公開本文とlocal artifactへ置く。`INTEGRITY_INCIDENT.violation_bindings`が最初の利用者で、violation集合自体はresumeのたびにC-06のchain検証で再導出するため、markerはbindingとしてのdigestで足りる
4. **markerが本文の代替へ育つ方向を塞ぐ**。projection追加後のmarker payloadが2048 byte上限（ADR-0007 決定1）を超える場合は、本文をrenderする前に`compose_record_marker_payload`が停止する。「載せたい値が増えたらcapに当たる」構造にして、projectionの肥大化を設計段階で失敗させる
5. **構造keyを上書きできない**。projectionは`key` / `kind` / `run` / `head` / `seq` / `prev`を書き換えられない（識別・順序・連結の意味をprojectionが侵さない）
6. **markerのheadとpayloadの対象headの一致を要求する**。`build_record_projection`はkindごとの対象head field（`target_head_sha` / `pushed_head_sha` / `approved_head_sha`）とmarkerの`head`が一致しない限りprojectionを作らない
7. **projectionの正規性判定はC-02が持つ**。C-06の`_parse_chain_payload`は構造的canonical判定（許可key・canonical encoding・末尾1行・型）を行った後、`decode_record_projection`へ委譲し、失敗を**条件2（非正規marker）**として扱う。判定の定義を2箇所へ分散させない

### payload hashとbinding導出

8. `pay` = 検証済みpayloadのcanonical encoding（sorted keys / compact separators / UTF-8）のSHA-256 hex。**markerより前に確定する入力だけ**から決まる
9. **record binding = markerの`key` = idempotency key**（同一値、変換規則を持たない）。`OpaqueBinding`の採番はC-06 / C-08と定義済みで（`domain/values.py`）、`PersistRecord(kind, binding)`が既に冪等identityを持つ以上、markerのkeyを別namespaceにする理由がない。同一化により、C-05の検索predicate（key一致 AND body hash一致）がそのままdomainのbinding一致になる
10. 導出は`cr:{run}:{seq:08d}:{16 hex}`で、hexは`{run, seq, kind, head, pay}`のcanonical encodingのSHA-256前16桁。**`derive_record_binding`はbody hashを引数に取れない**（signatureが循環を型として排除する）。run IDは`[A-Za-z0-9._-]{1,64}`に限り、`:`区切りのbindingを一意に読めるようにする
11. 適用範囲はControllerが投稿するrecordのみ。GitHubへ直接入力されたuser record（D-021）はmarkerを持たず、bindingはC-06の`ud:`導出（Phase 6実装済み）のままとする

### crash windowとlocal artifact（Phase 7後続PRへの前提）

12. **pending recordのtransaction値をcheckpointへ保存する**。同一`seq`で再composeした結果がbyte一致しないと、search-firstが既存recordを見つけられずC-06の重複検出（seq conflict）でBLOCKEDになる。したがって投稿前・投稿成否不明で中断したrecordは、binding・`pay`・render済み本文・seq・headをlocal checkpointへ保存し、resumeは**同一keyで**再発行する（決定9と併せてAC-C07-02の重複投稿防止が構造的に成立する）
13. **local artifactはrecordへbindする**。artifactは「`pay` + head SHA + comment ID + body hash」へbindし、不一致なら破棄する（canonicalへ昇格させない）。完全payloadの置き場はlocal artifactであり、GitHubへ載せるのはprojectionだけ

## Consequences

- ADR-0007（marker許可key）とADR-0008（条件2の判定範囲）へ追補を入れる。marker versionは`v1`のまま据え置く（未releaseで、既存recordが存在しないため）
- C-08はrecord投稿時に`build_record_projection` → `derive_record_binding` → `compose_record_marker_payload`の順で組む。producerがprojectionを付け忘れたrecordは、C-06が条件2として拒否する
- record→C-01 eventの対応表は本ADRの範囲外である。`CLARIFICATION_RESULTS`は5値でclarification系eventは4種であり対応が1対1でないため、event化はC-10 / C-11が確定させる。Phase 7が消費するのは「(kind, 結果値)」までとする
- projection keyの増設は「正本に要求がある値だけ」を条件とし、増設時は本ADRの改訂を要する。増設の物理的な歯止めは決定4のcapである
