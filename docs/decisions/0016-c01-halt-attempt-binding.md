<!-- SPDX-License-Identifier: Apache-2.0 -->

# ADR-0016: integrity halt gateのattempt bindingを violation 集合から分離する

- Status: Accepted
- Date: 2026-08-25

## Context

integrity violationを検出したactive stateは、`HaltingForBlockProcedure`（halt gate）へ入り、`HaltRun`でprocess treeを停止させ、完了eventを受けてから`BLOCKED`へ遷移する（I3）。この停止attemptの識別に、これまで`block.representative_binding`（violation集合のcanonical order先頭）を使っていた。

しかしI5は「停止gate中の追加検出で上書き・silent lossを作らない」ことを要求し、追加違反はblockへunionされる。violation bindingは`iv:<condition>:<run_id>:<subject>`（`identity/record_chain.py`の`_violation`）で、辞書順は**condition名が主キー**（`actor` < `chain` < `edited` < `gap` < `marker` < `missing` < `seqconflict` < `tamper`）であり、**検出順との単調性が無い**。したがって後から検出した違反が代表になり得る。

実測した系列（Issue #40）:

```
1) HaltRun(v-2) を発行
2) 停止中に v-1 を検出 -> violations ['v-1','v-2'] / representative が v-1 へ
3) resumeの再発行は HaltRun(v-1)              <- 別attemptになる
4) BlockHaltCompleted(v-2) が REJECTED        <- 発行した停止の完了報告が拒否される
```

C-03へ渡した停止attemptの identity が、C-01側で入れ替わっていた。

## Decision

1. **identityとviolation集合を別の値にする**。`HaltingForBlockProcedure`へ不変の`attempt_binding`を持たせ、発行・resumeの再発行・完了eventの照合はすべてこの値を使う。violation集合は追加検出で伸びる別の値として扱う（unionは維持する）
2. **I5と安定性は、同じ値へ載せている限り両立しない**。I5は集合が伸びることを要求し、代表値はその先頭なので必然的に動く。分離以外に両方を満たす方法が無い。これが本ADRの中心的な理由である
3. `attempt_binding`は**halt gateへ入る契機となった違反のbinding**（`_detect_halt_gate_effect`が受け取ったevidence）とする
4. **不変条件**: `attempt_binding`は`block.violations`のいずれかと一致しなければならない。unionは集合を伸ばすだけなので追加検出後も保たれる。違反は`IllegalMachineStateError("HALT_ATTEMPT_BINDING")`で**構築段階から拒否**する。checkpointから復元する際に、blockと無関係なattempt bindingを注入した状態を作れない（C-08が復元を実装するときの防御）
5. `BlockHaltCompleted.block_binding`を**`attempt_binding`へ改名**する。実体は「blockの現在の代表値」ではなく停止attemptの識別子であり、`CancellationCompleted.attempt_binding`と同じ語彙にすることで2つの停止手続きが揃う。C-01内部のeventで外部互換性の制約は無い
6. **`attempt_binding`を`BLOCKED`へ持ち込まない**。halt attemptは`BLOCKED`到達で終わるため、`_halt_completed_effect`は`block`だけを渡す

### block解消側（`BLOCK_INTERVENTION`）は変更しない

7. `RECORD_INTEGRITY` blockの解消照合（`_resolution_matches`）は`representative_binding`と現在のviolation集合全体の一致を要求する。これは**stale evidenceを拒否するための仕様**であり、halt gateとは目的が逆である。新しい違反が見つかった以上、それを知らずに作られた解消evidenceを拒否するのが正しい。同じ形へ揃えない

### C-08への引き継ぎ

8. **attemptのidentityをC-06のchain検証から再導出してはならない**。violation集合はresumeのたびに再導出できるが（ADR-0011 決定8）、identityは再導出すると同じ問題が再発する。したがってC-08がprocedureをcheckpointへ保存するとき、`attempt_binding`は**保存する**側になる（実装はIssue #13のPR-3）

## Consequences

- 停止gate中に何件違反が増えても、発行した`HaltRun`の完了報告が受理される。追加違反は`BLOCKED`のblockへ残る（I5を維持）
- 代表bindingでの完了eventは受理されなくなる。停止attemptの識別子ではないため正しい挙動である
- `HaltingForBlockProcedure`の構築点はすべてkeyword指定にし、fieldの追加漏れを型検査で落とす
- 正本の扱い: Phase 1計画は「完全遷移・guard排他・到達可能性の正本はC-01実装のcode registryとtest」と定めているため、同計画は改訂せず本ADRと`domain/values.py`のdocstring、およびI系列testで契約を固定する
