<!-- SPDX-License-Identifier: Apache-2.0 -->

# Claude Code–Codex Review Loop

Claude Codeをcoder、Codexをread-only reviewerとして使用し、GitHub Issue / PRを正式な会話履歴として、人間の明示承認まで開発ループを進めるためのプロジェクトです。

## Status

planningは完了し、実装開始前（Phase 0着手前）です。合意済みの[完成イメージ](docs/plans/target-experience.md)と承認済みの[実装計画](docs/plans/implementation-plan.md)に基づき、Phase順に実装します。

## Planned interface

推奨CLI名は`cc-review`です。

```powershell
cc-review pr 512 --repo OWNER/REPO
cc-review issue 123 --repo OWNER/REPO
```

長いaliasとして`claude-code-codex-review-loop`も予定しています。

## Documents

- [完成イメージ](docs/plans/target-experience.md)
- [実装計画](docs/plans/implementation-plan.md)
- [独立v2として開始する設計判断](docs/decisions/0001-independent-v2.md)
- [参考実装の調査と独立再実装の選択](docs/decisions/0002-independent-reimplementation.md)
- [Architecture](docs/architecture/README.md)

## License

本repositoryの現行成果物は[Apache License 2.0](LICENSE)で提供します。将来、第三者成果物を選択移植する場合は、そのPRで出典と適用ライセンスを追加します。

## Independence

本プロジェクトはAnthropicまたはOpenAIの公式プロジェクトではなく、両社から承認・支援されたものではありません。Claude Code、Codexおよび関連する名称は、それぞれの権利者に帰属します。
