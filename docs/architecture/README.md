<!-- SPDX-License-Identifier: Apache-2.0 -->

# Architecture

| 文書 | 内容 |
| --- | --- |
| [overview.md](overview.md) | 役割、主経路、component map、不変条件の1ページ要約。最初に読む |
| [implementation plan](../plans/implementation-plan.md) | component定義、依存、Phase、受入条件の正本 |

componentは次の15項目です。責務、依存、Phase、受入条件はimplementation plan Section 5とSection 6を参照してください。

| ID | Component |
| --- | --- |
| C-01 | domain state machine、event、command |
| C-02 | agent protocol schemaとcheckpoint envelope |
| C-03 | process abstraction（Windows / POSIX） |
| C-04 | security policy（redaction、permission profile、trust rule） |
| C-05 | GitHub canonical conversation transport |
| C-06 | actor認証とcredential隔離 |
| C-07 | resumeとartifact retention |
| C-08 | active host protocolとstep engine |
| C-09 | Codex fresh reviewer runtimeと隔離checkout |
| C-10 | PR mode review loop |
| C-11 | decision / clarification / follow-up protocol |
| C-12 | test・CI qualificationとfinal reporter |
| C-13 | human merge gate |
| C-14 | Issue modeとIssue-to-PR handoff |
| C-15 | Plugin配布と任意wrapper |

実装子Issueはimplementation planのレビュー・合意後に発行し、親roadmap Issue #2から参照します。

このdirectoryには、実装が進んだ段階でcomponent単位の詳細設計や図を追加します。implementation planと重複する記述は置きません。
