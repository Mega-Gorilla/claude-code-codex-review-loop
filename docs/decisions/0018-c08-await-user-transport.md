<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0018: `AWAIT_USER`の搬送路と2経路の重複防止key

- Status: Accepted
- Date: 2026-08-26

## Context

implementation plan Section 2.2の正本は`advance(run_id) -> HOST_ACTION | AWAIT_USER | TERMINAL`だが、PR-1（ADR-0014）が確定したのは`HOST_ACTION`側だけだった。`SUBMIT` v2は`action_id` / `action_kind` / `result_kind`を持つ`HOST_ACTION`専用のenvelopeで、**ユーザー入力をengineへ戻す形が存在しない**。したがって`advance`の3 outcomeのうち`AwaitUser`だけが行き止まりだった。

実測: C-01は`USER_INPUT_DECISION` / `USER_INPUT_GATE` / `USER_INPUT_PERMISSION`の3 awaitingで待ち、`AWAITING_COMMANDS`はいずれも**空tuple**（commandを発行しない）。よって`AWAIT_USER`は「発行済みcommand」ではなく**awaitingからの導出**である。

Phase 1計画はuser-input recordを2経路と定める。GitHub直接comment（経路2）はC-06が`accept_user_decision`でexternal evidenceとして受理し**再投稿しない**ため既に成立しているが、主経路（対話型sessionの入力をC-08がintentへ構造化し、内部record規約でGitHubへ転記する）のenvelopeが無かった。

**境界**: 意味解釈とgate semanticsはC-13（Phase 13）が所有する。本ADRが定めるのは搬送路（envelope / binding / 冪等 / 転記順序 / 重複防止key）に限る。

## Decision

### registryと語彙

1. **intent語彙を新設しない**。merge gateの4 intentは`schema/merge.py`の`MERGE_INTENTS`、判断回答は`USER_DECISION`として既に存在する。`USER_REQUEST_SPECS`はawaitingごとに**既存のrecord schemaを引く対応表**だけを持ち、結果payloadは`ACTION_SPECS`と同じく既存record schemaをそのまま再利用する（ADR-0014 決定4と同じ規則）
2. **result kind集合はC-01の`PRODUCED_RULES`と完全一致させる**。`USER_CANCEL`は全awaitingに入る（P-21はawaiting不問・非terminal全state）。awaitingを問わないruleは、**当該awaitingが滞在し得る全stateをruleが覆う場合にだけ**数える（`BLOCKED`限定のP-22 / P-23は滞在stateが重ならないので入らない）。この導出をcontract testに書き、driftを止める
3. **`input_route`の語彙をここで確定する**（`github_comment` / `host_transcript`）。`USER_DECISION`等のschemaが「入力経路の語彙はC-08 / C-06で確定」と留保していた点を閉じる。既存record schemaの`input_route`は`text()`のままにする（enumへの狭窄は非互換変更でversion bumpを要するため。ADR-0004 rule 2）。**engineが転記経路のrecordに対して値を検証**し、経路の詐称を止める
4. **`BLOCK_INTERVENTION`は含めない**。`BLOCKED`での介入は`ProgressBlock` / `ExternalDependencyBlock`を要するが、checkpointがまだそれらを表現しない（ADR-0017 決定8はhalt gateと`RECORD_INTEGRITY`まで）。表現を広げるPhaseが同じ形で追加する

### envelopeと判別

5. **`HOST_ACTION`用の`SUBMIT` v2は変更しない**。ユーザー入力は`USER_SUBMIT`という別variantで戻し、`submit`は**1つのentry pointのまま**両者を受ける（AC-C08-03）。判別は**`action_id` / `request_id`の排他**という構造で行い、「片方の定義で試して失敗したらもう片方」という推測経路は作らない。両方持つ／どちらも持たないenvelopeは`submit_unclassified`でfail closedにする。判別に使う2 keyが互いに素であることはcontract testで固定する
6. `result_kind`が無いsubmitは**`USER_INPUT_PERMISSION`に限る**（cross-field rule）。recordを作らない応答はtool permissionの明示resumeだけで、他のawaitingで種別を省略できると「何のrecordを作るか決めずに状態を進める」経路ができる

### 重複防止key

7. **key = `ui:{run_id, awaiting, since_seq, head_sha, kind}`のcanonical JSON**。`request_id`を唯一の相関keyにはできない: GitHub直接comment（経路2）は`AWAIT_USER`のrequest IDを持たないためである。両経路がcheckpointから導出できる値だけで構成する
    - **awaiting instance = `since_seq`**（request発行時点のchain最大seq）。同じstateとheadへ再び戻ってきた次のinstanceと、この値で区別される
    - **正規化intent = record kind**。merge gateではintentとkindが1対1で（`QUESTION`->`GATE_QUESTION` / `REQUEST_CHANGES`->`GATE_CHANGES` / `APPROVE_MERGE`->`MERGE_APPROVAL` / `CANCEL`->`USER_CANCEL`）、C-01は1 instanceにつきuser-input recordを1件しか受理しない（PRODUCED時にawaitingを消費する）
    - 区切り文字を含むopaque値でも衝突しないよう、sorted keysのcompact JSONで導出する（`identity.allowlist`の受理binding導出と同じ方式）
8. **同一keyが消費済みなら冪等**（`UserIntentAlreadyRecorded`）、**別keyなら停止**（`user_intent_conflict`）。後者は2経路がユーザーの意思について食い違っている状態で、どちらが正しいかを推測しない（「曖昧な入力を承認として解釈しない」と同じ原則）
9. key照合は**binding echoの直後、C-01の待機確認より前**に置く。別経路で決定済みのときに「requestが古い」とだけ返すと、runが壊れたのか決定が済んだのかを呼び出し側が区別できない。**どのbindingで、どの経路で確定したか**を返す
10. **本ADRが保証するのは「消費済みkeyが台帳にあれば重複を作らない」ところまで**である。経路2のcommentをGitHubから見つけて受理する（allowlist設定・`DecisionContext`の構成・consumed comment IDの管理）のはC-12 / C-13の責務で、C-08はkeyの導出・台帳の読み書き・engine側の判定だけを提供する。C-13は`accept_user_decision`の受理時に同じkeyを`with_consumed_intent`で書き、**未応答requestは残す**（消すと、遅れて届く転記submitへ確定bindingを返せない）

### 順序と検証

11. 順序は `binding echo -> 冪等判定 -> 重複防止key -> C-01がまだ待っているか -> result受理 -> head照合 -> 入力経路照合 -> chain gate -> render -> transaction -> RecordProduced`。以降の投稿・確認・検証は**PR-2bの`persist`をそのまま通る**（record kindで分岐しない汎用境界として作ったため、転記専用の実装を持たない）
12. **user-input recordの対象headはrequestのheadと一致しなければならない**。`FIX_RESULT`のようにpush後の新しいheadを対象にするrecordは無いため、`target_head_sha`も`approved_head_sha`も例外なくrequestが束ねたheadを要求する。ここを緩めると、承認を別headのrecordとして作らせる余地ができる（head binding。D-031）
13. **壊れたchainの上でユーザーへ判断を求めない**。`advance`はuser requestを払い出す前にchainを検証し、violationがあれば停止する。提示する根拠recordの正当性が確かめられていない状態では、承認をそこへbindできない
14. **転記本文は入力経路を明示する**。`prepare_public_body`はagent発言用でmodelの明示を要求するが、転記recordの内容を書いたのはmodelではなくユーザーである。`prepare_user_body`を追加し、同じ`改行正規化 -> sanitize -> redact`を通したうえでheaderに入力経路を書く（TE「Controllerが入力経路を明記して転記したPR comment」）
15. **採番から`RecordProduced`までをhost actionと共有する**（`produce_record`）。host actionの結果とユーザー入力の転記は本文の作り方と受理の記録だけが違い、chain gate・transaction発行・C-01の受理判定は同一である。分けて書くと同じ判断が2箇所へ散る

### checkpoint

16. **`user_request` sectionをadditiveに追加する**（新しいoptional sectionのためversion bumpなし。ADR-0004 rule 2 / 10）。`host_action`と別sectionにするのは、C-01がhost actionとユーザー入力を同時には待たない一方で、両者のbindingとreceiptの形が異なるためである
17. **`receipt`と`consumed`は単数**とする。上限を数字で決めたのではなく構造的な帰結である: engineはユーザー入力へretry attemptを発行せず（人間の入力にbudgetを課さない）、C-01は1 awaiting instanceにつきuser-input recordを1件しか受理しない
18. **新しいrequestはsection全体を入れ替える**。重複防止keyは`since_seq`を含むためinstanceを跨いで一致せず、前instanceのreceiptとconsumedを残しても判定に使えないまま伸びるだけである
19. **sectionは1回でまとめて読む**（`read_user_section`）。entryごとに呼び出し側で直和を捌くと、同じ「解釈できない」を3箇所で分岐することになる。読み出しの失敗は1点へ集約する

## Consequences

- Phase 8の`advance`は3 outcomeすべてが閉じ、`AWAIT_USER`が行き止まりでなくなった
- C-13は搬送路を実装せず、**intentの意味とgateの十分条件**（曖昧な肯定を承認と解釈しない、merge前提条件の再検証）だけを持てばよい
- **申し送り**: (a) D-031は「GitHub上のユーザー判断」の受理主体を縛るもので、転記経路の権限根拠はlocal session制御である。この非対称をC-13が明示的に決める必要がある。(b) `MERGE_APPROVAL` schemaは`comment_id`必須だが、転記recordでは自身のcomment IDが投稿前に確定しない。Phase 8は`opaque()`として素通しし、意味の確定をC-13へ送る
- AC-C08-01 / 02 / 04 / 06（adapterとprocess境界）はPR-3が扱う
