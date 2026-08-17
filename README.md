<!-- SPDX-License-Identifier: Apache-2.0 -->

# Claude Code–Codex Review Loop

Claude Codeをcoder、Codexをread-only reviewerとして使用し、GitHub Issue / PRを正式な会話履歴として、人間の明示承認まで開発ループを進めるためのプロジェクトです。

## Status

現在はarchitecture planning段階です。合意済みの完成イメージを基に、新しいControllerを独立設計します。旧`coding-review-agent-loop`の実装コードはこのrepositoryへ含めていません。

## Planned interface

推奨CLI名は`cc-review`です。

```powershell
cc-review pr 512 --repo OWNER/REPO
cc-review issue 123 --repo OWNER/REPO
```

長いaliasとして`claude-code-codex-review-loop`も予定しています。

## Documents

- [完成イメージ](docs/plans/target-experience.md)
- [独立v2として開始する設計判断](docs/decisions/0001-independent-v2.md)
- [旧repositoryからの移行記録](docs/decisions/migration-from-coding-review-agent-loop.md)
- [Architecture planning](docs/architecture/README.md)

## License

本repositoryの現行成果物は[Apache License 2.0](LICENSE)で提供します。旧repositoryの実装コードは含まれていません。将来、第三者成果物を選択移植する場合は、そのPRで出典と適用ライセンスを追加します。

## Independence

本プロジェクトはAnthropicまたはOpenAIの公式プロジェクトではなく、両社から承認・支援されたものではありません。Claude Code、Codexおよび関連する名称は、それぞれの権利者に帰属します。
