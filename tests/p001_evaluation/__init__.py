# SPDX-License-Identifier: Apache-2.0
"""P-001評価（ADR-0003）の再現用package。

corpus（tests/p001_corpus/）に対して2候補のvalidatorを同一interfaceで実行し、
verdict / stage / error path / public errorの一致をCIで検証する。
productionのvalidator実装ではない（採用実装はPhase 2 / C-02）。
"""
