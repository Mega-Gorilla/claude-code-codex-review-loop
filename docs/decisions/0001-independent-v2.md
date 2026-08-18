<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0001: 独立したv2として再設計する

- Status: Accepted
- Date: 2026-08-17

## Context

調査した参考実装は、複数agentと複数modeを扱うstandalone CLIとして成長している。一方、本プロジェクトはClaude Codeをhost / coder、Codexをfresh read-only reviewerとして固定し、GitHub上の正式な会話履歴と人間のmerge承認を中心に設計する。

既存orchestratorへこのworkflowを追加すると、既存の汎用分岐、CI、resume、agent lifecycleとの結合が増え、本家追従と新しい責務分離を同時に維持する必要がある。

既存実装は本プロジェクトと対象範囲・責務境界・運用モデルが異なり、直接拡張するには大幅な再構成が必要だった。

## Decision

- 本プロジェクトをforkの継続開発ではなく、独立したrepositoryとpackageとして開始する。
- 参考実装を通常のmerge / rebaseで追従しない。
- 参考実装から必要なedge case、test、utilityだけを出典付きで選択移植する。
- 新Controllerはdomain state、application workflow、external adapterを分離する。
- 既存の汎用orchestratorを移植せず、純粋な状態遷移と副作用commandを中心に構成する。

## Consequences

- 新しい完成イメージに合わせて責務境界を設計できる。
- upstream merge conflictを通常運用として抱えない。
- 参考実装が備えていたGitHub、CI、resumeのedge caseを失わないよう、移植時のprovenanceとbehavior testが必要になる。
