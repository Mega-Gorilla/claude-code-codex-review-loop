<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0008: C-06 record chainとallowlist受理の規約

- Status: Accepted
- Date: 2026-08-22

## Context

implementation planはC-06を「C-05が取得した未検証metadataから検証済みcanonical recordを生成する唯一のcomponent」と定め、record検証のthreat model（7条件）、high-water mark `N`による欠落検出とその限界（AC-C06-06〜09）、ユーザー判断受理のallowlist完全一致（D-031、fail closed。AC-C06-01〜02）を要求する。ADR-0007はmarker形式とbody hash規約をC-06との共有仕様点として確定済みだが、chain payloadの意味論（`seq` / `prev`の使い方）、「完全一致」の解釈、violation bindingの導出、検証algorithmの詳細はPhase 6で決定する技術判断である。本ADRのchain specはC-07（resume）とC-08（record producer）が参照する共有仕様点を含む。

## Decision

### Chain spec（`identity/record_chain.py`。C-07 / C-08と共有する仕様点）

1. 内部recordのmarker payloadはADR-0007の許可6 keyを全て使う: `key`（冪等投稿key。C-08採番）/ `kind`（`RecordKind.value`。21種のいずれか）/ `run`（run ID。**chainのscope**であり、同一Issue / PR上の複数runはrunごとに独立したchainを持つ）/ `head`（record対象のhead SHA）/ `seq`（**run内で1始まりの通し番号**。kind横断の単一系列、int）/ `prev`（**直前record（seq−1）の`body_hash`** = 正規化済み全body・marker行込みのSHA-256 hex 64桁小文字）
2. genesis（seq=1）は**prevの欠如**が正規形式であり、sentinel値を使わない。「seq=1にprevあり」「seq≥2にprevなし」は非正規形式（条件2）。prevが前recordのmarker行（そのseq / prev）を推移的に被覆するため、並べ替え・差し替えはhash照合で検出できる
3. 正規形式の完全定義: 本文末尾1行の`<!-- CC_REVIEW_META:v1 {...} -->`が抽出でき、payloadがJSON objectで、**raw JSONがcanonical encoding（sorted keysのcompact JSON、`ensure_ascii=False`）と完全一致**し（複数行・空白・key順の乱れ・重複key（parseで縮退する）を全て拒否）、**2048 bytes以下**で、**markerが本文末尾の単独1行そのもの**（末尾の余分な空白・改行、同一行への本文混在も非正規形式）であり、keyが許可集合内、必須key（key / kind / run / head / seq）が型・形状制約を満たし、kindが既知値で、**本文中の予約token出現がmarker行の1回のみ**であること。これ以外の予約token出現・形式は「Controller以外が付加したmarker」（条件2、ADR-0007決定1・2の履行）。この判定は`run`の値に関わらず行う（偽markerは正しいrunを名乗るとは限らない）
4. payloadの合成（`compose_record_marker_payload`）と解析はrecord_chain.pyに同居させ、round-trip property testで乖離を構造的に防ぐ。producer（C-08）はこのhelperだけでpayloadを構成する

### 7条件の検出trigger（AC-C06-06）

5. 検出条件と正規のtrigger: (1) **actor** = chain候補のauthorがproducer allowlistと完全一致しない（削除済みaccount = login欠如を含む）/ (2) **marker** = 正規形式以外の予約token出現 / (3) **tamper** = checkpointが記録したbody hashと現在観測の不一致 / (4) **edited** = `updatedAt != createdAt`（Controllerはrecordを編集しない）/ (5) **gap** = 検査範囲`1..max(観測最大seq, N)`内の欠番 / (6) **chain** = seq kのprevがseq k−1のbody_hashと不一致 / (7) **missing** = checkpointの既知comment IDがGitHubで404
6. 検証は**pure core**（`verify_record_chain`）と**I/O wrapper**（`probe_known_records`）に分離する。probeは取得窓に現れなかった既知IDだけをGETし、**NOT_FOUNDのみ**をviolation材料（`ProbeMissing`）にする。TRANSIENT / AUTH / PERMANENTは再raiseし、**一時障害・設定不備からviolationを捏造しない**。同じ原則で、producer allowlistの空集合は構築時`IdentityError`（設定誤りを全record不正actor化させない）、取得窓が既知IDを覆っていない入力は`IdentityError`（probe忘れ = 呼び出し誤り）
7. 二重報告の抑制: seq k−1が欠番・conflict・editedのとき、seq kのchain照合はskipする（1つの物理的事実を複数violationにしない）。ただし条件3（checkpoint hash照合）と条件4（編集timestamp）は独立の検出条件として両方報告する（検出根拠が異なる）。gapはseqの主張が観測されなかった場合のみ報告し、conflict / editedのseqはgapにしない

### Violation binding（C-01の`IntegrityEvidenceRef`への写像）

8. binding形式は`iv:{condition}:{run_id}:{subject}`。conditionは`actor` / `marker` / `tamper` / `edited` / `gap` / `chain` / `seqconflict` / `missing`、subjectはcomment基準の条件が`c{comment_id}`、seq基準の条件が`s{seq:08d}`（zero-padで昇順とlexicographic順が一致）。検出回・時刻・入力順序に依存しないため、**同一違反の再検出は同一binding（冪等）**であり、C-01の`canonicalize_integrity`（dedup keeps first）と両立する。衝突耐性: conditionは`:`を含まない固定集合、subjectは`:`を含まない固定形（comment IDは数字列）のため、文字列からの(condition, run, subject)の右端分解が一意になり、`run_id`が`:`を含んでも別入力が同一bindingへ写ることはない
9. descriptorはsorted keysのcompact JSON（type / seq / comment_id / expected / observed等。hashとIDのみで秘密値を含まない）とし、BLOCKED時の差分提示にそのまま使える。headは検出時の対象head（検証呼び出しの`detection_head`）
10. 同一(run, seq)の複数record: 全candidateの`(key, body_hash)`が一致する場合はtimeout再投稿の**良性重複**とし、`created_at`最小（tie: comment ID数値最小）を正として黙って選択する（violationにしない。ADR-0007決定7「重複canonical選択はC-06」の履行）。不一致は`seqconflict` violation（正を確定できないため全candidateをrecordsから除外する）
11. since境界の再配送は`comment_id`でdedupeし、**`updated_at`最大の観測**を採用する（ADR-0007決定17「(comment_id, updated_at)のdedupeは呼び出し側」の履行。最新観測の採用で編集検知が成立する）

### 保証範囲の限界（AC-C06-09）

12. `ChainVerification.assurance_high_water`（checkpointの`N`。無checkpointなら0）が保証境界を明示する: `N`以下の欠落は検出済み、`N`より後のtail truncationとcheckpoint喪失時のtail truncationは**検出しない**（gap検査範囲の上限が観測最大seqと`N`で決まるため、構造的に正常なconversationとして扱われる）。この負の保証はtestで常設検証する
13. 検証scopeは指定runのconversation comment（issue comment）のみ。過去runのchainは当該runのcheckpointの守備範囲であり、今回の検証対象にしない。review thread comment（GraphQL経由。timestampを持たない）はchain対象外とする — canonical recordはconversation comment経由でのみ投稿する
14. violationがある場合も`records`（検証を通過したrecord列）は差分提示のために返すが、consumerは`is_intact`でgateする（C-07以降の契約）

### ユーザー判断の受理（`identity/allowlist.py`。D-031）

15. **「完全一致」の解釈**: charset guard（`[A-Za-z0-9-]+`への適合。非適合は`INVALID`としてdeny）を通過したloginの**ASCII lowercase正規化後の等価**とする。GitHubのloginはcase-insensitiveに一意であり、正規化は受理集合を別のaccountへ広げない（false acceptを生まない）一方、設定の大文字小文字ズレによるfalse denyを防ぐ。正規化は集合構築側と照合側の両方で行う（ADR-0006判断11の委譲の履行）。charset guardによりUnicode casefold縮退（U+212A等）は持ち込まれない。C-04のtrusted_authors（実行gating用の別集合）はcase-sensitiveのまま変更しない
16. **取得不能と空集合の区別**: `AllowlistUnavailable`（未設定・取得失敗。AC-C06-02）と`DecisionAllowlist(空)`（常にdeny）は別の型で表現し、拒否理由（`ALLOWLIST_UNAVAILABLE` / `NOT_IN_ALLOWLIST`）を分けてtest可能にする。いずれもfail closed
17. **bot非受理の実装**: 安全境界はallowlist完全一致 + charset guardであり、`[bot]` suffixの分類（`BOT_ACTOR`）は拒否理由の可読性のためだけに置く（`[` / `]`はcharset外のためbotは構造的に一致し得ない）。REST応答の`user.type`をC-05へ露出する変更は行わない
18. **受理検査の順序**: allowlist取得不能 → actor解決（欠如 / bot / charset）→ allowlist照合 → **観測元照合** → 編集（`updatedAt != createdAt`）→ 消費済みcomment ID → 予約token出現（大小無視。ユーザーcommentがControl markerを含むことはない）。観測元照合は、GitHubが返した`html_url`（comment authorが操作できない観測値）から(repository, Issue / PR番号, comment ID)を導出し、期待contextとの完全一致（repositoryはGitHub上case-insensitiveのためcasefold等価）を要求する — 別repository / 別PRから取得したcommentを期待contextへbindする取り違えを構造的に防ぐ。URL形式は`https://github.com/{owner}/{repo}/{issues|pull}/{n}#issuecomment-{id}`（github.com前提。GHESはscope外の暫定）。併せて`DecisionContext`は構築時に形式検証する（repository=owner/name形式、番号は正、head SHAは40桁小文字hex、MERGE_APPROVALはmerge method必須、空文字列のbind対象を拒否）— 実質bindなしの承認を生成させない
19. **受理binding**: 区切り文字を含むopaque値でも衝突しないよう、`"ud:" + sorted keysのcompact JSON`（comment / fingerprint / head / kind / method / number / repository（casefold））で導出する（同一comment・同一contextの再受理は同一binding = 冪等）
20. **external evidence経路**: GitHubへの直接comment受理はPhase 1計画節5.2の2経路目であり、**PersistRecord（再投稿）を発行しない**。受理時にbody hashを記録し、`revalidate_user_decision`は再取得結果が**受理済みcommentそのもの**であること（comment ID・観測元・actor）を先に照合し（不一致は`VOIDED_SOURCE_MISMATCH`でfail closed）、編集（hash差または編集timestamp）/ 削除（404）/ binding不一致（head変更等）で失効させる。binding不一致の判定は再検証時のみ生じる（自由文commentからbindingを抽出することはできず、bindingは常にC-06が期待contextから導出するため）

### checkpoint envelope（ADR-0004のadditive追加）

21. `conversation`sectionへ`high_water_mark`（int）、`records[]`へ`seq`（int）/ `kind`（text）を追加する。既知recordの再構成（`KnownRecord`）は**seq付きentryのみ**を対象とし、Phase 5以前のseqなしentryはchain checkpointに関与しない。`decision`sectionへ`answer_comment_id` / `answer_body_hash`、`merge`sectionへ`approval_body_hash` / `candidate_fingerprint` / `approval_binding`を追加する（承認bind情報。すべてoptional、version bumpなし）

## Consequences

- C-08はrecord投稿時に`compose_record_marker_payload`で`seq` / `prev`を構成し、C-07はresume時に`verify_record_chain`の`is_intact`をgateにする。chain specの変更は本ADRの改訂を要する
- 書込権限を持つ第三者が予約tokenを含むcommentを書くだけで条件2のviolationとなりBLOCKED化するDoS面は、仕様が要求するfail closed（silent repairせず差分提示）の帰結として受け入れる（緩和はユーザー承認済みのsalvage手順 = C-07で行う）
- 参考実装のround_state（markerがあればauthorを確認せずrecordを復元する）は選択移植しない（reference assessment「再利用しない」判定の履行）。producer照合・chain検証・binding導出はすべて新規実装である
- merge method / candidate fingerprintの文字列形式はC-11 / C-13で確定するため、本Phaseでは不透明文字列として等価比較のみを行う
