<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0006: C-04 security policyの評価意味論

- Status: Accepted
- Date: 2026-08-21

## Context

implementation planはC-04を「GitHubへ問い合わせずに評価できる純粋なpolicyだけを持つ」componentとし、redactionのpattern一元管理、fork PR / allowlist外author / agent設定file変更へのtrust rule、permission bypass flagの構築禁止（P-006）を要求する（AC-C04-01〜03）。参考実装は「redactionとtrust判定が純粋関数として分離されていない。permission構成はP-006に反する」ため全面新規設計である。本ADRはPhase 4で決めた技術判断（pattern集合、境界の意味論、fail closedの向き）を記録する。

## Decision

### Redaction（`policy/redaction.py`）

1. **patternは`REDACTION_PATTERNS`の単一registryで管理**し、consumer（C-05の投稿本文、C-07のprompt、C-08のlog、C-12のartifact）は独自patternを持たず`redact()`を呼ぶ。pattern追加はadditive変更として行う
2. 語境界は`\b`でなく**ASCII lookaround**（`(?<![0-9A-Za-z_])` / `(?![0-9A-Za-z_])`）を使う。漢字・かなはUnicodeの語文字であり、日本語隣接では`\b`が境界にならずtokenを取り逃す（実測で確認）
3. 置換は`[REDACTED:<pattern名>]`（名前の字母は`[a-z-]`）。**markerはどのpatternにも再matchせず、redactは冪等**（property testで常設検証）。結果のhitsはpattern名と件数のみで、秘密値そのものを保持しない（P-015）
4. PEM private keyは (a) BEGIN/END対（本体は長さcap `{0,10000}?`。実PEMは数KBであり、capは敵対的入力での超線形時間を防ぐ）、(b) **END欠落（logの切詰め等）を素通りさせないunterminated fallback**（BEGINから末尾まで安全側でredact）の2段で扱う
5. **既知のFP / FN（許容として記録）**: `authorization: token required ...`のような文は後続1語がredactされる（安全側のover-redaction）。`${{ secrets.X }}`の参照はredactされない（秘密値でないため正しい挙動）。`github_pat_`は本体60文字以上のみを対象とし、短い識別子とのFPを避ける
6. redactionはschema検証の後、公開用render / 投稿の前に適用する（target experienceの順序）。予約markerの除去 / escapeはC-05の責務であり、redactionとは別の変換である

### Trust rule（`policy/trust_rules.py`）

7. 判定は`TrustInput`（base / head repository、author login、trusted_authors、changed_paths）だけで決定論的に再現できる（AC-C04-03）。GitHubへの問い合わせ・actor解決は行わない
8. **fail closedの向き**: trusted_authorsが空なら常にuntrusted。head / base repositoryが空（fork元削除等でnull）ならforkとして扱う。repository名の比較は`strip().casefold()`（GitHubのowner/repoはcase-insensitiveであり、case差でfork誤検知するとallow側でなくdeny側に倒せないため正規化する）
9. **author照合は完全一致（case-sensitive）**。case差は不一致= untrusted（deny側）に倒れる。loginの正規化が必要ならC-06がallowlist / trusted集合の構築時に行う。承認受理のallowlist照合（D-031、fail closed）はC-06の責務であり、C-04のtrusted_authorsは実行gating用の別集合である
10. agent設定fileの判定は**path component境界**で行う（`\`→`/`・casefold・空と`.`の除去後に、末尾componentの`claude.md` / `agents.md`、任意位置の`.claude` / `.codex`、隣接対`.github`+`workflows`）。`.claudex`等の部分一致は構造的に発生しない。`..`は解決しない（GitHubのchanged file一覧に現れない前提）
11. 判定結果は2用途を分離する: `display_prominently`（目立つ表示。設定file変更 / fork / untrustedのいずれか）と`denied_actions`（既定拒否。fork or untrustedでagent instructions / hooks / workflow / testの全種）。**既定拒否のoverride機構は持たない**（緩和はD-NNNのユーザー判断を要する）

### Permission profileと禁止flag（`policy/permission_profile.py`）

12. profileの値域はAuto / `acceptEdits` / `default` / `dontAsk`のenumのみとし、**bypass系はenumに存在せず構築経路を持たない**（P-006）。Auto mode利用可否の検出はC-06が行い、C-04は可否を入力に取る純粋な選択規則（不可時: 自動化=acceptEdits / 対話=default / 非対話=dontAsk）だけを持つ
13. 禁止flagの検査は2層とする: (a) **contract test**（`tests/test_repository_contract.py`）が非`.md`のtracked file全件を区切り可変・大小無視のregexで走査する、(b) **runtime choke point**（`ensure_argv_allowed`）がargv構築後の値を同じregexで検査し、違反を構造化error（違反値をmessageへ含めない）で拒否する
14. **既知の限界（記録）**: 分割連結で構築される禁止flagは静的走査では原理的に検出できず、(b)のruntime検査とレビューが補完する。文書（`.md`）は「使用不可にする」という禁止の記述自体を含むため走査対象外とする。将来prompt templateを`.md`で持つ場合は、一律除外をやめてdocs配下限定の除外へ切り替える。検査regexとpattern名は禁止語の隣接列を含まない形にし、自己検出を構造的に避ける（除外list機構は持たない）

## Consequences

- C-05以降のconsumerはredaction patternを一切持たず、投稿 / prompt / log / artifactの各choke pointで`redact()`を呼ぶだけでAC-C04-01の4面が揃う
- trust判定のfail closedは常にdeny側（fork扱い / untrusted扱い）へ倒れ、誤検知は「表示が増える・実行が拒否される」方向にのみ現れる
- 後続Phaseでagent CLIのargvを組み立てる箇所は`ensure_argv_allowed`を通すことで、P-006の保証がcode reviewだけに依存しなくなる

## 実装への反映

`src/claude_code_codex_review_loop/policy/`（redaction / trust_rules / permission_profile）、`tests/test_c04_*.py`、`tests/test_repository_contract.py`の禁止flag testが本ADRを実装する。implementation planの品質ゲート表のcontract test行（禁止flag）は本Phaseで導入済みとなった。
