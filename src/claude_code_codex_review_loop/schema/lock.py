# SPDX-License-Identifier: Apache-2.0
"""PR lock fileのschema v1（C-07。ADR-0011）。

lock fileはcheckpointとは別のfileで、同一PRへの同時runを検出するための最小の識別情報
だけを持つ（同時run拒否のworkflow動作はAC-C10-03 = C-10の責務）。破損したlockを
「無いもの」として黙って上書きしないよう、独自parseを持たずC-02のvalidatorを通し、
構造化errorとして扱う。

`host`と`pid`はstale lock回収の3条件（pid非生存・host一致・run一致）に使う。
"""

from __future__ import annotations

from .registry import SchemaDefinition, SchemaKind, integer, opaque, schema_version_field, text
from .validate import VersionSpec

RUN_LOCK = SchemaDefinition(
    kind=SchemaKind.RUN_LOCK,
    versions={
        1: VersionSpec(
            fields={
                "schema_version": schema_version_field(),
                "run_id": opaque(),
                "repository": text(),
                "number": integer(),
                # 取得したprocessの識別（回収判定に使う。host跨ぎの回収は行わない）
                "pid": integer(),
                "host": text(max_len=255),
                "acquired_at": opaque(),
                # 取得時点の対象head（診断用。回収条件には使わない）
                "head_sha": opaque(required=False),
            },
        )
    },
)
