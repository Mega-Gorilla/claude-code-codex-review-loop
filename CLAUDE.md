<!-- SPDX-License-Identifier: Apache-2.0 -->

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

既存baselineは承認済みで、Phase 8までの実行基盤が実装されています。両providerへの実接続と後続workflowの完成とは区別してください。role別provider選択の追加案D-032は`Proposed`です。設定契約・永続化・runtime照合は実装済み（ADR-0025 / 0026）、native adapterは未実装で、Issue #52が追跡します（2026-09-05時点）。

| 正本 | 役割 |
| --- | --- |
| `docs/plans/target-experience.md`（Status: **Agreed**） | 何を作るか。user-visible behaviorと合意済み制約。decision log（D-001〜D-032、D-032はProposed）を含む |
| `docs/plans/implementation-plan.md`（Status: **Accepted**） | どう作るか。設計原則P-002〜P-015、component C-01〜C-15、Phase 0〜17、受入条件AC-CNN-NNと横断条件案AC-RP-NN |

親roadmapはIssue #2、実装子IssueはPhase 0〜17に対応する#5〜#22です。最新の進捗を各Issueで確認し、dependency順に進めます。Phase 9 #14のruntime契約は追加案#52と調整します。

## Commands

```powershell
python -m pip install -e ".[dev,p001]" -c constraints/p001.txt   # 開発環境の準備（Python >= 3.11）
python -m pytest -q                 # 全test
python -m ruff check .              # lint
python -m mypy                      # type check（src対象、strict）
python -m coverage run -m pytest -q; python -m coverage combine; python -m coverage report   # coverage（floorはquality-baseline.toml）
python -m pytest tests/test_repository_contract.py::test_project_identity_is_consistent -q   # 単体test
git diff --check origin/main...HEAD # CIと同じwhitespace check
```

CI（`.github/workflows/test.yml`）はubuntu-latest / windows-latestのPython 3.11でlint、type check、coverage付き全test、coverage floor、`git diff --check`を実行します。品質baselineは`quality-baseline.toml`でversion管理し、緩める変更にはPRへの理由記載が必要です（CONTRIBUTING「品質ゲートの運用」）。

## Repository contract（testが強制する規約）

`tests/test_repository_contract.py`が検査します。検査対象は`git ls-files`から自動discoveryするため、手動のpath listはありません。

- 本project独自のfile（`.md` / `.py` / `.toml` / `.yml` / `.yaml` / `.ps1` / `.sh` / `.psm1` / `.psd1`）は**先頭3行以内に`SPDX-License-Identifier: Apache-2.0`**を置く。
- 選択移植した第三者成果物は元licenseのSPDX表示を保持し、同test内の`THIRD_PARTY_FILES`へpathとSPDX IDを登録する（未登録だとApache-2.0を要求されてfailする）。
- product名は`Claude Code–Codex Review Loop`（en dash `–`）。package名は`claude-code-codex-review-loop`、CLIは`cc-review`。
- `docs/plans/target-experience.md`は`| Status | **Agreed** |`を保持し、implementation planはbaselineへのlinkとIssue #2参照を保持する。
- `docs/examples/`と`docs/research/`の文書はauthority marker（`Non-normative example` / `Research`）を保持する。
- 本repository以外の同一owner配下repositoryを文書へ書かない。旧CLI名とその技術namespace（contract testが正規表現で検査する）を書かない。
- permission bypass系flag（P-006）をcodeへ書かない。contract testが非`.md`のtracked file全件を区切り可変regexで走査する。

## Naming

| Item | Name |
| --- | --- |
| Product表示名 | `Claude Code–Codex Review Loop`（en dash `–`） |
| Repository slug | `claude-code-codex-review-loop` |
| Recommended CLI | `cc-review`（長いalias: `claude-code-codex-review-loop`） |
| Python package | `claude_code_codex_review_loop` |
| Claude Code Plugin / Skill | `cc-review` |

## 文書構成

読む順番とauthority定義の正本は`docs/README.md`です。新規参加時は`docs/architecture/overview.md`（1ページ）から読みます。用語は`docs/glossary.md`、開発手順は`CONTRIBUTING.md`が正本です。

| Authority | 意味 |
| --- | --- |
| `Agreed` / `Accepted` | normative。要件および合意済みの制約 |
| `Draft` | review中で未確定 |
| `Research` | informative。判断材料であり要件ではない |
| `Non-normative example` | 例示。正本と食い違えば正本を優先 |

文書は正本を複製せず、安定ID（`D-NNN` / `AC-CNN-NN` / `AC-RP-NN` / `DOD-NN` / `MVP-NN` / `P-NNN`）とlinkで参照します。`AC-RP-NN`はrole / provider横断の受入条件で、単一の所有componentを持たず、Issue #52で横断追跡します（D-032の合意までは条件案）。節番号は編集で変わるため参照に使いません。

## Architecture（設計上の中心的な制約）

製品は、coderとread-only reviewerがGitHub Issue / PRを正式な会話履歴に据え、人間の明示承認までを進めるdevelopment loopです。承認済みbaselineはClaude Code coder / Codex reviewerです。両roleへClaude Code / Codexを独立設定するD-032は`Proposed`であり、GitHub上の明示合意recordを得るまでbaselineを置き換えません。以下のrole名による説明はprovider対応済みを意味しません。

**5つのrole**: User（実行開始と明示承認）/ Controller（LLMを内包しない決定論的state machine）/ host・coder（既存の対話型sessionのまま実装を担当）/ reviewer（turnごとに新規起動するdurable read-only subprocess）/ final reporter（承認済みheadの変更と検証履歴をread-onlyで説明）。同一providerでもcoderとreviewerのsession・権限を共有しないことがD-032案の条件です。

**起動主体**: 主経路のcoderはactive hostが実行し、Controllerは起動しない。headless復旧経路のcoderだけはControllerがsubprocess adapterとして起動する。reviewerとfinal reporterはControllerがfresh subprocessとして起動する。

**active host protocol**（implementation planの「active host protocol」節）: Controller CLIはactive coder sessionの外部toolとして呼ばれ、親のLLM turnを呼び戻せない。そのため主経路のcore engineはcoderを起動せず、`advance`で次の`HOST_ACTION`を返し、active hostが自分のcontextで実行して`submit`で結果を返すstep engineとする。この制御反転は全workflowの前提であり、覆すと全体の書き直しになる。

**6つの不変条件**（`docs/architecture/overview.md`）:

- GitHub canonical: workflowへ影響する各turnは、次agentを起動する前にGitHubへ投稿しread-after-writeで確認する。未永続化の出力を根拠にしない
- fresh reviewer: reviewerはturnごとに新規起動し、前roundやcoderのsession memoryへ依存しない
- active coder: 主経路ではcoderをsubprocess化せず、対話sessionのcontextを維持する
- durable read-only: reviewerは隔離checkout内の一時書込だけを行い、実repositoryとGitHubを変更しない
- head binding: review承認とmerge承認は特定のhead SHAへ結び付き、headが変われば失効する
- human merge gate: mergeはユーザーの明示承認後にControllerだけが実行する。曖昧な肯定を承認と解釈しない

**security境界**: C-05は未検証metadataのI/Oに限定し、canonical recordの検証と生成はC-06が行う。C-07以降は検証済みrecordだけを入力にする。ユーザー判断の受理はGitHub login allowlistとの完全一致が必須（D-031、fail closed）。permission bypass flagをcode上で構築しない（P-006）。

**state model**: 17 state（`RUNNING_REVIEW`〜`MERGED` / `MERGE_FAILED` / `BLOCKED` / `FAILED` / `CANCELLED` / `REPORT_FAILED`）。遷移図はtarget-experienceの「State model」節。agentまたはユーザーの発言を伴う全遷移はGitHub永続化gateを通過する。

**除外事項**: PR自動検知 / watcher / webhook、対話型TUIへのキー入力注入、既存対話sessionのreviewerとしての再利用、無人auto-merge、MCP serverとしての実装・配布（D-026）、独自daemon。

## Working conventions

- **decision flow**: target behaviorを変える決定はD-NNNとしてdecision logへ追記する。`Open` / `Proposed`を`Decided`へ変更できるのは、GitHub上のユーザーの明示合意recordだけ。implementation planが単独でgold documentのstatusを変えてはならない。Phase内で決める技術判断（P-001等）はユーザー判断と区別する。
- **開発フロー**: `CONTRIBUTING.md`が正本。Issue → `agent/<topic>` branch → dependency順の小さいPR → Codexレビューでblocking解消 → **ユーザーの明示承認を得てmerge**。レビュー承認はmerge承認ではない。
- **選択移植**: ADR-0002に従う。`docs/research/reference-implementation-assessment.md`のcomponent別判定が入口。移植PRへ対象file・source commit・理由・適用license・移植後testを記録し、`THIRD_PARTY_FILES`へ登録する。参考実装のrepository識別子とcommit SHAを正式文書へ書かない。
- **文書language**: 文書・commit messageは日本語（技術用語は英語のまま）。書き方の規約はCONTRIBUTING「文書の書き方」。
- **gh操作**: Issue / PR番号は作業directoryのremoteへ解決されるため、`--repo Mega-Gorilla/claude-code-codex-review-loop`を必ず明示する。
- **commit内容の確認**: commit済み内容の確認には`git show --stat <sha>`を使う（`git diff HEAD~1`は作業treeとの比較になる）。
