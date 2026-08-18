<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0001: 独立したv2として再設計する

- Status: Accepted
- Date: 2026-08-17

## Context

参照元の`coding-review-agent-loop`は、複数agentと複数modeを扱うstandalone CLIとして成長している。一方、本プロジェクトはClaude Codeをhost / coder、Codexをfresh read-only reviewerとして固定し、GitHub上の正式な会話履歴と人間のmerge承認を中心に設計する。

既存orchestratorへこのworkflowを追加すると、既存の汎用分岐、CI、resume、agent lifecycleとの結合が増え、本家追従と新しい責務分離を同時に維持する必要がある。

また、旧実装は本プロジェクトの使用用途と前提が異なり、技術負債も蓄積していた。既存forkを編集して作り替えるより、独立して再作成する方がcostと責務境界の両面で有利である。

## Decision

- 本プロジェクトをforkの継続開発ではなく、独立したrepositoryとpackageとして開始する。
- 旧repositoryを通常のmerge / rebaseで追従しない。
- 旧実装は参考実装とし、必要なedge case、test、utilityだけを出典付きで選択移植する。
- 新Controllerはdomain state、application workflow、external adapterを分離する。
- 旧巨大orchestratorを移植せず、純粋な状態遷移と副作用commandを中心に構成する。

## Consequences

- 新しい完成イメージに合わせて責務境界を設計できる。
- upstream merge conflictを通常運用として抱えない。
- 既存実装に蓄積されたGitHub、CI、resumeのedge caseを失わないよう、移植時のprovenanceとbehavior testが必要になる。
