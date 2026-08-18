<!-- SPDX-License-Identifier: Apache-2.0 -->

# Architecture

componentと依存関係の定義は[implementation plan](../plans/implementation-plan.md)へ集約しています。

| 内容 | 参照先 |
| --- | --- |
| 設計原則と根拠 | implementation plan Section 2 |
| 層構造とpackage layout | implementation plan Section 3 |
| component定義と依存グラフ | implementation plan Section 4 |
| 実装順序と子Issue | implementation plan Section 7 |

componentは次の10項目です。詳細はimplementation plan Section 4を参照してください。

1. domain state machine、event、command
2. GitHub canonical conversation transportとread-after-write
3. Claude Code Plugin / active host adapter
4. Codex fresh reviewer runtimeと隔離checkout
5. PR modeとIssue-to-PR handoff
6. decision / clarification protocol
7. test・CI qualificationとfinal reporter
8. human merge gate
9. checkpoint、resume、artifact retention
10. Windows PowerShell、Linux/SSH、`tmux` wrapper

実装子Issueはimplementation planのレビュー・合意後に発行し、親roadmap Issue #2から参照します。

このdirectoryには、実装が進んだ段階でcomponent単位の詳細設計や図を追加します。implementation planと重複する記述は置きません。
