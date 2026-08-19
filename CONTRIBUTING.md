<!-- SPDX-License-Identifier: Apache-2.0 -->

# Contributing

本projectはarchitecture planning段階です。実装は[implementation plan](docs/plans/implementation-plan.md)のPhase順に、子Issue単位で進めます。

最初に[architecture overview](docs/architecture/overview.md)を読んでください。用語は[glossary](docs/glossary.md)にあります。

## 開発環境

| 項目 | 要件 |
| --- | --- |
| Python | 3.11以上 |
| PowerShell | 7以上。公式MSI installerで配布される版（`Microsoft.PowerShell` winget packageによる導入を含む）。Windows Store版は未検証 |
| OS | Windows、およびLinux/SSH上のPowerShell 7 |
| 外部CLI | `gh`。実装が進んだ段階でClaude CodeとCodexのCLI |

```powershell
python -m pip install -e . pytest
```

## 実行するcheck

```powershell
python -m pytest -q                 # 全test
python -m pytest tests/test_repository_contract.py::test_project_identity_is_consistent -q   # 単体test
git diff --check origin/main...HEAD # CIと同じwhitespace check
```

lintとtype checkはPhase 0で導入します。導入後はここへcommandを追記してください。

CIは`ubuntu-latest`と`windows-latest`のPython 3.11で全testを実行します。testの実行対象を限定する設定は追加しません。test fileの追加漏れが構造的に起こらない状態を保ちます。

## 開発の流れ

1. 親roadmap Issue #2から、着手するPhaseの子Issueを確認する
2. `agent/<topic>`形式のbranchを作成する
3. 実装し、testを追加する
4. PRを作成する。PRはdependency順に小さく分ける
5. Codex reviewを受ける
6. blocking findingを解消する
7. ユーザーの明示承認を得てmergeする

PRのmergeにはユーザーの明示的な承認が必要です。レビュー完了はmerge承認ではありません。

## componentと子Issueの対応

implementation plan Section 5がcomponent（C-01〜C-15）を、Section 8がPhaseを定義します。1 Phaseが1つの子Issueに対応し、1 Phaseを複数のPRへ分けて構いません。

PRでは、対応するcomponent IDとPhaseをPR本文へ記載してください。

## 参考実装から選択移植する場合

参考実装のコードを利用する場合は、[ADR-0002](docs/decisions/0002-independent-reimplementation.md)のSelective porting policyに従います。

同じPRへ次を記録してください。

- 対象file
- source commit
- 移植する理由
- 適用license
- 移植後test

第三者成果物の場合は、元licenseと必要なnoticeも同じPRで追加し、`THIRD_PARTY_FILES`へ登録します。SPDX表示をApache-2.0へ書き換えません。

参考実装の識別子とcommit SHAを、本repositoryの正式な文書へ設計根拠として記録しません。調査結果は[reference implementation assessment](docs/research/reference-implementation-assessment.md)にあります。

## どの変更で何を更新するか

| 変更の種類 | 更新する対象 |
| --- | --- |
| user-visible behaviorを変える | [target experience](docs/plans/target-experience.md)のdecision logへ`D-NNN`を追加し、ユーザー合意後にStatusを`Decided`にする |
| 大きな方向転換 | `docs/decisions/`へADRを追加（連番`0001-`〜） |
| component、依存、Phase、受入条件を変える | [implementation plan](docs/plans/implementation-plan.md)。componentを増減する場合は`docs/architecture/README.md`の一覧も更新 |
| 参考実装の調査結果 | [reference implementation assessment](docs/research/reference-implementation-assessment.md) |
| 表示例 | `docs/examples/`。要件は正本へ書く |
| 用語を追加する | [glossary](docs/glossary.md) |
| 本project独自のfileを追加する | 先頭3行以内に`SPDX-License-Identifier: Apache-2.0`を置く。検査対象は`.md` / `.py` / `.toml` / `.yml` / `.yaml` / `.ps1` / `.sh` / `.psm1` / `.psd1`をgit管理下から自動discoveryするため、path listの手動更新は不要 |
| 第三者成果物を選択移植する | 元licenseのSPDX表示を保持し、`tests/test_repository_contract.py`の`THIRD_PARTY_FILES`へpathと適用SPDX IDを登録する。登録しないとApache-2.0を要求してfailする |

## 文書の書き方

- 日本語で説明し、CLI名、state名、class / function / field名だけを英語のまま使う
- 独自用語は初出時にglossaryへlinkする
- 1つのparagraphで要件、理由、実装案、調査結果を混ぜない
- 必須は「必須」、推奨は「推奨」、例は「例」と明示する
- 受入条件は観測可能な1動作を1項目にする
- 過去の構成や議論の経緯を、現行architectureの記述として残さない
- version管理対象fileは先頭3行以内に`SPDX-License-Identifier: Apache-2.0`を置く
