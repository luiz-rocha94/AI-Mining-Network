"""Durable identity bindings and signed key rotation, revocation, and recovery."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from eth_utils import to_checksum_address

from .canonical import content_hash
from .models import IdentityTransition, SignedMessage
from .signing import create_message, validate_envelope, verify_message


class IdentityDirectory:
    """Local durable trust directory; production genesis roots need out-of-band governance."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS identities (
                    identity_id TEXT PRIMARY KEY,
                    current_signer TEXT NOT NULL,
                    recovery_signer TEXT NOT NULL,
                    key_epoch INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'REVOKED'))
                );
                CREATE TABLE IF NOT EXISTS identity_transitions (
                    identity_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    message_hash TEXT NOT NULL,
                    action TEXT NOT NULL,
                    PRIMARY KEY(identity_id, sequence),
                    UNIQUE(message_hash),
                    FOREIGN KEY(identity_id) REFERENCES identities(identity_id)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @staticmethod
    def _address(value: str) -> str:
        try:
            return to_checksum_address(value).lower()
        except (TypeError, ValueError) as exc:
            raise ValueError("identity keys must be valid 20-byte EVM addresses") from exc

    def register(self, identity_id: str, signer: str, recovery_signer: str) -> None:
        if not identity_id or len(identity_id) > 128:
            raise ValueError("identityId must contain 1 to 128 characters")
        current = self._address(signer)
        recovery = self._address(recovery_signer)
        if current == recovery:
            raise ValueError("recovery key must be independent from the active signing key")
        with closing(self._connect()) as db:
            try:
                db.execute(
                    "INSERT INTO identities VALUES (?, ?, ?, 0, 'ACTIVE')",
                    (identity_id, current, recovery),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("identity is already registered") from exc

    def get(self, identity_id: str) -> dict:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM identities WHERE identity_id=?", (identity_id,)).fetchone()
        if row is None:
            raise ValueError("unknown identity")
        return dict(row)

    def transition_message(
        self,
        identity_id: str,
        action: str,
        signing_key: str,
        new_signer: str | None = None,
        *,
        chain_id: int | None = None,
    ) -> SignedMessage:
        record = self.get(identity_id)
        payload = IdentityTransition(
            identityId=identity_id,
            sequence=record["key_epoch"] + 1,
            action=action,  # type: ignore[arg-type]
            previousSigner=record["current_signer"],
            newSigner=new_signer,
        )
        return create_message(
            payload,
            signing_key,
            chain_id,
            principal_id=identity_id,
            key_epoch=record["key_epoch"],
        )

    def authenticate(
        self,
        message: SignedMessage,
        *,
        chain_id: int | None = None,
        now: datetime | None = None,
    ) -> None:
        validate_envelope(message, chain_id, now=now)
        record = self.get(message.principalId)
        if record["status"] != "ACTIVE":
            raise ValueError("signing identity is revoked")
        if message.signer.lower() != record["current_signer"] or message.keyEpoch != record["key_epoch"]:
            raise ValueError("signer or key epoch does not match the active identity binding")

    def apply_transition(
        self,
        message: SignedMessage,
        *,
        chain_id: int | None = None,
        now: datetime | None = None,
    ) -> dict:
        validate_envelope(message, chain_id, now=now)
        if not isinstance(message.payload, IdentityTransition):
            raise TypeError("message payload is not an identity transition")
        payload = message.payload
        if payload.identityId != message.principalId:
            raise ValueError("identity transition principal binding does not match its payload")
        message_hash = content_hash(message.model_dump(mode="json", exclude_none=True))
        with closing(self._connect()) as db:
            if db.execute(
                "SELECT 1 FROM identity_transitions WHERE message_hash=?",
                (message_hash,),
            ).fetchone():
                raise ValueError("identity transition was already applied")
        record = self.get(payload.identityId)
        if message.keyEpoch != record["key_epoch"] or payload.sequence != record["key_epoch"] + 1:
            raise ValueError("identity transition sequence or key epoch is stale")
        if payload.previousSigner.lower() != record["current_signer"]:
            raise ValueError("identity transition does not name the current signer")

        action = payload.action
        if action in {"ROTATE", "REVOKE"}:
            authorized_signer = record["current_signer"]
        else:
            authorized_signer = record["recovery_signer"]
            if record["status"] != "REVOKED":
                raise ValueError("recovery is permitted only after identity revocation")
        if message.signer.lower() != authorized_signer or not verify_message(message, chain_id):
            raise ValueError("identity transition signer is not authorized")

        if action == "ROTATE":
            if record["status"] != "ACTIVE" or payload.newSigner is None:
                raise ValueError("rotation requires an active identity and a replacement signer")
            next_signer = self._address(payload.newSigner)
            if next_signer in {record["current_signer"], record["recovery_signer"]}:
                raise ValueError("replacement key must be distinct from current and recovery keys")
            next_status = "ACTIVE"
        elif action == "REVOKE":
            if record["status"] != "ACTIVE" or payload.newSigner is not None:
                raise ValueError("revocation requires an active identity and no replacement signer")
            next_signer = record["current_signer"]
            next_status = "REVOKED"
        elif action == "RECOVER":
            if payload.newSigner is None:
                raise ValueError("recovery requires a replacement signer")
            next_signer = self._address(payload.newSigner)
            if next_signer in {record["current_signer"], record["recovery_signer"]}:
                raise ValueError("recovered key must be distinct from current and recovery keys")
            next_status = "ACTIVE"
        else:  # Pydantic constrains this; keep defensive for foreign implementations.
            raise ValueError("unsupported identity transition")

        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO identity_transitions VALUES (?, ?, ?, ?)",
                (payload.identityId, payload.sequence, message_hash, action),
            )
            db.execute(
                "UPDATE identities SET current_signer=?, key_epoch=?, status=? WHERE identity_id=?",
                (next_signer, payload.sequence, next_status, payload.identityId),
            )
            db.commit()
        except sqlite3.IntegrityError as exc:
            db.rollback()
            raise ValueError("identity transition was already applied or conflicts with history") from exc
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return self.get(payload.identityId)
