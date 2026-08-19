<!-- SPDX-License-Identifier: Apache-2.0 -->

# Documents

## 読む順番

| 読者の目的 | 最初に読む文書 | その文書が正本とする内容 |
| --- | --- | --- |
| 現在の構造を知る | [architecture overview](architecture/overview.md) | 役割、主経路、component map、不変条件 |
| 製品の完成像を知る | [target experience](plans/target-experience.md) | user-visible behaviorと合意済み制約 |
| 実装順序を知る | [implementation plan](plans/implementation-plan.md) | component、依存、Phase、受入条件 |
| 判断理由を知る | [decisions](decisions/) | 重要な不可逆判断 |
| 参考実装との比較を知る | [reference implementation assessment](research/reference-implementation-assessment.md) | 観測事実と選択移植候補 |
| 開発に参加する | [CONTRIBUTING](../CONTRIBUTING.md) | setupと変更手順 |
| 用語を確認する | [glossary](glossary.md) | 本projectの用語定義 |

## 文書のauthority

各文書の先頭にStatusを記載します。

| Status | 扱い |
| --- | --- |
| `Agreed` / `Accepted` | normative。要件および合意済みの制約 |
| `Draft` | review中であり未確定 |
| `Research` | informative。判断材料であり要件ではない |
| `Non-normative example` | 例示。仕様と食い違う場合は正本を優先する |

GitHubのPR commentはdiscussionおよびevidenceです。合意した内容は正本文書へ反映し、commentを正本として参照しません。

## Directory

| Directory | 内容 |
| --- | --- |
| [`plans/`](plans/) | target experienceとimplementation plan |
| [`decisions/`](decisions/) | ADR |
| [`architecture/`](architecture/) | overviewと、実装段階で追加する詳細設計 |
| [`research/`](research/) | 調査結果。要件ではない |
| [`examples/`](examples/) | 表示例。要件ではない |

参考実装の文書を一括移植せず、新設計に必要な資料だけを出典付きで選択移植します。
