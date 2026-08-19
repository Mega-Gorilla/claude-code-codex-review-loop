<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0003: P-001 schema検証は標準libraryのみの専用protocol validatorとする

- Status: Accepted
- Date: 2026-08-19

## Context

target experienceはCodex出力のschema検証を要求し、implementation planはこの判断をP-001としてPhase 0で決定すると定めた。比較対象は次の2案である。候補1は汎用JSON Schema validatorの再実装ではなく、C-02が必要とするschema機能だけを扱う。

| 候補 | 内容 |
| --- | --- |
| 1 | runtime依存をゼロに保ち、必要なschema機能だけを扱う専用protocol validatorを実装する |
| 2 | 成熟したvalidator library（`jsonschema` 4.26.0、MIT）をruntime依存として導入する |

## 比較方法

### 公平性の担保

両候補を**同一のapplication-facing interface**へ揃えた。size limit、UTF-8 decode、JSON parse、schema versionのgateは候補と独立した共通pipeline（`tests/p001_evaluation/common.py`）が行い、候補差はstructural / cross-field validationだけに限定した。公開errorは両候補とも`(code, path)`のみで構成し、free-textや入力値を含めない。候補2は`ValidationError.message` / `instance`を公開へ使用せず、`validator`種別と`absolute_path`（欠落・過剰field名は`validator_value`と`instance`のkey差分から決定論的に導出）で正規化した。

### Corpus

`tests/p001_corpus/`（representative 14件、malformed 24件、`manifest.json`が期待結果を固定）。malformedの期待stageは`size / utf8 / json / version / schema`から**一意**に指定し、schema違反はerror pathの期待値を持つ。次を含む: cross-field違反3種（条件付き必須、依存関係、kind依存の禁止field）、list of object、map、未知schema version、不正UTF-8のraw bytes、byte size超過（65,536 bytes上限に対する約70KB入力）、深いnest（3,000段）、Unicode、空文字列・空配列の許可/不許可。malformedの値には検出用sentinel（`SENTINELghp000`）を6 caseへ埋め込んだ。

### 再現方法

- 評価adapterと共通pipelineは`tests/p001_evaluation/`へversion管理する。`python -m pytest tests/test_p001_evaluation.py`が全corpusに対する両候補のverdict / stage / error path / public error集合の一致、sentinel非漏洩、決定性を検証する
- 候補2の依存は評価専用のoptional dependency `p001`（`pip install -e ".[dev,p001]"`）として固定: `jsonschema==4.26.0`（直接）、`attrs==25.3.0`、`jsonschema-specifications==2025.9.1`、`referencing==0.37.0`、`rpds-py==2026.6.3`（推移、Rust製compiled wheel）
- CIはUbuntu / Windows × Python 3.11で毎回この検証を実行する。初回評価はWindows 11 / Python 3.13.5でも実施した
- install sizeは`importlib.metadata`のdistribution file合計で測定（上記5 package合計 約2.3 MB）。性能はcorpus全件50回反復の平均（warm、単一機測定）で3.2 ms対4.5 msの参考値であり、決定要因にしない

### 候補2（jsonschema）の保守状態の記録

| 項目 | 値 |
| --- | --- |
| 評価日 | 2026-08-19 |
| 評価version | 4.26.0 |
| License | MIT（`License-Expression`で確認） |
| Python対応 | `Requires-Python: >=3.10`（package metadataで確認） |
| 配布形態 | main packageはpure Python wheel。推移依存`rpds-py`はplatform別のcompiled wheel |
| 直接依存 | `attrs`、`jsonschema-specifications`、`referencing`、`rpds-py`（評価時の解決versionは「再現方法」節に記録） |
| primary source | <https://pypi.org/project/jsonschema/>、<https://github.com/python-jsonschema/jsonschema> |
| release・保守状況 | 評価時点でPyPIの最新releaseは4.26.0であり、repositoryは活発に保守されている。release頻度と最新状況は上記primary sourceで確認する |
| security advisory確認先 | GitHub Security Advisories（上記repository）およびPyPIのadvisory表示 |

### Must-have条件（不合格なら点数に関係なく不採用）

Windows / Linuxで同一結果 / representative全受理 / malformed全拒否とstage分類一致 / error path特定 / untrusted入力でcrashしない / 決定性 / license適合 / Python 3.11対応。

## 結果

**両候補ともmust-haveをすべて満たし、公開resultは全38 caseで完全一致した**（verdict、stage、error code、error pathの集合が同一）。sentinel非漏洩も両候補で成立した。redactionはどちらの案でも達成可能であり、**採用の決定打にはならない**。

| 評価軸 | 候補1（専用validator） | 候補2（jsonschema） |
| --- | --- | --- |
| runtime依存 | 追加なし（第三者runtime依存由来のsupply-chain exposureは0。自前codeの正しさ・保守riskは残る） | 直接1＋推移4、追加install size約2.3 MB。`rpds-py`はplatformごとのcompiled wheel供給に依存 |
| 実装量（実測） | validator本体133行 | schema定義＋正規化adapter 143行（library本体は別） |
| 診断の正規化cost | code / pathを直接生成 | `required` / `additionalProperties`の対象field導出、null許可の判別、**cross-field ruleごとのallOf位置→canonical pathのmapping表**が必要。安全な公開errorに必要なwrapperが、専用validator本体と同規模になった |
| cross-field表現 | ruleを関数として直接記述 | `allOf` + `if/then` + `dependentSchemas`で表現可能だが、rule追加のたびにmapping表の保守を伴う |
| 将来のschema変更cost | 機能追加は自前実装 | JSON Schema語彙の範囲なら定義変更のみ。ただし正規化mapping表の追従が必要 |
| 保守責任 | validator全体を本projectが負う | schema定義とwrapperのみ。ただしlibraryのversion追従・脆弱性監視と、mapping表の保守を負う |

## Decision

**候補1を採用する。** runtime依存はゼロを維持する。

- 採用理由: (1) Controllerは任意repositoryへinstallされるCLIであり、第三者runtime依存とplatform別compiled wheelへの依存を持たないことが配布要件に直結する。(2) 公平化した比較の結果、候補2で安全な公開errorとcanonicalなcross-field診断を得るためのwrapper（143行）が候補1のvalidator本体（133行）と同規模となり、「保守を外部化できる」という候補2の主要な利点が本projectのprotocol検証という用途では実質的に成立しなかった。
- 不採用: 候補2（`jsonschema` 4.26.0、MIT、Python >= 3.10、main wheelはpure Python）。機能・正しさに問題はなく、must-haveと公開result一致をすべて満たした。
- 既知の欠点: validatorの正しさと保守の責任を本projectが全面的に負う。JSON Schemaの汎用語彙（pattern、format、複雑なconditional等）は持たない。
- 成立条件: C-02のschemaが「必須/optional、nested object、list、list of object、map、enum、基本型、null許可、非空文字列、size limit、extra field拒否、cross-field rule」の機能集合で表現できること。
- 再評価条件: production schemaがこの機能集合を超え、追加実装が現行validator規模（約130行）の2倍を超える見込みになった場合、本corpusと評価adapterで比較を再実施する。

## 実装への反映

- **runtime依存への追加**: なし。仮に将来候補2へ転換する場合は、その時点のPhaseでproduction schema実装と同時に追加し、version constraintは「実測で検証したexact versionを下限とするcompatible release（`~=`）」を方針とする。推移依存は固定しない。
- **評価adapterの扱い**: 使い捨てにせず`tests/p001_evaluation/`へ**version管理して残す**。CIが両OSで比較の一致を常時検証するため、結果は独立に再現・review可能である。これはproductionのvalidator実装ではない。
- **corpusの扱い**: `tests/p001_corpus/`へ恒久的に保持する。`tests/test_p001_corpus.py`がcorpusとmanifestの整合（stage別の実挙動確認を含む）を検証する。Phase 2でC-02のvalidatorが完成した時点で、このcorpusをC-02のregression testへ接続する。
- **Phase 2（C-02）への引き継ぎ**: (1) validatorはC-02の所有とし、schema定義を宣言的なspecとして分離する。(2) 公開errorは`(code, path)`のみとし、入力値を含めない設計を維持する。(3) 入力境界はbytes size → UTF-8 → JSON parse → version gate → structural validationのpipelineとし、未知のschema versionは`version` stageのvalidation errorとして拒否する。(4) manifest駆動でcorpus全件を検証するtestをC-02の受入へ含める。(5) **未知field名はerror pathに現れる攻撃者制御の文字列**であるため、C-02では公開renderの際にpath中の未知keyへ長さ制限とescapeを適用する。

## Consequences

- `pyproject.toml`の`dependencies = []`が、stubの初期状態ではなく**決定された要件**になる
- schema機能の追加はC-02の変更として現れ、機能集合の拡大が再評価条件の判定材料になる
- implementation planのP-001（未決事項）は本ADRにより解決した
