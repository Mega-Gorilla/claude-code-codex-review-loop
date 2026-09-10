<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0027: 隔離checkoutとCodex起動契約、canaryの2段階分割

- Status: Accepted（C-09の技術判断。Windowsのsandbox水準はOpenのまま。AC-C09-01〜05の文言は変更しない）
- Date: 2026-09-09

## Context

Issue #14は「実装前に固定する契約」を`codex-cli 0.149.1`の実測で書き、方式の具体化を着手時のplanへ委ねた。PR #56（reviewer用の独立checkout）とPR #57（canary harness第1段階）はその具体化だが、Issue #14の契約表から意図的に逸れた判断が3つあり、ADRに記録されていなかった。またPR #57のレビューで「builderは安全保証を主張しない」と正本へ書いた結果、fail-closed要件の実装先を明示する必要が生じた。

本ADRは`codex-cli 0.153.4`（Windows 11、非昇格user）で再実測した結果に基づく。CLIは更新されるため、後続PRは`--help`と`codex doctor --json`の出力を証跡として再確認する。

## Decision

### 隔離checkout（PR #56）

1. `git worktree`を使わず、private root配下へ`git clone --no-local --no-checkout`で**独立した一時repository**を作り、指定した40桁のGit object IDへdetached checkoutする。`.git`はdirectoryであり、`objects/info/alternates`が存在しないことを検証する。
2. clone直後に`origin`を削除し、`git remote`が空であることを検証する。`file://`とlocal pathへのpushは認証なしで成功する（ADR-0009 決定7〜9）ため、remoteの除去はAC-C09-02の必須防御である。
3. `rev-parse HEAD`が対象SHAと一致し、`rev-parse --abbrev-ref HEAD`が`HEAD`（detached）であることを検証する。不一致・remote残存・alternates存在は固定stageの構造化errorで停止する。
4. review終了時はdirty state（reviewerの一時書込）を観測してから、自分が作成したrootだけを破棄する。Windowsでcloneが付けたread-only属性はsymlinkを辿らずに外してから削除し、削除不能は元の作成・検証errorを置き換えずに報告する。
5. checkoutはCodexを起動せず、promptや認証材料を受け取らない。reviewer envはC-06のexplicit credential隔離env（`build_reviewer_env`）を受け取る。

### Codex起動契約（PR #57）

6. sandboxは**`default_permissions`と`[permissions.<name>]`だけ**で構成し、`-s / --sandbox`と旧`sandbox_mode` / `sandbox_workspace_write`を使わない。公式契約上、旧sandbox設定が**いずれかの読込済みconfigに1つでもあれば**permission profileは無視されるため、両者の併用は境界を壊す。Issue #14 §2の表が`-s`を挙げていたのは0.149.1時点の確認済みflagの列挙であり、本ADRで方式を置き換える。
7. `--ignore-user-config`を**使わない**。専用`CODEX_HOME`の`config.toml`は**本製品が供給するuser / project層のpolicy source**であり、同flagはそれを読まなくする。代わりに`CODEX_HOME`自体をcredentialを含まないprivate directoryへ差し替え、user / project execpolicyは`--ignore-rules`で遮断する。ただし隔離されるのはuser / project層だけで、**system config**（Unixは`/etc/codex/config.toml`。Windowsの所在は公式文書に無い）と**managed requirements**（`requirements.toml`。`allowed_permission_profiles`でprofileを制限し、特定のapproval policyを禁止できる）は別layerとして残る。そのlayerに旧sandbox設定が1つでもあればprofileは無視されるため、専用configだけを見てeffective policyを断定しない（決定13）。
8. 隔離checkoutを生成configで`[projects."<canonical path>"] trust_level = "untrusted"`へ固定する。trusted projectの`.codex/config.toml`はuser configより優先され、旧sandbox設定を含めばprofileごと置き換わる。未登録projectは現版では読み込まれないが、「未登録の既定」に依存しない。`trust_level`の値域は`trusted` / `untrusted`の2値で、不正値はconfig loadを失敗させる。
9. argvは`<codex> exec --ephemeral --ignore-rules -C <checkout> -`に固定し、promptはstdinで渡す。任意argv・`-c`上書き・`--add-dir`・`-p`の入口をAPIに持たせない。既存のargv choke point（P-006）へCodex固有の禁止語彙（approval / sandbox迂回、full access、`shell_environment_policy.inherit=all`、`sandbox_permissions`のfull read）を追加し、`-c`で到達できる既知の危険値も拒否する。
10. reviewer envにC-04の`TOKEN_ENV_NAMES`が1つでもあれば構成を拒否し、値は診断へ出さない。生成configはSHA-256 digestで再照合し、改竄・metadata不整合は起動前に停止する。
11. approval policyは生成configのtop-levelで`approval_policy = "never"`に固定する（**次のprocess facade PRで追加**）。`-a / --ask-for-approval`は0.153.4では**top-level option**であり、`codex -a never exec`は受理されるが`codex exec -a never`は引数errorになる（Issue #14 §2の表は`exec`のoptionとして挙げており古い）。builderのargvは`<codex> exec ...`で`exec`より前にoptionを置かない固定形なので、flagではなく管理下configで固定する。非対話実行では`never`を使うという公式案内に合わせ、他の値は採らない。untrusted projectの既定は`UnlessTrusted`だが、top-levelの明示値は`Never`へ解決されることを`codex doctor --json`で確認した。`never`はsandboxを唯一の強制点にする設定であり、**preflightで強制が成立し、effective configのapproval policyが`Never`である場合にだけ**許される。managed requirementsが`never`を禁止していればeffective configがそれを示すため、起動しない。

### canaryの2段階分割

12. **第1段階（PR #57）は純粋builder**であり、設定とargvを構築するだけでsandboxの強制を保証しない。呼出側からの「実測済み」申告値を受け取る入口を持たない（未検証boolは保証の偽装口になる）。
13. **第2段階（process facade PR）がpreflightを担い、次の2つを両方必須にする**。片方はもう片方の代替にならない。
    - **effective configの照合**: 実起動と同じcanonical executable・同じ`CODEX_HOME`・同じcwd（隔離checkout）で`codex doctor --json`（または同等のeffective-config出力）を取得し、config loadが成功、approval policyが`Never`、filesystem / network sandboxが`restricted`、denied-read restrictionsが有効、旧sandbox設定によるfallbackが無いことを確認する。これは「probeしたprofileが実起動でも選ばれる」ことの確認であり、system / managed layerを含めた解決結果を同じCLIから得る。
    - **OS強制の実測**: `codex sandbox -P <profile> --include-managed-config -C <checkout> -- <probe>`を専用`CODEX_HOME`で実行し、隔離checkoutへの書込成功、protected rootへの書込失敗、credential領域の読取失敗、shell networkの失敗を実測する。`--include-managed-config`はmanaged requirementsを含めてprofileを解決するoptionで、実起動と同じstackで測るために付ける。`codex sandbox`は認証を要求しないため、この実測はcredentialなしで行える。

    どちらか1つでも成立しなければspawn前にfail closedする。managed layerの所在をfile pathで探索・検出しようとはしない（Windowsでは文書化されていない）。effective configが専用configと異なるprofile・approval・sandboxを示した時点で差異ありとみなし、起動しない。

    **観測できる範囲の限界**（2026-09-10実測）: 0.153.4の`codex doctor --json`は、configに旧`sandbox_mode`を足しても`sandbox.helpers`の出力を変えない。したがって(a)で確認できるのは上記の観測可能なfieldまでで、system / managed layerに旧sandbox設定がある場合のfallbackは(a)では検出できない。専用config自体に旧keyが無いことはdigestで保証し、残る残余は起動PRで`codex/sandbox-state-meta`相当のeffective sandbox stateを取得するか、起動直後の自己検査で閉じる。また(b)のprobeは隔離checkout内でinterpreterを起動する必要があり、profileの`:minimal`（"General platform and runtime paths needed by common tools"）がそのinterpreterを含まなければprobe自体が起動できない。その場合も`probe_unavailable`としてfail closedし、profileへ読取許可を足すかどうかはelevated backendの実測後に決める。
14. preflightのevidenceはfacadeが取得した実測だけを認め、呼出側の申告値・過去の実測・configの解釈結果だけの確認で代替しない。evidenceは少なくとも**canonical executable pathと`codex --version`の出力、config digest（`CodexCanaryHome.configuration_digest`）、profile名、workspace root、protected roots、`CODEX_HOME`、reviewer envのdigest**にbindし、spawn直前に同じ条件を再検証して1つでも違えば起動しない。evidenceはreview turnごとに取り直し、前のturnの結果を再利用しない（fresh reviewer）。実測結果は固定stageで公開し、native出力は共通redaction registryを通す。

## 実測（2026-09-09、codex-cli 0.153.4、Windows 11 非昇格user）

| profile | `codex sandbox`の結果 |
| --- | --- |
| 生成config（`:root` / `:tmpdir` / `:slash_tmp` / protected rootをdeny） | 適用不可: `Restricted read-only access requires the elevated Windows sandbox backend` |
| deny entryを全て`read`へ緩めた変種（workspace write、`network.enabled = false`） | workspace外への書込: **拒否**。protected root・`CODEX_HOME/config.toml`の読取: **許可**。TCP 443: **許可**（`--sandbox-state-disable-network`を付けても許可） |

非昇格backendが強制するのは**filesystemの書込制限だけ**である。read denyとnetwork禁止はelevated backendを要求する（`codex doctor --json`の`sandbox.helpers`も`managed denied-read requirements need the elevated Windows sandbox backend`と報告する）。生成configはこの環境で適用できないため、決定13のpreflightは**fail closedになる**。これは設計どおりの挙動であり、緩めるかどうかは次節のOpenである。

approval policyの配置は次のとおり確認した。

| 呼び方 | 結果 |
| --- | --- |
| `codex exec -a never --help` | 引数error（exit 2） |
| `codex -a never exec --help` | 受理（exit 0） |
| 生成configのtop-levelに`approval_policy = "never"` | `codex doctor --json`のapproval policyが`Never`（untrusted project固定のまま） |
| 同上を`[projects.<checkout>]`の中に置く | 無視され`UnlessTrusted`のまま（table内のkeyはtop-levelではない） |
| 生成configのtop-levelへ旧`sandbox_mode = "workspace-write"`を追加 | `codex doctor --json`の`sandbox.helpers`は変化なし（旧設定によるfallbackはdoctorから観測できない） |

elevated backendはadmin権限による設定を要するため未実測。POSIX backendも未実測（CIにCodexは無く、開発機はWindows）。

## Open（ユーザー判断を要する。本ADRでは決めない）

**Windowsのsandbox水準**（Issue #14 §6）。非昇格環境で第2段階を成立させる選択肢は次のいずれかで、いずれもtarget behaviorへ影響するため`D-NNN`としてdecision logへの記録が要る。

| 選択肢 | 内容 | 影響 |
| --- | --- | --- |
| A | elevated Windows sandbox backendを必須にし、無ければfail closed | AC-C09-05とnetwork分離を保てる。D-029の検証対象環境で管理者設定が前提になる |
| B | 非昇格backendでは書込制限のみで継続する | model-generated commandから`CODEX_HOME`の認証材料を読め、shell networkも開く。AC-C09-05とIssue #14 §4を満たさず、認証材料の隔離をbroker / short-lived credentialへ移す設計が別途必要 |
| C | Windows nativeのreviewerを対象外にし、POSIX backend（WSL等）へ限定する | D-029の検証対象と衝突する可能性がある |

既定はfail closed（A）を推奨するが、決定はユーザーの明示合意recordを待つ。それまで第2段階のpreflightはAとして実装し、非昇格環境では起動しない。

2026-09-10、会話でユーザーがAに合意したため、target-experienceのdecision logへ**D-033（Proposed）**として記録した。会話での合意は合意根拠ではなく、Issue #14へのcommentでGitHub上の明示合意recordを得た後にDecidedへ変更する。

## 検証と完了境界

- PR #56: 実gitでexact SHA・detached HEAD・remoteなし・alternatesなし・dirty観測・破棄を検証。branch coverage 100%
- PR #57: 生成profileの内容、`projects` tableのuntrusted固定、home名・private root・protected rootの拒否、config改竄・token env・metadata不整合のfail closed、申告値の入口が無いことをhermetic testで固定。branch coverage 100%
- 本ADRの実測は`codex doctor --json`と`codex sandbox`によるもので、実API呼出・認証・実GitHub mutationを伴わない。sandbox強制の受入evidenceは第2段階のpreflightが取得する

本ADRはAC-C09-01〜05の文言を変更しない。第2段階、手動canary（Issue #14「Canary harness」節）、prompt / `ReviewContext`、AC-C09-04の三者照合、AC-C06-03の統合（#11）は未実装である。D-032はProposedのままである。
