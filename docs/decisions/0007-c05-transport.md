<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0007: C-05 GitHub transportの規約

- Status: Accepted
- Date: 2026-08-21

## Context

implementation planはC-05を「未検証のGitHub metadataの取得・投稿・read-after-write確認・thread操作」に限定し（認証・canonical record判定はC-06）、AC-C05-01〜05を完了条件とする。取得の`--json`構造境界（P-004）、投稿本文のfile経由（P-005）、error分類の構造化（P-003）は原則で確定しているが、予約markerの形式、body hashの規約、冪等post flowの詳細、`gh`呼び出しの具体的方式、error分類表はPhase 5で決定する技術判断である。gh CLIのflag仕様（`-F body=@file` / `--include` / `--paginate` / GraphQL error形状 / exit code）は`gh 2.79.0`の実出力で検証した。本ADRはC-06が同じ規約で検証を行う共有仕様点（marker・hash）を含む。

## Decision

### 予約marker（`transport/marker.py`。C-06と共有する仕様点）

1. 予約markerの名称を`CC_REVIEW_META`として正式化する（従来はresearch / non-normative exampleのみに現れていた）。形式は本文**末尾1行**のHTML comment `<!-- CC_REVIEW_META:v1 {compact JSON} -->`。payloadはsorted keysのcompact JSONで**2048 bytes以下**、keyは許可集合（`key` / `kind` / `run` / `head` / `seq` / `prev`）のみ、値はstr / intのみ。大きな本文をpayloadへ格納しない
2. **markerの付加はControllerのみ**。agent生成本文中の予約token（大小無視）は投稿前に`CC~REVIEW~META`へ置換してescapeする（AC-C05-04）。この置換は単一passで不動点になる（replacementの接頭辞 / 接尾辞がtokenの接尾辞 / 接頭辞と一致しないため、境界を跨いだ新規tokenが生成されない。property testで常設検証）。C-06は「末尾1行の正規形式以外のmarker」をController以外の付加として判定する
3. 適用順序は**sanitize（marker escape）→ redact（C-04）→ render（発言者・model明示）→ marker attach → 投稿**。markerをredactへ通さない（redactのwrapper patternがmarker JSONを破壊するriskの排除）。C-05は独自のredaction patternを持たない

### body hashとread-after-write

4. 本文hashは**SHA-256 hex（UTF-8生bytes、正規化なし、marker含む全body）**。投稿前に改行を`\n`へ正規化した本文をfileへ書き、そのbytesのhashを期待値とする。read-after-writeは**comment IDでのGET直接取得**で行い、hash一致・comment ID・URL・head SHAの記録をもってturnをcompletedにできる（AC-C05-01）。hash不一致は編集・改変の疑いとして`PostHashMismatch`で返し、retryしない（呼び出し側がBLOCKED化）
5. 本文はGitHubの上限（65,536字）を投稿前に検査し、超過はPERMANENTとして投稿しない

### 冪等post flow（AC-C05-02）

6. 投稿のtimeoutおよびTRANSIENT失敗は**成否不明**として扱い、blind retryしない。idempotency marker（payloadの`key`）でGitHubを検索し、見つかれば確認のみ、無ければ**同一key**で再投稿する。検索は`since=(投稿開始時刻 − 時計skew余裕)`を起点に**bounded N回**（backoff付き）行う
7. **検索のpredicateは「marker key一致 AND body hash一致」**。書込権限を持つ第三者が同一keyのmarkerを偽造して再投稿を抑止する攻撃を無効化する（真正性の最終判定はC-06のallowlist照合）。事後に重複が発覚した場合のcanonical選択はC-06の責務であり、**C-05はcommentを削除しない**（mutable anchor更新方式の不採用と整合）
8. replyの冪等flowも同型（timeout → thread再取得 → 同一thread内でkey+hash検索 → 確認 or 再投稿）

### `gh`呼び出し規約（`transport/gh.py`）

9. 実行はC-03の`run_tree`経由（explicit env・argv list・stdout/stderrはworkdir内0600 file・stdin=DEVNULL）。**`gh_command`はargv prefix tupleとして注入可能**（本番は絶対pathの`gh`、testは`(sys.executable, fake_gh.py)`）。先頭は絶対path必須（envが非継承でPATH解決に依存しないため）。argvは常に`ensure_argv_allowed`を通す（P-006のruntime choke。C-05が最初の接続先）
10. 全`gh api`呼び出しへ`--include`を付加し、**HTTP status行とheader（lowercase正規化）を構造化取得**する（P-003の分類根拠。`--include`はrun_gh_apiが常に付加し、付け忘れを構造的に防ぐ）。投稿bodyは`-F body=@file`（file内容が文字列値として送られることを実測確認。`--input`はstdin依存の形があるため不採用）
11. **paginationは自前のpage loop**（`per_page=100&page=N`）。`--paginate`は複数JSON documentの連結を出力し単一JSONとして壊れるため使わない。継続判定はLink headerの`rel="next"`の**有無のみ**を使い、server提供URLをargvへ渡さない（応答値の注入面の排除）。max_pagesは必須引数で、超過はsilent truncationせずerror
12. retryは**TRANSIENTのみ**のbounded retry。待機は`Retry-After` → `x-ratelimit-reset − now` → 固定backoffの優先順で、**max_wait超過は眠らず即座に諦める**（primary rate limitのresetは1時間先があり得る）。sleep / nowは注入可能（test決定論化）。timeoutはretryせず冪等flowが回復する。timeout・retry回数・backoffの既定値はC-05は持たない（必須引数。C-12で解決）

### error分類（`errors.py`）

13. 分類表: exit 4→AUTH（request前の認証失敗。status不在でも確定）/ exit 2→PERMANENT / 401→AUTH / **403は（Retry-After有 or `x-ratelimit-remaining`==0）のみTRANSIENT、他はAUTH** / 404・410→NOT_FOUND / 409・429・5xx→TRANSIENT / 422・他4xx→PERMANENT / **status行なし+exit 1（network断等）→TRANSIENT**（bounded retryが誤分類の被害を有限化する判断）。GraphQLは**HTTP 200 + exit 1 + `errors[].type`**（構造化fieldの完全一致: RATE_LIMITED→TRANSIENT / NOT_FOUND→NOT_FOUND / FORBIDDEN→AUTH / 他→PERMANENT）
14. `errors.py`（package root）はP-003の分類語彙（`ErrorCategory`）と純粋な分類関数の正本とする。component固有の例外は各component側（transportは`TransportError`系）

### thread操作（AC-C05-03）

15. 取得はGraphQL（`isResolved`はRESTに無い）。thread levelはcursor loop（max_pages有界）、**thread内commentsの未取得の続きは`truncated`で顕在化**する（内側paginationはv1では実装しない）。**reply対象はthread先頭comment（top-level）のdatabaseId**（reply IDへのreplyはAPIが拒否する）。replyはREST replies endpoint + body file
16. **fallbackは恒久分類（NOT_FOUND / PERMANENT / AUTH）のみ**: 元comment URLを前置したconversation commentへ切り替え、経路を型（DIRECT_REPLY / FALLBACK_COMMENT）で返す。TRANSIENTの尽きはfallbackせず伝播する（恒久 / 一時の混同を避け、FAILED化は呼び出し側）

### 未実装・先送り（記録）

17. review objectの作成（review bodyでの投稿・approve / request changes）はC-10のreview loopで実装する（本transport primitivesで拡張可能）。thread内commentsの内側pagination、`since`検索のdedupe（(comment_id, updated_at)）は呼び出し側 / 後続Phaseの責務。checkpoint envelopeへは`review_id` / `thread_id`をoptional additive追加した（ADR-0004、version bumpなし）

## Consequences

- C-06はC-05の`UnverifiedComment`（加工なしのcreatedAt / updatedAt / author login / body / marker）だけで編集検知・payload hash照合・404検知が成立する
- C-01の`PersistRecord`（冪等な永続化）は`ensure_comment_posted` / `ensure_thread_reply`がそのまま実装になる。C-05はC-01 eventのproducerにならない（VERIFIED系はC-06が生成する）
- fake gh（P-011）は`--include`形式・exit code・`-F body=@file`のfile読みを実仕様どおり模倣し、全ACをlive接続なしで検証する

## 実装への反映

`src/claude_code_codex_review_loop/errors.py`、`src/claude_code_codex_review_loop/transport/`（gh / marker / render / conversation / threads）、`tests/c05_support/`（fake gh）、`tests/test_c05_*.py`が本ADRを実装する。
