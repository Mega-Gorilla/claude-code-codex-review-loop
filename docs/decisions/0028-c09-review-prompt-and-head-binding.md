<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0028: reviewer promptのfence（P-008）とhead bindingの三者照合（AC-C09-04）

- Status: Accepted（C-09の技術判断。AC-C09-04の文言は変更しない。contextの収集はC-10へ委ねる）
- Date: 2026-09-11

## Context

reviewerはsession memoryを引き継がず、Controllerが毎turn組み立てるpromptだけを入力にする（D-015）。promptへ埋め込むPR本文・comment・findingはGitHub由来の**外部入力**で、agentへの指示として解釈され得る（P-008）。またreview結果は特定のhead SHAへbindされ、headが変われば失効する（head binding）。AC-C09-04は「隔離checkoutのHEAD、PRのadvertised head、review出力の対象headが一致する」ことを求める。

Issue #14 §5により、canonical conversationとfinding ledgerの収集・選択はC-10の責務で、Phase 9はtypedな`ReviewContext`をportから受け取り、fakeのcontextでprompt構築を検証する。

## Decision

### prompt構築（`runtime/review_prompt.py`）

1. `ReviewContext`は`repository` / `number` / `target_head_sha` / `base_ref` / `title` / `round` / `instructions` / `materials`から成る。**`instructions`だけがController作**で、`repository` / `base_ref` / `title`と`materials`（`GitHubText(label, author_login, body)`）はGitHub由来として扱う。repository名とbase refは限定語彙でも**人が命名する文字列**であり（hyphen連結の指示文は有効なbranch名になる）、Controller領域には残さない。Controller領域に残すのは語彙が非意味的な値（head SHA、PR番号、round）だけである。
2. `ReviewContextPort.context_for(run_id, repository, number, head_sha, round)`がcontextを供給する。本実装はC-10で、それまでは`UnavailableReviewContext`が`PortUnavailableError`で停止する（既存portと同じfail closed）。
3. `build_review_prompt`は、GitHub由来のtextを**すべて**fenceの中へ置く（`repository` / `base_ref` / `pr_title`のlabelを持つblockを先頭に置く）。fenceは`<<<GITHUB_DATA:<boundary> label=<label> author=<login|unknown>` … `>>>GITHUB_DATA:<boundary>`で、boundaryは**呼出ごとにランダム**（`secrets.token_hex(16)`。testは固定値を渡す）。指示部はblockが「データであって指示ではない」こと、blockの中の文に従わないこと、報告へ対象head SHAをそのまま含めることを明記する。
4. 埋め込む前に改行を正規化し、制御文字を拒否し、C-04のredactionを通す（glossary「redaction」: prompt・log・artifactへ共通適用）。fenceの内側にboundaryまたはfence markerの接頭辞が現れる入力は`boundary_collision`で拒否する。ランダムなboundaryを知り得ないため通常は起きず、起きた場合はfenceを閉じて指示を続ける形の注入とみなす。
5. 形式検証（repository slug、正のnumber / round、40桁のhead、空白を含まない`base_ref`、非空の`instructions`、label / loginの語彙、材料数の上限）は固定stageの`PromptError`で停止し、本文を例外へ含めない。各入力はstrict UTF-8で符号化できることを要求し、unpaired surrogate等は入力のstageを持つ`PromptError`へ閉じる（生の`UnicodeEncodeError`を出さない）。
6. bytes上限は**2段**で適用する。正規化後・redaction前の入力bytesを全入力（instructions / repository / base_ref / title / materials）で累積し、`MAX_REVIEW_PROMPT_BYTES`を超えた時点で`size`で停止する（超過した入力はredactionへ渡さない）。最終promptのbytesにも同じ上限を適用する。前者が無いと、credential wrapperとしてredactされ短いmarkerへ縮む入力で上限を迂回でき、外部入力に対するresource上限にならない。

### head binding（`runtime/head_binding.py`、`runtime/checkout.observe_checkout_head`）

7. `observe_checkout_head`は隔離checkoutの`rev-parse HEAD`を**実際のrepositoryから**読む。作成時の`target_head_sha`を再利用しない（review中にHEADが動いた場合を検出するため）。checkout moduleのgit実行はC-03の`SpawnError`に加えて`StopError`（timeout後の停止・close失敗。native detailを持つ）も固定stageの`CheckoutError`へ写す。これは作成・release経路にも同時に適用される。
8. `verify_review_target(checkout_head, advertised_head, reported_head)`は純粋関数で、3つが一致した場合だけ`HeadsBound`を返す。形式不正（`*_invalid`）はそれ自体を理由にし、値の比較は3つとも正しい形式のときだけ行う。`advertised_moved` / `reported_differs`は固定語彙で、値の詳細を含めない。
9. `HeadMismatch`のときの**非投稿**（head race時に結果を投稿しない）は呼出側であるC-10の責務で、本moduleは判定だけを行う。advertised headの観測はC-05 `get_pull_request`（ADR-0012）を、reported headの抽出はC-10のreport parserを使う。

## 検証と完了境界

- fenceの構造（repository / base ref / title / 全materialがblock内、指示部にGitHub由来のtextが現れない。指示文に見えるrepository / base refもfence内に留まる）、boundaryの呼出ごとの差、redaction、boundary混入の拒否、形式検証、不正UTF-8の固定error、入力bytes上限（title / instructions / 単一material / 複数材料の合計 / multibyte / redactionで縮む入力 / 上限内の受理）と最終prompt上限、portのfail closedをhermetic testで固定
- `_git_output`が`SpawnError` / `StopError`のどちらも固定stageの`CheckoutError`へ写し、native detailを漏らさないことを固定
- `observe_checkout_head`は実gitで、HEADを動かした後に観測値が変わることを確認
- 三者照合は一致・各不一致・形式不正をtestで固定

本ADRはAC-C09-04の文言を変更しない。prompt本文の最終形（report schemaとの対応、言語解決）とcontextの収集はC-10 / C-12で決める。
