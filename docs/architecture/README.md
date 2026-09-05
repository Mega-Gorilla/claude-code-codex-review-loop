<!-- SPDX-License-Identifier: Apache-2.0 -->

# Architecture

| Field | Value |
| --- | --- |
| Status | **Accepted**（PR #4のユーザー承認とmergeにより確定） |

| 文書 | 内容 |
| --- | --- |
| [overview.md](overview.md) | 役割、主経路、component map、不変条件の1ページ要約。最初に読む |
| [implementation plan](../plans/implementation-plan.md) | component定義、依存、Phase、受入条件の正本 |
| [c01-state-machine.md](c01-state-machine.md) | C-01の遷移表・遷移図（code registryから生成。手動編集しない） |

componentは次の15項目です。一覧と依存とPhaseはimplementation plan Section 4、主要な決定と受入条件はSection 5、実装順序はSection 8を参照してください。

| ID | Component |
| --- | --- |
| C-01 | domain state machine、event、command |
| C-02 | agent protocol schemaとcheckpoint envelope |
| C-03 | process abstraction（Windows / POSIX） |
| C-04 | security policy（redaction、permission profile、trust rule） |
| C-05 | GitHub transport（未検証metadataの取得と投稿） |
| C-06 | canonical record検証とcredential隔離 |
| C-07 | resumeとretention |
| C-08 | active host protocolとstep engine |
| C-09 | fresh reviewer runtimeと隔離checkout（Claude Code / Codex拡張案はD-032: Proposed / Issue #52） |
| C-10 | PR mode review loop |
| C-11 | decision / clarification / follow-up protocol |
| C-12 | test・CI qualificationとfinal reporter |
| C-13 | human merge gate |
| C-14 | Issue modeとIssue-to-PR handoff |
| C-15 | Plugin配布と任意wrapper |

実装子Issue #5〜#22は発行済みで、親roadmap Issue #2から参照しています。

このdirectoryには、実装が進んだ段階でcomponent単位の詳細設計や図を追加します。implementation planと重複する記述は置きません。
