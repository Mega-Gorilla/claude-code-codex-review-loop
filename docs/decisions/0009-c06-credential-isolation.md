<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0009: C-06 credential隔離とpermission gateの規約

- Status: Accepted
- Date: 2026-08-22

## Context

implementation planはC-06へ、record検証に加えて「agentごとのcredential到達可能範囲」と「OS別file権限」を求める（P-015 / P-009、AC-C06-03 / 04 / 05 / 10 / 11）。C-03の`SpawnSpec`は既にenvを非継承の明示指定にしており（隔離の前提）、C-04は選択規則`select_profile`とredaction patternを持つ。決めるべきはreviewer envの具体的な内容、Windowsで「作成者のみ」をどう実現するか、Auto mode利用可否の検出方法、resume gateの意味論である。R-06は「AC-C06-03を両OSのCIで常時実行し、隔離手段を1箇所へ集約する」ことを求める。

env契約の各項目は本実装環境（Windows 11 / git 2.50.0.windows.2 / gh 2.79.0 / claude 2.1.239）で実測して決めた。

## Decision

### reviewer envの構築（`identity/credentials.py`）

1. **除去方式ではなく構築方式**を採る。親envからは`COPY_ENV_NAMES`（`PATH` / `PATHEXT` / `SYSTEMROOT` / `SYSTEMDRIVE` / `WINDIR` / `COMSPEC` / `LANG` / `LC_ALL` / `TZ`）だけを複写し、その他は届かない。未知のtoken変数名に対して構造的に閉じており、builderは秘密値を読むことすらない（P-015）。両OSの変数名の和集合を単一listで扱い、存在しない名前は単に複写されない（platform分岐を持たない）
2. **isolation overlay**（実測根拠つき）。extraより後に適用し、C-09 / C-12の追加変数がoverlayを上書きできないようにする。

| 変数 | 値 | 根拠 |
| --- | --- | --- |
| `HOME` / `USERPROFILE` / `HOMEDRIVE`+`HOMEPATH` | reviewer home | HOME相当の差し替え。実global設定へ書かないことを実測確認 |
| `TEMP` / `TMP` / `TMPDIR` | reviewer home内tmp | temp fileを隔離領域へ閉じ込める |
| `XDG_CONFIG_HOME` / `XDG_CACHE_HOME` / `XDG_STATE_HOME` / `XDG_DATA_HOME` | reviewer home内 | Linux側の設定探索を遮断 |
| `GH_CONFIG_DIR` | reviewer home内の空dir | token不在と併せて`gh api user`が**exit 4（AUTH）・0.06秒・network到達なし**になることを実測 |
| `GH_PROMPT_DISABLED` / `GH_NO_UPDATE_NOTIFIER` | `1` | C-05の`GhContext`と同じ非対話規約 |
| `GIT_CONFIG_NOSYSTEM` | `1` | system設定（`credential.helper=manager`を含む）を遮断。`--list --show-origin`が空になることを実測 |
| `GIT_CONFIG_GLOBAL` | reviewer home内の空file | **devnullは不可**（`GIT_CONFIG_GLOBAL=nul`への書込は`could not lock config file`でexit 255になる実測）。private fileなら遮断と正当な書込を両立できる |
| `GIT_CONFIG_COUNT=1` / `GIT_CONFIG_KEY_0=credential.helper` / `GIT_CONFIG_VALUE_0=`（空） | 固定 | env由来の設定は**最高優先度（listの末尾）**へ入り、gitcredentials(7)の「空値はhelper listをresetする」規約でrepo-localのhelperまで無効化されることを実測 |
| `GIT_TERMINAL_PROMPT` | `0` | 認証要求が0.3秒でexit 128になり、promptでhangしないことを実測 |
| `GIT_ASKPASS` / `SSH_ASKPASS` | reviewer home内の**存在しないpath** | spawn失敗で対話取得経路をfail closedにする |
| `GIT_SSH_COMMAND` | `ssh -o BatchMode=yes` | ssh remoteでも対話promptを出さず即失敗させる |

3. **二重防御のdenylist**: 結果envのkeyがC-04の`TOKEN_ENV_NAMES`（正本）と一致したら`CredentialIsolationError`。token env名の集合はC-04 redactionと共有し、片方だけが更新されるdriftをtestで常設検証する（`test_c06_credentials.py::TestTokenNameRegistry`）
4. **reviewer homeはcanonical絶対path**: `prepare_reviewer_home`はparentを絶対path・symlink解決済みへ正規化し、`ReviewerHome`は全構成要素が**正規化済み（`path == path.resolve()`）の絶対path**かつroot配下であることを構築時に検証する。相対pathをenvへ入れると、Controllerと子processのcwdが異なる場合に**子cwd配下の別領域**が`HOME` / `GH_CONFIG_DIR` / `GIT_CONFIG_GLOBAL`として解決され、隔離契約が破れる（子cwdへ囮の設定を置く回帰testを常設）。また`..`やsymlinkを含むpathは字句上の包含判定（`is_relative_to`）を素通りしてroot外を指せるため、**実体で**containmentを判定する
5. **env配布直前の実体再検証**: `build_reviewer_env`は、containment（決定4と同じcanonical判定。**構築後のpath実体の差し替えを検出する**）、全private directoryとgit config fileが作成者限定で実在すること、**askpass pathが存在しないこと**（`lexists`。実体の無いsymlinkも検出する）を確認してからenvを返す。`ReviewerHome`は手動でも構築でき、fs側の状態は構築後にも変わり得るため（fail closed）
6. **private fileはfile実体を共有しない**: link数が1であることを要求する（`create` / `verify`の両方）。**hard linkはpath正規化では検出できず**（`resolve()`してもlink側のpathのまま）、mode / owner / DACLも同じ実体から読み出されるため権限検証も素通りする。root外のowner-only fileへのhard linkを`GIT_CONFIG_GLOBAL`に据えれば、隔離領域内のpathのまま外部のGit設定へ到達できてしまう

### AC-C06-03の充足状況（**未充足を含む**）

7. 正本（implementation plan Section 5）のAC-C06-03は「reviewer環境からの**GitHub mutation**、**実repository書込**、**GitHub write credentialへの到達**が、いずれも失敗する」ことを要求する。Phase 6の実装で充足するのは前者2つ（GitHub mutationとcredential到達。`gh api -X POST/PATCH/DELETE`が認証段階でexit 4となりrequestを送出しないことを実測・常設test化）である
8. **「実repository書込の失敗」はPhase 6では未充足**とする。env契約はfilesystemの書込を止める機構を持たず、これはsandboxと隔離checkout（C-09。AC-C09-02）が担う。`file://` remoteへのpushは認証なしで成功するため、Phase 6のnegative controlにもならない
9. したがって**Issue #11（Phase 6）はC-09の統合完了まで閉じない**。ADRが正本の受入条件を縮小することはできないため、本PhaseはAC-C06-03を「部分充足」として明示し、残件をC-09へ引き渡す。traceability（DOD / MVP表）の扱いを変える必要があるかはユーザー判断の対象とする（本ADRは判断を代行しない）
10. reviewer home名・timeout・実行commandの既定値解決はC-12

### OS別file権限（`identity/fs_permissions.py` + backend）

11. 共通契約は**排他作成 + 読み戻し検証**。事前に存在するpathの権限を信用しない（攻撃者が緩い権限で先に作ったdirectoryを使う経路を作らない）。検証失敗はsilentに続行せず`FsPermissionError`。OS分岐はfacade末尾のconditional import 1箇所に閉じる（C-03の`spawn.py`と同じ構造）
12. POSIXはdirectory `0o700` / file `0o600`。umaskが権限を削っても作成者がアクセスできる期待値へ揃えてから、`stat`でmodeと所有者を検証する
13. Windowsは**ctypesによる明示DACL**を採る。現userの単一許可ACE（`(OI)(CI)` / `FILE_ALL_ACCESS`）を持つsecurity descriptorを`CreateDirectoryW`へ渡し、**作成とACL設定を原子的に**行う。private directory内のfileは`(OI)`継承で同じ単一ACEを得る
   - **却下した代替案**: `mkdir` → `icacls`での付け替え。(a) 2手順の間に親DACL継承のままのrace windowが残る、(b) `icacls`の要約出力が日本語localeで文字化けし解析による検証ができない（実測）、(c) subprocess依存が増える
   - 検証は`GetNamedSecurityInfoW` + `GetAce`の構造化読み出しで行う（locale非依存）。**NULL DACL（全員アクセス可）とACE 0件は共にerror**
14. 「作成者のみ」の定義は**現userの単一ACE**とし、SYSTEM / Administratorsを含めない。administratorはtake ownershipで到達できるが、これはPOSIXのrootと同格の限界として受け入れる

### permission gate

15. **resume gate**（AC-C06-04）はcheckpoint値との**全field完全一致**（Permission ID / head / tool / scope）でのみ`ResumeTicket`を返す。scopeの縮小も一致しない限り拒否する（部分一致で範囲を推測しない決定的な等値比較）。空値fieldは「何にでも一致する停止点」を作るため構築時に拒否する
16. **authority分離**（AC-C06-11）はmodule構造で強制する。`identity/permissions.py`はdomainの承認event / evidence型を一切importせず（AST検査で常設）、`ResumeTicket`は`RecordEvidence`と型的に無関係で、gateはGitHub由来の値を引数に取らない。したがって「GitHub commentだけではlocal tool permissionを付与しない」「tool permissionの許可がmerge / follow-up / 仕様判断の承認を生成しない」が構造として成立する
17. **Auto mode検出**（AC-C06-10）は`claude auto-mode config`の**exit codeとJSON objectとして解釈できるか**だけで判定する（claude 2.1.239で`auto-mode config`がexit 0 + JSONを返すことを実測）。helpやmode名の文字列一致には依存しない（P-003。表示文言はversionで変わり、helpは禁止flag名を含むためfixture化もしない）。起動失敗・timeout・解釈不能は利用不可へ倒し、C-04の`select_profile`が用途別fallbackを選ぶ。**provisional**: account / model / providerの適格性は単発probeでは確定できず、誤って利用可と判定した場合は実行時blockの`AWAITING_TOOL_PERMISSION`経路が受け止める。実行command・timeoutの既定値解決はC-12
18. envelopeへ`permission.head_sha`をoptional additiveで追加する（resume gateがPermission IDと併せて再検証するblock時のhead。ADR-0004のadditive規則、version bumpなし）

## Consequences

- C-09は本componentの`prepare_reviewer_home` / `build_reviewer_env`を隔離checkoutの起動に使い、remote構成の統制（push可能remoteを与えない）を足して`AC-C09-02` / `AC-C09-05`を完成させる
- CIのcoverage floor stepは、C-03と同様にOS専用backendを異OS側のreportから除外する（Windows: `identity/mode_posix.py`、ubuntu: `identity/acl_windows.py`）。floor値は変更しない
- `transport.write_private_file`（C-05のworkdir内一時file、`0o600`）は前提が異なるため本Phaseでは統合せず、将来の整理課題として残す
- 実gitと実gh（認証不達側）を使うtestは両OSのCIで常時実行する（R-06）。networkへは出ず、認証情報も持たない
