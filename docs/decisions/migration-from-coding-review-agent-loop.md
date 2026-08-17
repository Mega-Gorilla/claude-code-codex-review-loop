# coding-review-agent-loopからの移行記録

## Source lineage

- Original: <https://github.com/wwind123/coding-review-agent-loop>
- Historical fork: <https://github.com/Mega-Gorilla/coding-review-agent-loop>
- Target-experience PR: <https://github.com/Mega-Gorilla/coding-review-agent-loop/pull/3>
- Agreed plan merge commit: `72c8b77a2c76e33bb56971d3c610d0e236befa0f`
- Agreed plan head: `4e6080e6e2719092347996cea401e38a908c7c29`

## Naming decision

| Item | Name |
| --- | --- |
| Repository / product | `claude-code-codex-review-loop` |
| Recommended CLI | `cc-review` |
| Long CLI alias | `claude-code-codex-review-loop` |
| Python package | `claude_code_codex_review_loop` |
| Claude Code Plugin / Skill | `cc-review` |

## Migration policy

- 合意済み完成イメージを新repositoryの設計baselineとして引き継ぐ。
- 旧実装sourceは一括copyしない。
- 選択移植する場合は、対象file、source commit、理由、適用license、移植後testを同じPRへ記録する。
- 旧repositoryのIssue / PR discussionは改変せず、URLで参照する。
- 新規成果物の主ライセンスはApache-2.0とする。
