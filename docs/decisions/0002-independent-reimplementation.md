<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0002: 参考実装を調査したうえで独立再実装を選択する

- Status: Accepted
- Date: 2026-08-17

## Context

本プロジェクトの着手にあたり、Claude Codeとreviewer agentを組み合わせる既存の参考実装を調査した。調査対象は<https://github.com/wwind123/coding-review-agent-loop>である。

参考実装は複数agentと複数modeを扱う汎用CLIとして設計されており、本プロジェクトが固定するrole構成、GitHub canonical conversation、人間のmerge承認gateとは対象範囲と責務境界が異なる。

## Decision

- 参考実装をforkして継続開発せず、本repositoryで独立に再実装する。
- 参考実装のrepository、Issue / PR番号、commit SHAを本プロジェクトの正式な設計根拠として参照しない。
- 参考実装の出典は本ADRに1か所だけ記録し、設計上の判断根拠は本repository内の文書とdecision recordで完結させる。

## Naming decision

| Item | Name |
| --- | --- |
| Repository / product | `claude-code-codex-review-loop` |
| Recommended CLI | `cc-review` |
| Long CLI alias | `claude-code-codex-review-loop` |
| Python package | `claude_code_codex_review_loop` |
| Claude Code Plugin / Skill | `cc-review` |

## Selective porting policy

- 合意済み完成イメージを本repositoryの設計baselineとする。
- 参考実装のsourceを一括copyしない。
- 選択移植する場合は、対象file、source commit、理由、適用license、移植後testを同じPRへ記録する。
- 現行成果物はApache-2.0とする。
- 第三者成果物を選択移植する場合は、元ライセンスと必要なnoticeを同じPRで追加する。

## Consequences

- 設計判断の根拠が本repository内で完結し、外部repositoryの可用性に依存しない。
- upstream merge conflictを通常運用として抱えない。
- 参考実装が備えていたGitHub、CI、resumeのedge caseを自動的には引き継がないため、選択移植時にprovenanceとbehavior testが必要になる。
