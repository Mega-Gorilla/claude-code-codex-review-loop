<!-- SPDX-License-Identifier: Apache-2.0 -->

# Final report例

| Field | Value |
| --- | --- |
| Authority | **Non-normative example** |
| 正本 | [target experience](../plans/target-experience.md) Section 11 |

この文書は書き方の例であり、要件ではありません。必須項目、出力形式、言語解決順は正本を参照してください。仕様と食い違う場合は正本を優先します。

## READY_FOR_HUMAN_MERGE時のPR comment

```markdown
## READY_FOR_HUMAN_MERGE

### Summary

`PR #512`は、WindowsとLinuxでagent processを安全に停止できるplatform abstractionを追加します。既存のPOSIX動作を維持し、Windowsでは子process treeがtimeoutやCtrl+C後に残らないようにします。

### Why

従来のrunnerはPOSIXのprocess groupに依存し、Windowsネイティブでtimeoutとcancelを安全に処理できませんでした。

### User-visible changes

- PowerShell 7から同じCLIを実行できます
- Ctrl+C時にClaude / Codexの子processが停止します
- timeout時にresume可能な状態と原因を表示します

### Acceptance criteria

| Criterion | Result | Evidence |
| --- | --- | --- |
| Linux existing behavior | Pass | `pytest ...` |
| Windows child cleanup | Pass | Windows CI `process-tests` |
| Ctrl+C recovery | Pass | test and run log |

### Review history

- Round 1: Codex found that grandchildren survived forced timeout
- Commit `fedcba9`: Claude assigned the process tree to a Windows Job Object
- Round 2: Codex approved the exact head

### Validation

- Approved head: `fedcba9876543210`
- Local tests: 128 passed
- GitHub CI: `test` and `lint` passed

### Remaining risks and follow-ups

- Windows Store版PowerShellは未検証です
- SSH切断後の継続は対応`tmux` wrapper内で保証し、wrapper外ではGitHub checkpointからresumeします

#### Approved follow-up候補

- `followup-001`: Windows Store版PowerShellを検証する
  - Codex評価: `CREATE_ISSUE`。現在PRのacceptance criteriaには含まれないが、互換性riskの追跡に必要
  - 状態: ユーザー許可待ち（未許可のためIssue未作成、mergeはblockingしない）

### merge前の確認

1. PR headが`fedcba9876543210`のままであること
2. Windows runner結果を確認すること
3. Remaining risksを許容できること

この時点ではまだmergeされていません。Claude Code画面で質問・修正依頼・対象PRの明示的なmerge承認を入力できます。
```

## merge完了時の追記record

```markdown
## MERGED

- PR: #512
- Approved head: `fedcba9876543210`
- Merged commit: `1234567890abcdef`
- Merge method: repository policy
- User approval: canonical comment link
- GitHub verification: PR state `MERGED`
```
