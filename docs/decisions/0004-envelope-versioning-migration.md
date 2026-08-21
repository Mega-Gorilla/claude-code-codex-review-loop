<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0004: schema versioningとcheckpoint envelope migration policy

- Status: Accepted
- Date: 2026-08-20

## Context

implementation planはC-02の必須要件として「すべてのschemaにschema versionを持たせる。未知versionは推測補完せずvalidation errorとする」を定め、「checkpoint envelopeのversioning方式とmigration policyの詳細」をPhase 2で決定する技術判断とした（AC-C02-03）。validatorの実装方式はADR-0003（runtime依存ゼロの専用validator）で決定済みである。

checkpoint envelopeには追加の制約がある。target experience 10.1が保存項目18種を列挙する一方、implementation planのdeviations表は「構造を追加して採用 — C-02のversioned envelopeへ格納し、fieldは利用するPhaseで追加する」と定める。つまりenvelopeの構造はPhase 2で確定するが、内容fieldはPhase 5〜16が段階的に追加するため、**field追加のたびにversionが上がる方式は運用できない**。

## Decision

### Versioning

1. **kindごとの整数`schema_version`、1始まり・単調増加・欠番なし**とする。kind間でversionは独立する（全schemaを束ねるglobal versionは持たない）
2. **互換な変更はversionを上げない（additive方針）**: optional fieldの追加、enum値の追加、optional sectionの追加は同一version内の変更とする。既存fieldの削除・改名・型変更・required化などの非互換変更のみversionをbumpする
3. 未知（将来）のinteger versionは`version` stageのvalidation errorとして拒否する（推測補完しない）。integer tokenでない`schema_version`（`1.0` / bool / 欠落）は`schema` stageの型違反として拒否する（ADR-0003のinteger意味論）
4. Plugin↔CLIのprotocol version（P-012、Phase 16）は本ADRのschema versionとは別の機構であり、ここでは定めない

### Migration

5. migrationは**損失のないpure関数 `payload(v_n) -> payload(v_n+1)`**としてkindのregistryへ登録し、現行versionへ到達するまで**段階的にchain**する。飛び越し（v1->v3の直接変換）は登録しない
6. migrationが行ってよい変換は損失のないもの（field改名、構造の移動、既定値による補完）に限る。**意味的fieldの捏造を禁止**する
7. **migration後は必ず現行versionの同じvalidatorを通す**（repairと同一の原則。AC-C02-02 / 03）。再検証に失敗した場合はmigration errorとする
8. **chainが現行versionへ到達しないversionは構造化error（`migration_unavailable`）とし、silentに無視しない**（AC-C02-03）。旧version payloadはmigration前に宣言versionのspecで検証し、不正な旧payloadを黙って変換しない

### Checkpoint envelope構造

9. envelopeは**必須の外枠**（`schema_version` / `run_id` / `repository` / `number`）と**全section optionalの本体**で構成する。target experience 10.1の18項目はv1で16のoptional sectionへ写像済みであり、各section内のfieldもoptionalとする
10. **fieldはそれを利用するPhaseと同じPhaseで追加する**（implementation planのPhase別追加予定に従う）。optional field / sectionの追加はadditive変更（rule 2）であり、versionは上がらない。したがって最初のversion bumpは非互換な構造変更が必要になった時点で発生し、その時にmigration chainを登録する

### 入力sizeとrepair

11. 入力bytes上限の既定は**65,536 bytes**（Phase 0評価pipelineと同値）とし、kindごとに`SchemaDefinition.max_input_bytes`で上書きできる。長文になるkind（final report、checkpoint envelope）は**262,144 bytes**とする
12. repairとして許可する変換は**UTF-8 BOMの除去**と**specへ宣言された既定値による欠落optional fieldの補完**の2つに限る。repair後は必ず同じvalidatorを通す（AC-C02-02）

## Consequences

- 現行の全定義はv1のみを持ち、migrationは未登録である（`tests/test_c02_migration.py`が全定義のv1・migration未登録を固定し、機構自体はtest定義のv1->v2->v3 chainで検証する）
- 後続Phaseがcheckpointへfieldを追加する場合、該当sectionへのoptional field追加はC-02のspec変更（additive）として現れ、versionは上がらない
- 非互換変更を行うPRは、version bumpとmigration登録と両version specの保持を同時に行う（旧versionのspecを削除するのはretention方針を定めるPhase 14以降の判断）
- Phase 8（HOST_ACTIONの最終確定）・Phase 11（clarification結果のC-01 event対応）等で暫定enumが変わる場合、値の追加はadditive、値の削除・改名はversion bumpとして扱う

## 実装への反映

`src/claude_code_codex_review_loop/schema/`のregistry（`SchemaDefinition.versions / migrations / max_input_bytes`）、`migrate.load_with_migration`、`registry.repair_and_validate`が本ADRを実装する。implementation planの未確定事項「checkpoint envelopeのversioning方式とmigration policyの詳細」は本ADRにより解決した。
