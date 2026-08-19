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

比較条件はprototypeより先に固定した。corpusは`tests/p001_corpus/`にversion管理されている（representative 11件、malformed 16件、期待結果は`manifest.json`）。malformedはJSON解析エラーとschema検証エラーを区別し、error pathの期待値を持つ。

### Must-have条件（不合格なら点数に関係なく不採用）

- Windows / Linuxで同じ結果になる
- representative corpusをすべて受理する
- malformed corpusをすべて拒否し、json / schemaのエラー分類が期待と一致する
- 診断結果から問題fieldのpathを特定できる
- untrustedな入力でcrashしない（深いnest、巨大入力、不正UTF-8を含む）
- 同じ入力から同じ診断結果が得られる（決定性）
- licenseがApache-2.0 projectで利用可能
- Python 3.11以降へ対応する

### 結果

両案ともmust-haveを満たした（27 / 27 case、error path全一致、crash・非決定性なし）。比較評価は次のとおり。

| 評価軸 | 候補1（専用validator） | 候補2（jsonschema） |
| --- | --- | --- |
| runtime依存数 | **0** | 直接1、推移4（`attrs`、`jsonschema-specifications`、`referencing`、`rpds-py`） |
| install size | 0（package本体のみ） | 約2.3 MB |
| supply-chain risk | なし | 5 package。`rpds-py`はRust製compiled wheelで、platformごとのbinary供給に依存する |
| 診断品質 | error pathとmessageを完全に制御。**error messageへ入力値を含めない** | pathは取得可能だが、**既定のerror messageに入力値がそのまま含まれる**（実測で確認）。credential redactionの観点では別途sanitization層が必要 |
| malformed耐性 | corpus全拒否。size limit・不正UTF-8・深いnestを一次入力の段階で処理 | corpus全拒否（同等） |
| 実装量 | prototype実測109行 | schema定義＋path変換のwrapper約80行（library本体は別） |
| 将来のschema変更cost | 機能追加は自前実装（保守責任を負う） | JSON Schema語彙の範囲なら定義変更のみ |
| protocol version拡張性 | version fieldの扱いを自由に設計できる | 同等（上位層の責務） |
| 保守責任 | validator全体を本projectが負う | schema定義とwrapperのみ。ただしlibraryのversion追従と脆弱性監視を負う |
| 性能（corpus 1周） | 3.2 ms | 4.5 ms（同等とみなす） |

## Decision

**候補1を採用する。** runtime依存はゼロを維持する。

- 採用理由: (1) Controllerは任意repositoryへinstallされるCLIであり、supply chainとplatform binary依存の最小化が配布要件に直結する。(2) validation errorはGitHubへ投稿されるrecordの材料になるため、**error messageへ入力値を含めない**性質を設計として保証できることは、redaction（P-004 / C-04）の観点で本質的な利点である。(3) 検証対象は本projectが定義する固定のprotocolであり、必要な機能集合は列挙可能で小さい（prototype実測109行）。
- 不採用: 候補2（`jsonschema` 4.26.0、MIT）。機能・正しさに問題はなく、must-haveをすべて満たした。汎用schemaの表現力とlibraryとしての実績では優るが、上記(1)(2)を上回らない。
- 既知の欠点: validatorの保守責任を本projectが全面的に負う。JSON Schemaの汎用語彙（conditional、pattern、format等）は持たないため、必要になれば自前実装が増える。
- 成立条件: C-02のschemaが「必須/optional、nested object、list、enum、基本型、null許可、非空文字列、size limit、extra field拒否」の機能集合で表現できること。
- 再評価条件: Phase 2以降のproduction schemaがこの機能集合を超え、追加実装が現行prototype規模（約110行）の2倍を超える見込みになった場合、本corpusを用いて比較を再実施する。

## 実装への反映

- **runtime依存への追加**: なし（候補1のため）。仮に将来候補2へ転換する場合は、その時点のPhaseでproduction schema実装と同時に追加し、version constraintは「実測で検証したexact versionを下限とする compatible release（`~=`）」を方針とする。推移依存は固定しない。
- **prototypeの扱い**: 両prototypeとも使い捨てとして**削除した**。採用実装はPhase 2（C-02）で、本ADRの機能集合とcorpusを起点に新規に実装する。比較の再現はcorpusと本ADRの記録で可能である。
- **corpusの扱い**: `tests/p001_corpus/`へ**恒久的に保持**する。`tests/test_p001_corpus.py`がcorpusとmanifestの整合を検証し、Phase 2でC-02のvalidatorが完成した時点で、このcorpusをC-02のregression testへ接続する。
- **Phase 2への引き継ぎ**: (1) validatorはC-02の所有とし、schema定義を宣言的なspecとして分離する。(2) error messageへ入力値を含めない設計を維持する。(3) manifest駆動でcorpus全件を検証するtestをC-02の受入へ含める。(4) size limitは一次入力（bytes）の段階で判定する。

## Consequences

- `pyproject.toml`の`dependencies = []`が、stubの初期状態ではなく**決定された要件**になる
- schema機能の追加はC-02の変更として現れ、機能集合の拡大が再評価条件の判定材料になる
- implementation planのP-001（未決事項）は本ADRにより解決した
