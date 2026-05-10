from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import to_plain_dict


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def append(
        self,
        run_id: str,
        node_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        with sqlite3.connect(self.path) as conn:
            cursor = conn.execute(
                """
                insert into events (ts, run_id, node_id, event_type, payload_json)
                values (?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    run_id,
                    node_id,
                    event_type,
                    json.dumps(to_plain_dict(payload), sort_keys=True),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select id, ts, run_id, node_id, event_type, payload_json
                from events
                where run_id = ?
                order by id
                """,
                (run_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "ts": row["ts"],
                "run_id": row["run_id"],
                "node_id": row["node_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def _init_db(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                create table if not exists events (
                    id integer primary key autoincrement,
                    ts text not null,
                    run_id text not null,
                    node_id text not null,
                    event_type text not null,
                    payload_json text not null
                )
                """
            )
            conn.execute(
                "create index if not exists idx_events_run_id on events(run_id, id)"
            )
            conn.commit()
