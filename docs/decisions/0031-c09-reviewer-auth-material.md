<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0031: reviewerのprovider認証材料の供給方式（OS credential storeと固定path・内容再生成のreviewer home）

- Status: Accepted（C-09の技術判断。D-032（Decided、2026-09-15）とAC-C09-06の実装方式を決める。2026-09-15に[Issue #14の判断record](https://github.com/Mega-Gorilla/claude-code-codex-review-loop/issues/14#issuecomment-5679368555)でユーザーが承認。**本ADRの決定はWindows nativeのelevated backendに限定**し、Linux / SSHはOpen。方式の承認であり、real-auth canaryの成功やLinux / SSH対応の完了を意味しない）
- Date: 2026-09-15

## Context

D-032の合意record（Issue #52）は次を定める。

- 同一providerのアカウントまたは認証元は、**明示設定がある場合に限り**共有可能とする。既定では共有しない
- これはcoderの設定領域（`~/.codex`等）や**認証fileをそのままreviewerへ渡すことを意味しない**
- AC-C09-06: reviewerがprovider認証を利用して正常に実行できる一方、model-generated commandからprovider認証材料を取得できないことをnegative testする。成立しない環境ではfail closedする

Codex CLI（0.154.0）の認証保存先は`cli_auth_credentials_store`で選ぶ（`file` = `CODEX_HOME/auth.json`、`keyring` = OSのcredential store、`auto`、`ephemeral`）。公式docsは`auth.json`をaccess tokenを含むpassword同等の機微情報として扱うよう求め、`keyring`はOS credential storeが利用できなければ失敗する方式と定義する。実測とsourceで次を確認した。

- keyringの保存keyは`CODEX_HOME`のcanonical pathのSHA-256（先頭16桁、service `Codex Auth`）で、**homeごとに独立**する（`login/src/auth/storage.rs` `compute_store_key`、0.154.0）。別homeへcredentialが暗黙に共有されることはない
- 専用homeに`cli_auth_credentials_store = "keyring"`を書くと、`codex doctor --json`は`auth storage mode: Keyring`、credential未登録なら`no Codex credentials were found`（fail）を報告する
- elevated Windows sandboxでは、sandbox内のcommandは別のlocal user（`CodexSandboxOffline`）で動き、専用homeのdeny root配下は読めない（ADR-0027 追補(3)、`credential_read=denied`）。Windows Credential Managerはuserごとに分離される

現在の設計（ADR-0027 決定12）はrunごとにuuid名の専用homeを作る。keyringのkeyはpathに依存するため、この設計のままではreviewerの認証をOS credential storeへ置けない。

## Decision（Windows native、elevated backend）

1. **reviewerの認証はOS credential storeへ置き、fileでは渡さない**。専用homeの生成configに`cli_auth_credentials_store = "keyring"`を固定する。`auth.json`は専用homeへ作らず、`~/.codex/auth.json`を複製しない。これにより「認証fileをそのまま渡さない」を構造で満たす。
2. **reviewer homeは固定pathで、内容はrunごとに再生成する**。呼出側が明示する`reviewer.codex.home`（product所有のprivate parentの下の固定path。既定値は無し）を`CODEX_HOME`とし、turn開始時に前回の内容を削除してから作り直す（config、provisioning成果物の複製、reviewer home）。keyringのkeyが安定し、`--ephemeral`と再生成でfresh reviewerを保つ。ADR-0027 決定12の「runごとに新しい名前」はこの範囲で置き換える。
3. **初回登録はproduct所有のauth-setup手順で行い、順序を固定する**。ユーザーが素の`codex login`を実行する形は採らない（生成configが無い状態ではfile保存へ進み、禁止している`auth.json`が作られ得る）。手順は次の順で、途中で失敗した場合はcredentialを使用せず停止する（作成済みの専用homeは決定7の再生成対象として次回に消す）。
   1. 固定homeを決定7の検証つきで作成する（存在すれば決定7の順序で再生成）
   2. 管理下config（決定1の`keyring`を含む）を生成する
   3. `codex doctor --json`で`auth storage mode: Keyring`を確認する（`File` / `Auto`なら停止）
   4. `CODEX_HOME=<home>`を明示したenvで`codex login --device-auth`（または`codex login`）を**productが起動**し、ユーザーが認証する。argvは固定で、tokenをargv / envで渡さない
   5. 完了後に`auth.json`が存在しないこと、`codex doctor --json`の`auth.credentials`が`ok`かつ`auth storage mode: Keyring`であることを確認する。どちらかが満たされなければ停止し、`auth.json`が存在すれば削除して`auth_setup`の失敗として報告する
4. **アカウント共有は申告**である。設定`reviewer.codex.declared_account_mode = "shared" | "separate"`をユーザーが申告し、evidenceへ`declared_account_mode`として記録する。Controllerはアカウントの同一性を検証しない（keyringの内容を読まない）ため、監査記録では「確認済みaccount」とは書かない。申告が無ければ起動しない。
5. **起動前のauth gate**: 起動facadeはpreflightのdoctor出力から`auth.credentials`の状態と`auth storage mode`を読み、`ok`かつ`Keyring`でなければ`auth`で停止する。加えて`codex --version`が本ADRの実測version（0.154.0）と一致しない場合、keyring keyの導出（内部実装への依存）が変わっている可能性があるため、`auth_version`で停止し、対応versionの更新と再loginを案内する。preflight（`codex sandbox` / `codex doctor`）自体は認証を要さず、credential-free canaryは従来どおり成立する。
6. **AC-C09-06のnegative testはpositive controlを伴う**。境界probeへ「reviewer credentialのexact lookup」を追加する。対象はservice `Codex Auth`と固定homeから導出したkey（対応CLI versionへbind）で、列挙は使わない。
   - sandboxの**外**（positive control）: 同じprobeが対象credentialの**存在**を確認できること（秘密値・名前・件数を出力しない。結果は`present` / `absent`だけ）。`absent`ならprobeか登録が壊れているため`control_boundary`で停止する
   - sandboxの**内**: 同じ対象へのlookupが失敗すること（`denied`）。`present`なら`boundary`で停止する
   - 既存の`credential_read`（専用home配下の読取拒否）も引き続き要求する
7. **固定homeの再生成は、検証できたentryだけを、lock取得後に行う**。順序は次のとおりで、検証不能・不一致の時点で**削除せず**停止する（stage `home`）。
   1. sibling lock `<home>.lock`をexclusive createで取得する（取得できなければ`home_locked`。残留lockは自動で奪わず、operatorが状態を確認して除去する）
   2. `reviewer.codex.home`がproduct所有のprivate parentの直下にあること（canonical containment）、path上にsymlink / junction / reparse pointが無いこと、coderの`CODEX_HOME`（環境変数と`~/.codex`）・実repository・protected rootsのいずれとも同一・祖先・子孫でないことを確認する
   3. entryが存在する場合、product ownership marker（`<home>/.cc-review-reviewer-home.json`。product名と生成時のversionを持つ）が存在し検証できるときだけ削除する。markerが無い、または内容が検証できないentryは削除しない
   4. 削除後にprivate dirとして再作成し、markerを書き、config・provisioning成果物を生成する
   5. lockはreviewerの終了と破棄が済むまで保持する
8. **配置と権限**: 固定homeとreviewer homeはprivate dir検証（決定12）をそのまま適用する。sandbox profileは従来どおり専用homeをdenyする。runtime成果物（`.sandbox-bin` / `cap_sid` / `tmp` / log）は次のrun開始時の再生成で消える。

## Open（Linux / SSH）

target experienceはWindowsとLinux / SSHを対象にする。本ADRの成立根拠はWindows Credential Managerと別local sandbox userであり、POSIXでは次が未実測である。

- headless環境でのOS keyring（secret service等）の可用性。利用不能なら公式仕様どおり失敗し、AC-C09-06のとおりfail closedとする
- sandbox内のcommand（同一OS user）がcredential serviceへ到達できないこと。到達できる場合は本方式を採れない

POSIXの方式（keyringの可否、sandbox内からの到達不能、positive control）は実測後に別途確定する。それまでPOSIXでは決定5のauth gateが`auth_platform`で停止する。

## 採らなかった案

| 案 | 理由 |
| --- | --- |
| `~/.codex/auth.json`を専用homeへ複製 | 認証fileをそのまま渡すことになり合意recordに反する。token refreshで2つのhomeの内容が乖離し、写し戻しが要る |
| runごとのuuid homeへ短命のauth.jsonを配置 | 同上（fileでの供給）。refresh tokenの回転と写し戻しの問題も同じ |
| `OPENAI_API_KEY`（または`CODEX_API_KEY` / `CODEX_ACCESS_TOKEN`等のCodex用alias）をreviewer envやauth-setupのenvへ渡す | C-04の`TOKEN_ENV_NAMES`で構造的に禁止（ADR-0027、Issue #14 §3）。aliasは対応CLI version（0.154.0）のbinaryとloginのhelpから棚卸しして同registryへ含める |
| `cli_auth_credentials_store = "ephemeral"` | process内memoryだけで、非対話の`codex exec`に登録手段が無い |
| 固定homeをそのまま再利用（再生成しない） | session・log・runtime成果物が残り、fresh reviewer（D-015）に反する |
| ユーザーが素の`codex login`で登録 | 生成configが無い状態ではfile保存へ進み得る（決定3） |

## 依存と順序

- D-032のDecided化（PR #76）はmerge済みで、本ADRはその上に置く
- keyring keyの導出（path hash）はCLI 0.154.0の内部実装への依存であり、決定5のversion bindで変更を検知する

## 検証と完了境界

- hermetic test: 生成configの`cli_auth_credentials_store`、auth-setupの順序と各段の停止、固定homeの検証順序（lock → containment → marker → 削除）と削除しない条件、auth gateの各状態（Keyring未登録 / File / 登録済み / version不一致 / POSIX）、申告の記録
- 実機（Windows）: credential-free canary（認証なし。preflightのみ）→ auth-setupで固定homeへ登録した後のreal-auth canary（runtime承認。決定6のprobe結果と`codex exec`の完走）
- 本ADRはnetwork境界（firewall構成）を扱わない。D-032のnative adapter（4組み合わせ）はIssue #52で別途扱う
