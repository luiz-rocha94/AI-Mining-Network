"""Durable, transactional replay rejection for live protocol message admission."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .models import SignedMessage
from .signing import validate_envelope


class ReplayError(ValueError):
    pass


class SQLiteReplayGuard:
    """Persist message identifiers and nonces across process restarts.

    This protects one SQLite trust domain. A multi-peer deployment must share or
    gossip accepted-message state and define its partition/conflict policy.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS protocol_replays (
                    chain_id INTEGER NOT NULL,
                    signer TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    expires_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(chain_id, signer, nonce),
                    UNIQUE(chain_id, message_id)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def consume(
        self,
        message: SignedMessage,
        *,
        chain_id: int | None = None,
        now: datetime | None = None,
        identity_directory=None,
    ) -> None:
        instant = now or datetime.now(timezone.utc)
        if chain_id is None:
            from .signing import configured_chain_id

            chain_id = configured_chain_id()
        if identity_directory is None:
            validate_envelope(message, chain_id, now=instant)
        else:
            identity_directory.authenticate(message, chain_id=chain_id, now=instant)
        now_ms = int(instant.timestamp() * 1000)
        expires_ms = int(message.expiresAt.timestamp() * 1000)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM protocol_replays WHERE expires_at_ms <= ?", (now_ms,))
            db.execute(
                "INSERT INTO protocol_replays VALUES (?, ?, ?, ?, ?)",
                (chain_id, message.signer.lower(), message.nonce.lower(), message.id, expires_ms),
            )
            db.commit()
        except sqlite3.IntegrityError as exc:
            db.rollback()
            raise ReplayError("message identifier or signer nonce has already been accepted") from exc
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
