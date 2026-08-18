<!-- SPDX-License-Identifier: Apache-2.0 -->

# Architecture

componentと依存関係の定義は[implementation plan](../plans/implementation-plan.md)へ集約しています。

| 内容 | 参照先 |
| --- | --- |
| 設計原則と根拠 | implementation plan Section 2 |
| active host protocolと制御構造 | implementation plan Section 3 |
| 層構造とpackage layout | implementation plan Section 4 |
| component一覧と依存グラフ | implementation plan Section 5 |
| component定義と受入条件 | implementation plan Section 6 |
| requirement traceability | implementation plan Section 7 |
| 実装順序と子Issue | implementation plan Section 11 |

## Componentの再編

当初この文書が挙げていた10項目は、implementation planでC-01〜C-13へ再編しました。順序矛盾の原因になっていた2項目を前段と後段へ分割し、横断的なsecurity境界を独立componentとして追加しています。

| 当初の項目 | 再編後 |
| --- | --- |
| 1. domain state machine、event、command | C-01 |
| 2. GitHub canonical conversation transportとread-after-write | C-03 |
| 3. Claude Code Plugin / active host adapter | C-06（active host protocol）、C-13（Plugin配布） |
| 4. Codex fresh reviewer runtimeと隔離checkout | C-07 |
| 5. PR modeとIssue-to-PR handoff | C-08（PR mode）、C-12（Issue mode） |
| 6. decision / clarification protocol | C-09 |
| 7. test・CI qualificationとfinal reporter | C-10 |
| 8. human merge gate | C-11 |
| 9. checkpoint、resume、artifact retention | C-05 |
| 10. Windows PowerShell、Linux/SSH、`tmux` wrapper | C-02（process abstraction）、C-13（任意wrapper） |
| （新規） | C-04 trust、permission、credential境界 |

実装子Issueはimplementation planのレビュー・合意後に発行し、親roadmap Issue #2から参照します。

このdirectoryには、実装が進んだ段階でcomponent単位の詳細設計や図を追加します。implementation planと重複する記述は置きません。
