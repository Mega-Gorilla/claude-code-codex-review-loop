<!-- SPDX-License-Identifier: Apache-2.0 -->

# Architecture planning

次のimplementation planで、以下のcomponentと依存関係を定義します。

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

初期実装Issueは、この文書をarchitecture planへ更新し、レビュー・合意した後に発行し、親roadmap Issue #2から参照します。
