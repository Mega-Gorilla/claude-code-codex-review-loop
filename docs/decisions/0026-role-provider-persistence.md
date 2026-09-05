<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0026: role設定の永続化とruntimeへのbinding

- Status: Accepted（C-02 / C-08の技術判断。D-032の採択とは独立）
- Date: 2026-09-05

## Context

Issue #52のPR-B1（ADR-0025）はcodecと事前検証interfaceを実装したが、保存場所とruntimeへの接続は未実装だった。PR-B2は既存の単一step engineを維持したまま、この設定をrunへ結び付ける。D-032はProposedのままであり、実装許可や本ADRのAcceptedをユーザー要件の採択記録に代えない。

## Decision

### 保存形式と初期化

1. `session.json`へoptionalな`agent_selection`（AGENT_SELECTION v1の全snapshot）、`agent_initialization`（preparing / ready）、`agent_initial_checkpoint`（初期checkpointのSHA-256）を追加する。選択付きrunは3項目すべて必須で、欠落を既定値で補わない。nested snapshotの意味検証もAGENT_SELECTIONのcodecへ委ねる。
2. checkpointへoptional section `agent_selection: {digest: <snapshotのSHA-256>}`を追加する。checkpoint v1 / v2に同じ追加を行い、既存migrationはこのsectionを保存する。既存fieldの意味・CODEX識別子・HOST_ACTION / SUBMITのversionは変更しない（ADR-0004のadditive変更）。
3. `initialize_agent_session`は新run専用のPython APIとする。C-12が設定の解決と初期stateを所有し、本APIは明示されたsession・checkpoint・selectionを検証して保存する。起動commandや初期workflow状態を推測しない。正式CLIのinit commandや設定優先順位は先取りしない。
4. checkpoint guardの下で、**preparing session → bound checkpoint → ready session**の順にprivate atomic replaceを行う。ready以外のsessionはruntimeが読込時に拒否する。file 2個の同時atomic更新ではなく、readyを最後の公開点とする初期化protocolである。
5. 中断後の再試行は、preparing sessionがcanonical bytesで完全一致し、既存checkpointも初期payloadと一致する場合だけ許す。初期checkpointのhashもsessionへ含めるため、checkpoint作成前の中断でも別の初期stateへ変更できない。ready session・旧session・既存の別checkpointは上書きせず新runを案内する。完了済みrunのcheckpointが消えた場合も初期状態へ巻き戻さない。
6. process強制終了で残る`.guard`は、既存C-07の契約どおり取得失敗として停止する。生きている書き手のguardを自動削除しない。運用者が旧processの終了を確認してguardを解放した後、同一入力で再試行する。最終ready公開後の中断は初期化済みであり、再初期化せずresumeする。

### runtime境界

7. `step`の各反復、`submit_result`の受理前、`drive`のhost呼出直前で、disk上のsession選択・呼出側のimmutable選択・checkpoint digest・対象run / repository / numberを照合する。不一致時にcheckpoint、未完了envelope、receipt、transaction本文を修復・置換しない。
8. 旧runは3設定fieldとcheckpoint sectionの双方が無い場合に従来経路を維持する。選択の片側だけの欠落は旧run扱いにしない。旧runへproviderを後付けする初期化は拒否し、speaker / modelから推測しない。全fileを同時に書き換え／削除できる攻撃者への認証・改竄防止をhashで主張しない。
9. `HOST_ACTION`を返す前に`AgentExecution`の明示的なactive providerと信頼済みprobe集合でpreflightする。`drive`も同じ`step`を通る。module entry pointは`--active-provider`を受け取るが、値はhostの自己申告であってactor認証ではない。既定のnative probeは存在せず、設定しただけで実CLI対応済みとしない。
10. `AWAIT_USER`、受理済みsubmitの再送、recordの永続化、停止にCLI起動可否を要求しない。ただし設定bindingは照合する。preflight前に既存engineがenvelopeをlocal保存する場合があり、拒否時も未完了actionを残し、次回は同じnonceで再提示する。既存の未実装payload port等でpreflightより先に止まる場合もある。
11. 設定不一致でも記録済み緊急停止はlocal停止経路で実行してから拒否を返す。設定不一致を理由にprocess treeを放置せず、停止後に通常処理へ戻らない。checkpointのtarget／state自体を復元できない場合は既存の安全側停止に従う。
12. guardはruntimeの実行入口を対象とする。C-07のread-only resume観測はstate遷移を行わず、このPRでは変更しない。下位workflow primitiveを直接使う後続componentは、このruntime境界を通す必要がある。controller外の同一userによる同時file改変への排他的な防御や、role / provider / instanceを結果へ認証bindingする機能は本PRではない。

### 診断と安全性

13. 公開拒否には固定codeとroleを使い、初期化I/O例外のmessageは出さない。sessionやnative出力をCI artifactへ追加しない。checkpointへ入る新情報はdigestだけである。
14. native probe失敗の原因をevidenceへ安全に記録する責務・redaction・権限・保持期間はC-09で決める（Issue #52の申し送り）。本PRに未検証のlogging／credential機構を作らない。

## 検証と完了境界

- 4組み合わせ × active/headless設定で、fake hostによる共通runtimeの質問・回答・cancelを通す。これはnative adapterの実証ではない。
- 別processのadvance / submit / resumeで、選択・nonce・receiptと二重投稿防止を確認する。
- 初期化の各公開点でI/O失敗と実process終了を注入し、partial runの拒否と限定した再試行を確認する。
- 設定差替え・片側欠落・旧run・未知schema・対象不一致・CLI未対応を拒否し、緊急停止の安全経路も回帰する。
- 既存全test、両OSのCI、coverage floorを維持する。

本PRでも実Claude / Codex adapter、native認証・sandbox検証、C-10以降のworkflow、role別の出力provenanceは未実装である。AC-RP-01 / 06 / 07 / 09を部分的に進めるが、このPRだけで完了扱いにしない。Issue #52は実CLIによる4組み合わせの受入までopenを維持する。
