<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0025: role別provider設定のcodecとadapter事前検証契約

- Status: Accepted（C-02 / C-08の技術契約。D-032のユーザー判断とは独立）
- Date: 2026-09-05

## Context

D-032はcoder / reviewerへClaude Code / Codexを独立設定する拡張案である。PR #53は提案文書を取り込んだが、GitHub上のユーザー明示合意recordが無いためD-032はProposedのままである。その後、会話でユーザーから続きの実装を許可された。本PRはその許可の下で契約を実装するが、D-032を暗黙に採択しない。

既存C-08の`session.json`は実行基盤の設定であり、speaker / modelからagentの実行先を導出できない。また`HeadlessHost`は共通protocolの実行基盤で、native CLIへのadapterではない。設定の新規導入と、全resume経路への組込み・安全性検証を1つのPRへ混ぜず、Issue #52のPR-Bを次の2段階へ分割する。

- **PR-B1（本PR）**: schema、immutableな解決済みsnapshotのcodec、復元時の比較、adapter事前検証interface、fake contract test
- **PR-B2（後続）**: 保存位置・atomic初期化・checkpointとのbinding、全entry pointとresume経路へのguard組込み、起動前検証結果の扱い、crash / legacy run回帰test

本PRには製品の設定file保存・CLI option・agent実行・出力正規化・GitHub記録へのprovenance追加を含まない。既存の`step` / `submit_result` / `drive`へ新しい進行判断を足さない。したがって現行CLIからproviderを切り替えることはまだできない。

## 技術契約

### 1. C-02所有の独立schema

`AGENT_SELECTION`を新しいprotocol kindとしてregistryへ登録し、`schema_version: 1`を持つ。既存SESSION_CONFIG / CHECKPOINT / HOST_ACTION / SUBMITのversionやfieldは変更しない。record kindではなく、GitHub canonical recordも増やさない。

全fieldは必須で既定値を持たない。設定の優先順位解決はC-12、ここが受け取るのは解決済みの明示値である。未知field・未知provider・未知schema version・不正な型は共通validatorで拒否する。

| Field | 意味 |
| --- | --- |
| run_id / repository / number | 復元先のrunと対象をbind。run IDは既存の共通検証、repositoryはC-05のRepoRefと同じ文字集合、numberは1以上 |
| coder.provider / reviewer.provider | CLI製品の識別子`claude` / `codex`。表示名やmodel名とは独立 |
| coder.model / reviewer.model | 明示的な非空・制御文字なしの文字列。modelの実在や利用可能性はproviderのprobeで検証 |
| coder.mode | `active` / `headless`。別provider・別方式へのsilent fallbackなし |
| reviewer.mode | `fresh`のみ。既存coder sessionをreviewerとして再利用しない |
| coder.safety_profile | `coder_workspace`のみ |
| reviewer.safety_profile | `reviewer_isolated`のみ |
| 各roleのadapter_contract_version | 1以上の整数。対応adapterのversionと完全一致を要求し、未知versionを他のversionへfallbackしない |

profile名は役割の安全要件を識別するための値で、Claudeのpermission modeやCodexのsandbox flagではない。これらの文字列を設定しただけでOS隔離が成立したとは扱わない。任意argv、環境変数、認証材料、hooks / MCP設定をこのsnapshotへ持ち込むfieldは設けない。model文字列等に利用者が秘密を入れないことも必要で、schema自体はsecret検知器ではない。

### 2. 不変snapshotと復元比較

`decode_selection`は共通validatorを通したpayloadから、nested値もfrozenな`AgentSelection` / `RoleSelection`を返す。呼出元のmutable辞書と共有しない。`to_bytes`はcanonical JSONを返し、digestはそのSHA-256とする。digestは変更検出用であり、認証・署名・改竄耐性を提供しない。

`restore_selection`は保存済みbytesと期待するrun / repository / numberを照合する。requested設定が与えられた場合、providerだけでなくmodel・mode・安全profile・adapter契約versionのどれが違っても新runを要求する。秘密値や入力文字列をerrorへ反映しない。

snapshotを持たない旧runは、新契約では`selection_missing_new_run_required`で拒否する。speakerからのprovider推測や旧データへの意味的な補完は行わない。既存C-08の旧run経路は本PRでは無変更であり、旧runがこのPRによって動かなくなることはない。旧runの明示migrationを将来認める場合は別ADRを要する。

**本PRの別process testはcodecの往復だけ**である。snapshotの実保存、変更禁止、checkpointとsession設定の相互照合、投稿前後crashの保証はPR-B2で初めて成立する。設定fileを両方書き換えても防げるといった認証上の主張はしない。

### 3. adapter事前検証interface

`AdapterKey`はrole / provider / mode / safety_profile / contract_versionの組である。同じproviderでもcoderとreviewerを別keyで選ぶ。`AdapterProbe.check`は選択したmodelを含むRoleSelectionを受け、CLI・認証・必要な安全機能・対応versionの確認結果を構造化して返す契約とする。

`preflight_selection`はsnapshotを再検証し、active経路では呼出側が明示したactive providerとの一致、headless経路ではactive providerが渡されていないことを要求する。全keyを登録検査し、重複key、未登録adapter、probeによる未対応の申告を拒否する。どちらかのroleが失敗したら別providerへ切り替えない。既定のnative probeは無く、空の登録集合は必ず失敗する。

probeは製品側が信頼する実装を渡すもので、外部JSONやagent出力から生成しない。fakeの成功はprobe interfaceの検証であり、認証やOS隔離の実証ではない。実行session・credential分離、process停止・timeout、native出力の正規化はC-09と後続adapter PRが実装する。最終報告のprovider選択も今回確定しない。

`key`はI/Oせず例外を送出しない固定metadataとする。`check`は既知の未対応を`cli_missing` / `authentication_unavailable` / `capability_unavailable` / `version_unsupported`で返し、検証処理自体の失敗は`probe_error`で返す。native processのtimeout・停止・回収はprobeの責務であり、この同期interface自体は期限を強制しない。

`preflight_selection`も`check`が送出した`Exception`を防御的に`probe_error`へ変換する。例外message・native出力・認証材料は公開結果へ含めず、`KeyboardInterrupt` / `SystemExit`等の`BaseException`は握り潰さない。roleに紐付く拒否は`SelectionRejected.errors`へ`PublicError(code, role)`を付け、coder / reviewerのどちらが失敗したかを固定語彙で示す。最初の失敗で停止し、後続probeの実行・別providerへのfallbackはしない。

## 検証と完了境界

- 4組み合わせ × coder active/headlessのcodec・fake事前検証
- roleごとのmodel独立性、同一model許容、mutable入力からの分離
- 欠落・未知field・未知version・不正profile・不正mode・size / UTF-8 / JSON違反の拒否
- 別processから同一bytesを復元、全選択項目の変更・別target・旧snapshot欠落を拒否
- active provider不一致、未登録・重複・role違い・安全profile違い・version違いのadapterを拒否
- role付きの未対応／probe_error拒否、例外messageの非露出、割込みの伝播
- 既存C-02 registryの全kind検証へ新kindを含め、既存全testを回帰実行

AC-RP-01 / 06 / 07 / 09の一部を準備するが、**いずれも本PR単独では完了扱いにしない**。active/headlessのengine同値性は未検証で、実CLIも起動しない。D-032はProposedを維持し、Issue #52は4組み合わせの実接続受入までopenのままとする。製品名・package名・CLI名や永続化済みCODEX識別子は変更しない。
