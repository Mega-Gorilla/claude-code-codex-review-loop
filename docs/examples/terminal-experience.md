<!-- SPDX-License-Identifier: Apache-2.0 -->

# Terminal表示例

| Field | Value |
| --- | --- |
| Authority | **Non-normative example** |
| 正本 | [target experience](../plans/target-experience.md) Section 6 |

この文書は表示の雰囲気を伝える例であり、要件ではありません。必須表示項目、state名、禁止事項は正本を参照してください。仕様と食い違う場合は正本を優先します。

## 通常表示

```text
Claude–Codex Development Loop
Repository : OWNER/REPO
PR         : #512 Improve process lifecycle handling
Base       : main @ 0123456
Head       : feature/process @ abcdef0
Round      : 2 / 3
State      : RUNNING_REVIEW

[12:10:03] PR and trust policy validated
[12:10:04] Fresh Codex reviewer started in read-only mode
[12:13:20] Review completed: 2 blocking findings
[12:13:21] Active Claude Code host started applying fixes
[12:19:48] Tests passed: 128 passed
[12:20:12] Pushed new head: fedcba9
[12:20:14] Starting fresh review for fedcba9

Skill state : GitHub CC_REVIEW_META + local session cache
Codex log   : .cc-review-logs/<run-id>-codex.log
```

## 監視pane

ユーザーが明示的に要求した場合だけ、任意wrapperが次のように配置します。

```text
+--------------------------------+-----------------------------+
| Claude Code host               | Codex reviewer log          |
| 対話・Skill・state・実装・承認 | fresh subprocessの進行・結果 |
+--------------------------------+-----------------------------+
```

## merge判断gate

```text
READY_FOR_HUMAN_MERGE

PR            : https://github.com/OWNER/REPO/pull/512
Approved head : fedcba9876543210
Review rounds : 2
Local tests   : PASS (128 passed)
GitHub CI     : PASS (test, lint)
Final report  : PRへ投稿・localへ保存済み

選択できる操作:
1. PR内容について質問する
2. 修正または追加検証を依頼する
3. 「`PR #512` のmergeを承認します」と明示する
4. 今回のrunをcancelする

現在はまだmergeされていません。曖昧な返答ではmergeしません。
```

## merge完了

```text
MERGED

PR              : https://github.com/OWNER/REPO/pull/512
Approved head   : fedcba9876543210
Merged commit   : 1234567890abcdef
Merge method    : repository policy
Approval record : https://github.com/OWNER/REPO/pull/512#issuecomment-123
GitHub state    : MERGED（再取得して確認済み）
```
