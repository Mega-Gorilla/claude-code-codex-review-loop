<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0031: reviewerのprovider認証材料の供給方式（OS credential storeと固定path・内容再生成のreviewer home）

- Status: Proposed（C-09の技術判断。D-032（Decided、2026-09-15）とAC-C09-06の実装方式を決める。レビューとユーザーの明示承認を経てAcceptedにする）
- Date: 2026-09-15

## Context

D-032の合意record（Issue #52）は次を定める。

- 同一providerのアカウントまたは認証元は、**明示設定がある場合に限り**共有可能とする。既定では共有しない
- これはcoderの設定領域（`~/.codex`等）や**認証fileをそのままreviewerへ渡すことを意味しない**
- AC-C09-06: reviewerがprovider認証を利用して正常に実行できる一方、model-generated commandからprovider認証材料を取得できないことをnegative testする。成立しない環境ではfail closedする

Codex CLI（0.154.0）の認証保存先は`cli_auth_credentials_store`で選ぶ（`file` = `CODEX_HOME/auth.json`、`keyring` = OSのcredential store、`auto`、`ephemeral`）。公式docsは`auth.json`をaccess tokenを含むpassword同等の機微情報として扱うよう求める。実測とsourceで次を確認した。

- keyringの保存keyは`CODEX_HOME`のcanonical pathのSHA-256（先頭16桁、service `Codex Auth`）で、**homeごとに独立**する（`login/src/auth/storage.rs` `compute_store_key`）。別homeへcredentialが暗黙に共有されることはない
- 専用homeに`cli_auth_credentials_store = "keyring"`を書くと、`codex doctor --json`は`auth storage mode: Keyring`、credential未登録なら`no Codex credentials were found`（fail）を報告する
- elevated Windows sandboxでは、sandbox内のcommandは別のlocal user（`CodexSandboxOffline`）で動き、専用homeのdeny root配下は読めない（ADR-0027 追補(3)、`credential_read=denied`）。Windows Credential Managerはuserごとに分離される

現在の設計（ADR-0027 決定12）はrunごとにuuid名の専用homeを作る。keyringのkeyはpathに依存するため、この設計のままではreviewerの認証をOS credential storeへ置けない。

## Decision

1. **reviewerの認証はOS credential storeへ置き、fileでは渡さない**。専用homeの生成configに`cli_auth_credentials_store = "keyring"`を固定する。`auth.json`は専用homeへ作らず、`~/.codex/auth.json`を複製しない。これにより「認証fileをそのまま渡さない」を構造で満たす。
2. **reviewer homeは固定pathで、内容はrunごとに再生成する**。呼出側が明示する`reviewer.codex.home`（private dirの下の固定path。既定値は無し）を`CODEX_HOME`とし、turn開始時に前回の内容を削除してから作り直す（config、provisioning成果物の複製、reviewer home）。keyringのkeyが安定し、`--ephemeral`と再生成でfresh reviewerを保つ。ADR-0027 決定12の「runごとに新しい名前」はこの範囲で置き換える。
3. **認証はユーザーの一度だけの明示操作で登録する**: `CODEX_HOME=<reviewer.codex.home> codex login`（またはdevice auth）。同一アカウントで登録するかは設定`reviewer.codex.account = "shared" | "separate"`で**申告**し、evidenceへ記録する。Controllerはアカウントの同一性を検証しない（keyringの内容を読まない）。申告が無ければ起動しない。
4. **起動前のauth gate**: 起動facadeはpreflightのdoctor出力から`auth.credentials`の状態と`auth storage mode`を読み、`Keyring`かつcredential登録済みでなければ`auth`で停止する。preflight（`codex sandbox` / `codex doctor`）自体は認証を要さず、credential-free canaryは従来どおり成立する。
5. **固定homeの排他**: turn開始時に`<home>.lock`をexclusive createで取得し、reviewer終了と破棄が済むまで保持する。取得できなければ`home_locked`で停止する（同一homeでの並行runを許さない。前runのcrashで残ったlockは、operatorが状態を確認して除去する運用とし、自動で奪わない）。
6. **AC-C09-06のnegative test**: 境界probeへ「credential storeの列挙」を追加する（Windows: `CredEnumerateW`で現在tokenが見えるcredentialの件数。sandbox userでは0件または列挙不可を`denied`とする）。専用home配下の読取拒否（既存の`credential_read`）と併せ、両方が`denied`でなければ`boundary`で停止する。POSIX backendの列挙方法は実測後に決める。
7. **配置と権限**: 固定homeとreviewer homeはprivate dir検証（決定12）をそのまま適用する。sandbox profileは従来どおり専用homeをdenyする。runtime成果物（`.sandbox-bin` / `cap_sid` / `tmp` / log）は次のrun開始時の再生成で消える。

## 採らなかった案

| 案 | 理由 |
| --- | --- |
| `~/.codex/auth.json`を専用homeへ複製 | 認証fileをそのまま渡すことになり合意recordに反する。token refreshで2つのhomeの内容が乖離し、写し戻しが要る |
| runごとのuuid homeへ短命のauth.jsonを配置 | 同上（fileでの供給）。refresh tokenの回転と写し戻しの問題も同じ |
| `OPENAI_API_KEY`をreviewer envへ渡す | C-04の`TOKEN_ENV_NAMES`で構造的に禁止（ADR-0027、Issue #14 §3） |
| `cli_auth_credentials_store = "ephemeral"` | process内memoryだけで、非対話の`codex exec`に登録手段が無い |
| 固定homeをそのまま再利用（再生成しない） | session・log・runtime成果物が残り、fresh reviewer（D-015）に反する |

## 検証と完了境界

- hermetic test: 生成configの`cli_auth_credentials_store`、固定homeの再生成と排他、auth gateの各状態（Keyring未登録 / File / 登録済み）、申告の記録
- 実機: credential-free canary（認証なし。preflightのみ）→ ユーザーが固定homeへloginした後のreal-auth canary（runtime承認。AC-C09-06のprobe結果と`codex exec`の完走）
- 本ADRはnetwork境界（firewall構成）を扱わない。D-032のnative adapter（4組み合わせ）はIssue #52で別途扱う
