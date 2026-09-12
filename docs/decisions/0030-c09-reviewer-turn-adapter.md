<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0030: C-09 reviewer turn adapter（checkout → prompt → 起動 → head bindingの固定順序）

- Status: Accepted（C-09の技術判断。AC-C09-01〜05の文言は変更しない。engineへの接続はC-10）
- Date: 2026-09-12

## Context

Phase 9の部品（隔離checkout ADR-0027 決定1〜5、専用`CODEX_HOME` 決定6〜12、preflight 決定13 / 14 / 16 / 17、起動facade 決定15、prompt fenceとhead binding ADR-0028、provisioning成果物の複製 ADR-0029）は単体で検証済みだが、実装codeから相互に呼ばれる本番経路が無かった。部品の**呼ぶ順序**は安全性の一部で（例: preflightより前に`codex exec`を呼ばない、checkoutは失敗しても必ず破棄する）、呼出側に組み立てを委ねると順序を崩す入口になる。

## Decision

1. `runtime/reviewer_turn.py`の`run_reviewer_turn(request, *, report_head)`が1 turnを**固定順序**で実行する: prompt構築（純粋）→ reviewer専用home / env（C-06）→ 隔離checkout → 専用`CODEX_HOME`（workspace = 隔離checkout、protected = 実repository + 呼出側のroot）→ Windowsではprovisioning成果物の複製（ADR-0029。`provisioning_source`が無ければ複製せず、preflightがfail closedする）→ 起動facade（preflightを同じ呼出の中で行う）→ 隔離checkoutのHEADの再観測 → checkoutの破棄 → **GitHub上のadvertised headの再観測**（`AdvertisedHeadPort`。本実装はC-05の`get_pull_request`、ADR-0012）→ 三者照合。起動前の`advertised_head`はsnapshotで、review中にPRへpushされたheadは起動前の値では見えないため、投稿判断の直前にportで取り直した値を照合に使う。
2. **`run_root`は実repository・protected rootと同一・祖先・子孫のいずれでもない**ことを、prompt / home / checkoutを作る前にcanonical pathで検証する（`run_root`）。重なりを後段のcanary home検証に委ねると、その前に保護対象の中へreviewer homeやcheckoutを作ってしまう。
3. **advertised headが起動前に動いていれば**（`advertised_head != context.target_head_sha`）checkoutを作る前に`head:advertised_moved`で停止する。reviewを走らせても投稿できないためである。
4. **checkoutは失敗経路でも必ず破棄を試みる**。破棄の失敗は元の失敗理由を置き換えない（root内に限られる）。成功経路での破棄失敗は`checkout:release`で停止する。dirty stateは拒否理由ではなくevidence（`CheckoutRelease.dirty`）として返す（AC-C09-01）。
5. 失敗は`TurnError(stage)`で、stageは`<部品>:<部品のstage>`（`prompt:` / `checkout:` / `canary:` / `provisioning:` / `preflight:` / `launch:`）または本module固有（`run_root` / `reviewer_home` / `evidence_root` / `head:advertised_moved`）。本文・path・native出力を含めない。
6. **reportからの対象head抽出は`ReportHeadPort`**（本実装はC-10のreport parser）、**advertised headの再観測は`AdvertisedHeadPort`**（本実装はC-10がC-05の`get_pull_request`で組む）で、それまでは`UnavailableReportHead` / `UnavailableAdvertisedHead`が`PortUnavailableError`で停止する。timeoutや抽出失敗で対象headが無い場合は空文字を渡し、`reported_invalid`の`HeadMismatch`になる。`HeadMismatch`時の非投稿はC-10の責務で、本moduleは判定を返すだけである。
7. `ReviewerTurnRequest`は**すべて明示値**で既定値を持たない。`run_root`は呼出側が所有するprivate dirで、turn後にadapterは削除しない（専用home・reviewer home・evidence rootが残り、呼出側がevidenceを回収してから破棄する）。argv・probe・接続先を注入する入口は無い。
8. 結果`ReviewerTurn`はprompt本文を含めない（起動facadeがevidence root配下へ私有fileとして書く）。boundaryとredaction hits、起動結果、観測したHEAD、取り直したadvertised head、照合結果、dirty state、複製の記録、evidence rootを返す。

## 検証と完了境界

- checkoutは実gitで作り、起動facadeはfakeへ差し替える（facadeの実挙動は`test_c09_codex_launch.py`が固定）。実Codex・実GitHub・実`~/.codex`は使わない
- 固定順序と渡す値（workspace = checkout、protected roots、fence付きprompt、token非到達のenv、evidence root）、同一headへの2回目の独立性（AC-C09-03）、review中の隔離checkoutのHEAD移動とPRへのpush（advertised headのold→new）の検出（AC-C09-04）、保護対象と重なる`run_root`の事前拒否と保護対象の不変、dirty stateの報告、各失敗経路でのcheckout破棄、破棄失敗が元の理由を置き換えないこと、portのfail closed、既定値と注入口の不在をhermetic testで固定
- engineへの接続（`RequestCodexReview`の実行、`ReviewContext`の収集、reportの受理と投稿、round管理）はC-10で、本ADRは扱わない
- 実Codexでのturn完走はcredential-free canary（Issue #14）で確認する。この機ではnetwork境界（firewall構成依存）でpreflightが停止する
