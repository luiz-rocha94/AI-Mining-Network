"""Task state machine and local append-only audit event log."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .canonical import content_hash

TRANSITIONS = {
    "CREATED": {"QUOTED", "CANCELLED", "EXPIRED"},
    "QUOTED": {"FUNDED", "EXPIRED", "CANCELLED"},
    "FUNDED": {"ASSIGNED", "REFUNDED", "CANCELLED"},
    "ASSIGNED": {"EXECUTED", "FAILED", "EXPIRED", "REFUNDED"},
    "EXECUTED": {"VERIFYING", "FAILED", "DISPUTED"},
    "VERIFYING": {"VERIFIED", "FAILED", "INCONCLUSIVE", "DISPUTED"},
    "DISPUTED": {"VERIFIED", "FAILED", "INCONCLUSIVE", "REFUNDED"},
    "VERIFIED": {"SETTLED"},
    "FAILED": {"REFUNDED"},
    "INCONCLUSIVE": {"REFUNDED"},
    "EXPIRED": {"REFUNDED"},
    "CANCELLED": {"REFUNDED"},
    "SETTLED": set(),
    "REFUNDED": set(),
}


class InvalidTransition(ValueError):
    pass


class EventLog:
    """Local audit storage. This SQLite file is private to this node."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, "
                "event_type TEXT NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL, "
                "previous_hash TEXT NOT NULL DEFAULT '', event_hash TEXT NOT NULL DEFAULT '')"
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(events)").fetchall()}
            if "previous_hash" not in columns:
                db.execute("ALTER TABLE events ADD COLUMN previous_hash TEXT NOT NULL DEFAULT ''")
            if "event_hash" not in columns:
                db.execute("ALTER TABLE events ADD COLUMN event_hash TEXT NOT NULL DEFAULT ''")
            db.execute(
                "CREATE TABLE IF NOT EXISTS settlements ("
                "idempotency_key TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def append(self, task_id: str, event_type: str, payload: dict) -> int:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as db:
            previous = db.execute(
                "SELECT event_hash FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 1", (task_id,)
            ).fetchone()
            previous_hash = previous["event_hash"] if previous and previous["event_hash"] else "0x" + "00" * 32
            encoded_payload = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            cursor = db.execute(
                "INSERT INTO events(task_id,event_type,created_at,payload_json,previous_hash,event_hash) VALUES(?,?,?,?,?,?)",
                (task_id, event_type, created_at, encoded_payload, previous_hash, ""),
            )
            sequence = int(cursor.lastrowid)
            event_hash = self._event_hash(sequence, task_id, event_type, created_at, previous_hash, payload)
            db.execute("UPDATE events SET event_hash=? WHERE seq=?", (event_hash, sequence))
            return sequence

    def events(self, task_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM events WHERE task_id=? ORDER BY seq", (task_id,)).fetchall()
        return [
            {
                "sequence": row["seq"],
                "taskId": row["task_id"],
                "type": row["event_type"],
                "createdAt": row["created_at"],
                "payload": json.loads(row["payload_json"]),
                "previousHash": row["previous_hash"],
                "eventHash": row["event_hash"],
            }
            for row in rows
        ]

    @staticmethod
    def _event_hash(sequence: int, task_id: str, event_type: str, created_at: str, previous_hash: str, payload: dict) -> str:
        return content_hash({
            "sequence": sequence,
            "taskId": task_id,
            "type": event_type,
            "createdAt": created_at,
            "previousHash": previous_hash,
            "payload": payload,
        })

    def verify_event_chain(self, task_id: str) -> bool:
        previous_hash = "0x" + "00" * 32
        for event in self.events(task_id):
            if event["previousHash"] != previous_hash:
                return False
            expected = self._event_hash(
                event["sequence"], task_id, event["type"], event["createdAt"], previous_hash, event["payload"]
            )
            if event["eventHash"] != expected:
                return False
            previous_hash = expected
        return True

    def store_settlement(self, key: str, task_id: str, payload_hash: str, payload: dict) -> tuple[dict, bool]:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as db:
            previous = db.execute("SELECT * FROM settlements WHERE idempotency_key=?", (key,)).fetchone()
            if previous:
                if previous["payload_hash"] != payload_hash:
                    raise ValueError("settlement idempotency key was reused with different terms")
                return json.loads(previous["payload_json"]), False
            db.execute(
                "INSERT INTO settlements(idempotency_key,task_id,payload_hash,payload_json) VALUES(?,?,?,?)",
                (key, task_id, payload_hash, encoded),
            )
        return payload, True


def validate_transition(previous: str, current: str) -> None:
    if current not in TRANSITIONS.get(previous, set()):
        raise InvalidTransition(f"task state cannot transition from {previous} to {current}")

