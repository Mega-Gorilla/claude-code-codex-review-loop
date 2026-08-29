<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0023: `BLOCK_INTERVENTION`の搬送路と待機識別子の直和

- Status: Accepted
- Date: 2026-08-29

## Context

PR-3b3（#47）でAC-C08-01〜07がすべて揃い、Phase 8に残る行き止まりは2つになった。本ADRはそのうち「`BLOCKED`からのユーザー介入」を扱う。

**C-01側は完成している**。`BLOCKED`で`BLOCK_INTERVENTION`の`RecordProduced`を受理する規則（P-22 / P-23）と、検証済みrecordの`BlockResolvedIntervention`で**保存した継続を1回だけ再現**する規則（B-IV1 / B-IV2）が既にある（AC-C01-11）。無いのは**C-08の搬送路**で、`advance`は`BLOCKED`に対し`Blocked`を返すだけだった。

ADR-0018 決定4は`BLOCK_INTERVENTION`を対象外とした理由を「checkpointが`ProgressBlock` / `ExternalDependencyBlock`を表現しない」と書いたが、**この前提はPR-3a（ADR-0019）で解消済み**である（`checkpoint_view._block_of` / `_block_entry`が3種すべてを扱う）。ADR-0019は残作業を「request identityが`awaiting` + `since_seq`ではなくblock bindingになるため、`USER_REQUEST` / `USER_SUBMIT`へ別のdiscriminatorを足すschema変更を伴う」と特定していた。本ADRはその変更である。

## Decision

### 待機の識別子を直和にする

1. **`awaiting`と`block_binding`を排他のdiscriminatorにする**。`AWAIT_USER`の待機はC-01の`awaiting`で識別するが、`BLOCKED`の待機に`awaiting`は無く、識別子は解除対象の**block attempt binding**である。`USER_REQUEST` / `USER_SUBMIT` / checkpointの`user_request.pending`で`awaiting`をoptionalへ緩め、`block_binding`をoptionalで足し、**どちらか一方だけ**をcross-field ruleで要求する。両方あるenvelopeはどちらの待機を指すか決まらず、どちらも無いenvelopeは何の待機か決まらない。**どちらも推測せず拒否する**。checkpointのreaderも同じ規則でfail closeする。
2. **`Awaiting`へ架空の値を足さない**。C-01は`BLOCKED`にawaitingを与えていない。ここで語彙を作るのはC-01の語彙の捏造であり、ADR-0018 決定1（intent語彙を新設しない）に反する。
3. **version bumpしない**。ADR-0004 rule 2が非互換として挙げるのは削除・改名・型変更・**required化**であり、required -> optionalはその逆方向である。旧payloadは緩めたspecでもそのまま検証を通り、migrationが保証する方向（**新しいcodeが古いdataを読む**）は壊れない。緩めた分は決定1のcross-field ruleが構造で締める。

### block instanceの同一性

4. **instanceはblock attempt bindingそのもの**である。`since_seq`はenvelopeへ従来どおり載せる（chain位置の提示）が、**instance判定には使わない**。blockへ入り直せばbindingが変わるため、instanceの識別子として過不足がない。
5. `since_seq`を判定へ入れると、**chainが伸びただけで同じblockのrequestを作り直す**ことになり、そのたびに消費済みintentを捨てる。`AWAIT_USER`側が`since_seq`を使うのは、同じawaitingへ再到達したinstanceを区別する手段が他に無いからで、blockにはbindingがある。
6. 重複防止keyは**同じ`ui:`台帳**を使い、instance固有のfieldだけを差し替える（`{"block": binding}` / `{"awaiting": ..., "since": ...}`）。台帳を分けないのは、C-13が経路2（GitHub直接comment）で同じkeyを導けることが要件だからである（ADR-0018 決定7）。field集合が異なるので待機の種類を跨いだ衝突は起こらない。

### 介入を受け付けるblockはC-01が決める

7. **`BLOCK_INTERVENTION`を受理するblockでだけrequestを出す**。C-01の写しを`BLOCK_REQUEST_SPECS`として持ち、contract testが`PRODUCED_RULES`との一致を固定する。

    | block | 介入request | 出口 |
    | --- | --- | --- |
    | `ProgressBlock`（`NO_PROGRESS`） | 出す | P-22 -> B-IV1 |
    | `ExternalDependencyBlock` | 出す | P-23 -> B-IV2 |
    | `ProgressBlock`（`LIMIT_REACHED`） | 出さない | limit引き上げ（B-LR） |
    | `RecordIntegrityBlock` | 出さない | 復元 / salvage専用evidence（fail closed） |

8. 対象外のblockで「cancelだけ受け付けるrequest」を出すことは**しない**。requestの意味は「C-01が受理する応答の提示」であり、受理されない選択肢を並べるとその意味が崩れる。これらのblockは`Blocked`のまま返す（ADR-0019 決定19は有効なまま）。
9. 結果種別は`BLOCK_INTERVENTION`と`USER_CANCEL`である。後者はP-21（awaiting不問・非terminal全state）が`BLOCKED`を覆うため、ADR-0018 決定2の導出規則をそのまま適用した結果として入る。**`BLOCKED`のrunをcancelできるようになったのはこの帰結**であって、別に足した機能ではない。

### 順序と検証

10. **chain gateはrequest発行の前に通す**。`Blocked`は状態を報告するだけなのでchainを読まないが、介入requestは**ユーザーへ判断を求めるturn**を起こす。壊れたchainの上で判断を求めない（ADR-0018 決定13）。
11. **受理時にtarget block bindingを照合する**。`BLOCK_INTERVENTION` recordは`target_block_binding`を持ち、C-01は解消時にblockとの完全一致を要求する（AC-C01-11）。ここが食い違うとrecordはGitHubへ投稿されるのに解消eventが一致せず、runは`BLOCKED`のまま詰まる。**投稿してから気付くのではなく、受理の時点で止める**。
12. `_still_awaited`のblock版は「**同じblockがまだあるか**」を見る。解消されたblockや別のblockへ入り直した後のrunへ、古いrequestの応答を流し込ませない。

### record -> eventの写像はportが担う

13. **`build_event`では`BLOCK_INTERVENTION`のeventを組み立てられない**。`BlockResolvedIntervention`が受け取るのは`RecordEvidence`ではなく`BlockResolutionEvidence`である。`ResultVariant`へ`port_mapped`を足し、`build_event`は**推測で埋めずに拒否**する。`extra_event_inputs`は「C-08が作らない**値**」を宣言するが、こちらはevidenceの**形そのもの**が違う場合である。
14. 形だけの問題ではない。C-01の完全一致照合（`_resolution_matches`）は、対象bindingとheadに加えて**blockが持つ値**（reason / budget / counter snapshot / fingerprint）まで一致を要求する。これらはC-10 / C-11が所有する値で**C-08は作らない**。写像がportの責務であることの実質的な理由がここにある（ADR-0017 決定2）。
15. したがって本PRが主張するのは**搬送路の完結**であって、実portの実装ではない。Phase 8のfakeは、C-10 / C-11の実装が置かれる位置を示すものである。

## Consequences

- **`BLOCKED`からのユーザー介入が閉じた**。`BLOCKED` -> 介入request -> ユーザー入力 -> canonical record転記 -> 継続の再現、が1本で通る
- **`BLOCKED`のrunをcancelできるようになった**（決定9の帰結）。ただし介入を受け付けるblockに限る
- **`Blocked` outcomeは残る**。出口がC-08の外にあるblock（limit引き上げ / integrity復旧）はこれまでどおり状態を報告する
- **`RESULT_VARIANTS`の「record kindとeventは1対1」contractに例外ができた**。`port_mapped`で明示し、contract testを「evidenceから組み立てるvariant」に限定したうえで、port_mappedなvariantには別のcontractを置いた
- `PendingUserRequest.awaiting`が`Awaiting | None`になり、`AWAIT_USER`側の全経路がNone分岐を持つ。型（mypy strict）が漏れを止めている
- **残る行き止まりは`RecordIntegrityIncident`の実行だけ**になった（PR-3d）
