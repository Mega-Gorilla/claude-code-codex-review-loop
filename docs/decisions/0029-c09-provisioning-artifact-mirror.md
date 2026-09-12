<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0029: reviewer用`CODEX_HOME`へのelevated sandbox provisioning成果物の複製（案D）

- Status: Accepted（D-033の実装上の技術判断。ユーザーは2026-09-12にチャットで案Dに同意し、[Issue #14の整理comment](https://github.com/Mega-Gorilla/claude-code-codex-review-loop/issues/14#issuecomment-5645824357)に記録した。D-032 / D-033の文言は変更しない）
- Date: 2026-09-12

## Context

D-033（Decided）は、Windows nativeのreviewer sandboxにelevated backendを必須とし、preflightが強制を実測できなければ起動しないと定める。ADR-0027 追補の実測で次が判明した。

- elevated backendのprovisioning完了判定は`CODEX_HOME`ごとの2 file（`.sandbox/setup_marker.json`、`.sandbox-secrets/sandbox_users.json`）のversion一致で決まる（openai/codex `identity.rs` `sandbox_setup_is_complete`）
- provisioning（`codex sandbox setup --elevated`と、未provisioning homeでの自動setup）は実行のたびにsandbox userのrandom passwordを生成して既存userへ上書きし、対象homeへsecretsを書く（`sandbox_users.rs` `provision_sandbox_users`）。したがって同一machineでprovisioning済みにできるhomeは実質1つで、別homeを追加provisioningすると`~/.codex`側が失効し、以後は互いを壊し合う
- runごとに新しい専用`CODEX_HOME`を作る現在の設計（ADR-0027 決定12）は、そのままでは`sandbox provisioning: incomplete`となり`sandbox_unavailable`で停止する

案Dの検証（ADR-0027 追補(3)）では、`~/.codex`の成果物2 fileを専用homeへ複製するだけで`provisioning: complete`になり、preflightがelevated sandbox内で境界probeを起動でき、`~/.codex`は無傷だった。

## Decision

1. **複製する成果物は2 fileだけ**とする。`<authoritative home>/.sandbox/setup_marker.json`と`<authoritative home>/.sandbox-secrets/sandbox_users.json`を、専用homeの同じ相対pathへ複製する。`auth.json`、`config.toml`、session、log、`.sandbox-bin`、`.sandbox`配下の他fileは複製しない。複製はWindowsでだけ行う（POSIX backendは未実測で、provisioningの概念が無い）。
2. **複製元（authoritative home）は呼出側が明示する**。runtime moduleは与えられたpathを検証するだけで、`~/.codex`や`CODEX_HOME`環境変数を暗黙に探索しない。既定値の解決（hostの`CODEX_HOME`、無ければ`~/.codex`を推奨）はC-12の設定解決で決め、未設定時はWindowsでは複製を行わずpreflightが`sandbox_unavailable`で停止する（fail closed）。
3. **複製前の検証**（固定stage `provisioning_source`で停止）: 両fileが通常file（symlink不可）、UTF-8のJSON object、`version`が正の整数、markerの`offline_username` / `online_username`が非空、`proxy_ports`が空、`allow_local_binding`が`false`。専用configはproxyを設定しないため、markerにproxy portがあるとCodexがsetup driftとみなして再provisioning（password回転の起点）を試み得る。secretsの中身（password blob）は検証・復号・logしない。
4. **複製fileはowner限定権限**（`write_private_text`相当）で置く。sandbox user（`CodexSandboxUsers`）からは読めない。`~/.codex`側の`.sandbox-secrets`もdeny ACEで同じ扱いであり、曝露範囲は広がらない。
5. **順序保証**: preflight（ADR-0027 決定13 / 16）は`codex doctor --json`（読取のみ）で`sandbox backend: elevated`と`sandbox provisioning: complete`を確認してから`codex sandbox` / `codex exec`を起動する。`incomplete`なら`sandbox_unavailable`で停止し、Codexの自動elevated setupを起動させない。これが案Dで`~/.codex`を壊さないための唯一の保証点であり、doctorより前に`codex sandbox` / `codex exec`を呼ぶ経路を作らない。
6. **version drift**: Codexの更新でmarker / users fileのversionが変わると、複製homeは`incomplete`になり停止する。復旧はユーザーがCodexを通常利用して`~/.codex`を再provisioning（UAC）した後、次のrunが新しい成果物を複製することで自動的に済む。runtimeは自動で再provisioningを試みない。
7. **用語**: 複製後の専用homeはsandbox provisioning材料（sandbox userのOS password。DPAPI machine scopeで暗号化され、同一machine上の同一userのprocessは復号できる）を含む。これはprovider認証ではない。専用homeの不変条件は「**provider認証を含まない**」とし、「credentialを含まない」とは書かない（ADR-0027 追補で用語を改めた）。
8. **runごとの破棄**: 専用homeはrunごとに作って捨てる（決定12を維持）。固定homeで必要になる排他lock・atomic replace・残留回収は不要である。実行後にCodexが専用homeへ生成するruntime成果物（`.sandbox-bin`、`cap_sid`、`tmp`）は破棄の対象で、`~/.codex`へは書かない。

## 採らなかった案

| 案 | 理由 |
| --- | --- |
| 固定reviewer homeを追加provisioning | password回転で`~/.codex`と互いに壊し合う（Context） |
| `~/.codex`をreviewerの`CODEX_HOME`にする | coderのprovider認証（`auth.json`）・session・configを共有し、AC-C09-05 / AC-C09-06案とD-032案の権限分離に反する |
| `~/.codex`の`.sandbox` / `.sandbox-secrets`へのjunction / symlink | Codexが`.sandbox`配下へlogやruntime成果物を書くため`~/.codex`側を変更してしまう。決定12のprivate home検証はsymlinkを拒否する |
| profileの`:root`をdenyから緩める | AC-C09-05のcredential隔離とD-033の趣旨に反する |

## 検証と完了境界

- hermetic test: fakeの成果物でversion / usernames / proxy_ports / allow_local_binding / symlink / 非JSONの各拒否、複製後のpath・権限、POSIXで複製しないこと、複製元未指定時にWindowsで複製しないこと
- 実機: ADR-0027 追補(3)。複製で`complete`、`~/.codex`は実行前後とも`complete`、境界probeが起動して`boundary`まで到達
- 本ADRはnetwork境界（firewall構成依存）を解決しない。network deny不成立時のfail closedはD-033どおり維持する
- provider認証材料の供給方式（Issue #14 §3）は本ADRの範囲外で、D-032の緩和判断と合わせて別ADRで決める
