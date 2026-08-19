<!-- SPDX-License-Identifier: Apache-2.0 -->

# Contributing

planningは完了しています。実装は[implementation plan](docs/plans/implementation-plan.md)のPhase順に、子Issue（#5〜#22）単位で進めます。

最初に[architecture overview](docs/architecture/overview.md)を読んでください。用語は[glossary](docs/glossary.md)にあります。

## 開発環境

| 項目 | 要件 |
| --- | --- |
| Python | 3.11以上 |
| PowerShell | 7以上。公式MSI installerで配布される版（`Microsoft.PowerShell` winget packageによる導入を含む）。Windows Store版は未検証 |
| OS | Windows、およびLinux/SSH上のPowerShell 7 |
| 外部CLI | `gh`。実装が進んだ段階でClaude CodeとCodexのCLI |

```powershell
python -m pip install -e ".[dev]"
```

開発用依存は`pyproject.toml`の`[project.optional-dependencies].dev`だけで定義します（直接依存をexact pin。推移依存は固定しません）。runtime依存の方針はP-001で決定し、開発用依存とは分離します。

## 実行するcheck

```powershell
python -m pytest -q                 # 全test
python -m ruff check .              # lint
python -m mypy                      # type check（src対象、strict）
python -m coverage run -m pytest -q # coverage計測つきtest（parallel dataを生成）
python -m coverage combine          # parallel dataの結合
python -m coverage report           # coverage表示（floorはquality-baseline.toml）
python -m pytest tests/test_repository_contract.py::test_project_identity_is_consistent -q   # 単体test
git diff --check origin/main...HEAD # CIと同じwhitespace check
```

## 品質ゲートの運用

baselineは`quality-baseline.toml`でversion管理します。値を緩める変更（coverage floorの引き下げ、module size上限の引き上げ）は、このfileの変更としてPR diffへ現れます。**PRへ理由の記載を必須とします。**

- **coverage**: branch coverageで計測し、`[coverage].floor`を下回るとCIがfailします。floorは引き上げる方向を推奨します。`patch = ["subprocess"]`により、通常のPython subprocessとして起動した子processも自動計測されます（parallel dataのためreport前に`combine`が必要）
- **module size**: git管理下の全`.py`を`[module-size]`へ登録します。**baselineは登録時点の実測行数**とし、headroomを設けません。以後の増加は`quality-baseline.toml`の変更として必ずPR diffへ現れるため、あわせて理由を記載してください。未登録・存在しないfileの登録（stale）はCIがfailします。生成fileは現状存在せず、導入時に除外listをbaselineへ追加します
- **mypy**: 対象は`src`配下でstrictです。対象は広げる方向のみ変更できます。例外が必要な場合は`pyproject.toml`のoverridesへ理由つきで追加し、PRで説明してください
- **subprocess coverage**: 有効です。素の`python child.py`として起動した子processが自動計測されることを`tests/test_quality_gates.py`のtestで担保しています
- **version整合**: 通常CIで`pyproject.toml`とpackageの`__version__`の一致を検証し、`v*` tagのbuildではtagとpackage versionの不一致をfailにします

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
- 本project独自のversion管理対象fileは、先頭3行以内に`SPDX-License-Identifier: Apache-2.0`を置く。選択移植した第三者成果物は元licenseのSPDX表示を保持し、`THIRD_PARTY_FILES`へ登録する
