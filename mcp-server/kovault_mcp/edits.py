"""Edit-history helper.

Every write logs an `edits` row: what table/row, which operation, the changed fields, who
(`edited_by` = local-config username) and the actor kind (self/ai/script). `pages.contributors`
is appended separately by the update path. Runs inside the caller's transaction (takes a cursor).

`changes` is normally just the written fields. A change the SERVER made on the caller's behalf
also records WHY, as a flat key alongside them, so the log can be queried for it later:

    {"lifecycle": "archived", "cascaded_from_group": "<uuid>"}   E4, group archive cascade

Flat rather than nested because the query that matters is an equality test on one key
(`changes->>'cascaded_from_group' = …`); keep new reason-keys flat for the same reason. The row's
`actor` is 'script' whenever the server, not the caller, decided — so a cascaded archive is
distinguishable from a deliberate one without reading `changes` at all.
"""
from __future__ import annotations

import json
from typing import Any


def log_edit(
    cur,
    *,
    table_name: str,
    row_id: str,
    operation: str,            # 'insert' | 'update' | 'trash'
    edited_by: str,
    actor: str = "self",       # 'self' | 'ai' | 'script'
    changes: dict[str, Any] | None = None,
) -> None:
    cur.execute(
        """
        INSERT INTO edits (table_name, row_id, operation, changes, edited_by, actor)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (table_name, row_id, operation, json.dumps(changes) if changes is not None else None,
         edited_by, actor),
    )
