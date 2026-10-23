"""EIP-712 envelopes for auditable protocol messages."""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from eth_account import Account
from eth_account.messages import encode_typed_data

from .canonical import canonical_json_bytes, sha256_hex
from .models import Payload, SignedMessage

MESSAGE_TYPES = {
    "ProtocolMessage": [
        {"name": "protocolVersion", "type": "uint16"},
        {"name": "messageId", "type": "bytes32"},
        {"name": "createdAtMs", "type": "uint64"},
        {"name": "expiresAtMs", "type": "uint64"},
        {"name": "principalId", "type": "string"},
        {"name": "signer", "type": "address"},
        {"name": "keyEpoch", "type": "uint64"},
        {"name": "messageType", "type": "string"},
        {"name": "payloadHash", "type": "bytes32"},
        {"name": "nonce", "type": "bytes32"},
    ]
}


def configured_chain_id(explicit: int | None = None) -> int:
    if explicit is not None:
        chain_id = explicit
    else:
        raw = os.environ.get("AMN_CHAIN_ID")
        if raw is None:
            if os.environ.get("AMN_ENV", "development").strip().lower() == "production":
                raise RuntimeError("production mode requires an explicit AMN_CHAIN_ID")
            raw = "31337"
        try:
            chain_id = int(raw)
        except ValueError as exc:
            raise ValueError("AMN_CHAIN_ID must be a positive integer") from exc
    if chain_id <= 0:
        raise ValueError("chain ID must be a positive integer")
    return chain_id


def _hex32(value: str) -> bytes:
    return bytes.fromhex(value.removeprefix("0x").removeprefix("sha256:"))


def _millis(value: datetime) -> int:
    delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def typed_message(message: SignedMessage, chain_id: int | None = None) -> dict:
    chain_id = configured_chain_id(chain_id)
    payload = message.payload.model_dump(mode="json", exclude_none=True)
    return {
        "domain": {"name": "AI Mining Network", "version": "2", "chainId": chain_id},
        "types": MESSAGE_TYPES,
        "primaryType": "ProtocolMessage",
        "message": {
            "protocolVersion": int(message.protocolVersion),
            "messageId": hashlib.sha256(message.id.encode("utf-8")).digest(),
            "createdAtMs": _millis(message.createdAt),
            "expiresAtMs": _millis(message.expiresAt),
            "principalId": message.principalId,
            "signer": message.signer,
            "keyEpoch": message.keyEpoch,
            "messageType": payload["kind"],
            "payloadHash": _hex32(message.payloadHash),
            "nonce": _hex32(message.nonce),
        },
    }


def create_message(
    payload: Payload,
    private_key: str,
    chain_id: int | None = None,
    *,
    principal_id: str | None = None,
    key_epoch: int = 0,
    message_id: str | None = None,
    nonce: str | None = None,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    lifetime: timedelta = timedelta(hours=24),
) -> SignedMessage:
    environment = os.environ.get("AMN_ENV", "development").strip().lower()
    if environment == "production":
        raise RuntimeError("production mode rejects local private-key signing; configure an external signer backend")
    if environment not in {"development", "test"}:
        raise ValueError(f"unsupported AMN_ENV value: {environment!r}")
    chain_id = configured_chain_id(chain_id)
    account = Account.from_key(private_key)
    now = created_at or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc).replace(microsecond=(now.microsecond // 1000) * 1000)
    expiry = expires_at or (now + lifetime)
    expiry = expiry.astimezone(timezone.utc).replace(microsecond=(expiry.microsecond // 1000) * 1000)
    stable_principal = principal_id or f"eip155:{chain_id}:{account.address.lower()}"
    payload_dict = payload.model_dump(mode="json", exclude_none=True)
    unsigned = SignedMessage(
        signer=account.address,
        nonce=nonce or "0x" + secrets.token_hex(32),
        payload=payload,
        payloadHash=sha256_hex(canonical_json_bytes(payload_dict)),
        signature="0x" + "00" * 65,
        createdAt=now,
        expiresAt=expiry,
        principalId=stable_principal,
        keyEpoch=key_epoch,
        id=message_id or str(uuid4()),
    )
    signable = encode_typed_data(full_message=typed_message(unsigned, chain_id))
    signature = Account.sign_message(signable, private_key=private_key).signature.hex()
    unsigned.signature = signature if signature.startswith("0x") else "0x" + signature
    return unsigned


def verify_message(message: SignedMessage, chain_id: int | None = None) -> bool:
    if message.protocolVersion != "2":
        return False
    try:
        chain_id = configured_chain_id(chain_id)
    except (ValueError, RuntimeError):
        return False
    payload_dict = message.payload.model_dump(mode="json", exclude_none=True)
    expected_hash = sha256_hex(canonical_json_bytes(payload_dict))
    if expected_hash != message.payloadHash:
        return False
    try:
        signable = encode_typed_data(full_message=typed_message(message, chain_id))
        recovered = Account.recover_message(signable, signature=message.signature)
        return recovered.lower() == message.signer.lower()
    except Exception:  # noqa: BLE001 - untrusted envelope parse/recovery must fail closed.
        return False


def validate_envelope(
    message: SignedMessage,
    chain_id: int | None = None,
    *,
    now: datetime | None = None,
    max_lifetime: timedelta = timedelta(days=7),
    max_clock_skew: timedelta = timedelta(minutes=2),
) -> None:
    """Validate a message for live admission; historical signature checks use verify_message."""

    if message.protocolVersion != "2":
        raise ValueError("unsupported protocol version")
    chain_id = configured_chain_id(chain_id)
    if not verify_message(message, chain_id):
        raise ValueError("message signature or payload hash is invalid")
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
        raise ValueError("validation time must be timezone-aware UTC")
    if message.createdAt > instant + max_clock_skew:
        raise ValueError("message creation time is in the future")
    if message.expiresAt <= instant:
        raise ValueError("message has expired")
    if message.expiresAt - message.createdAt > max_lifetime:
        raise ValueError("message expiry exceeds the protocol maximum lifetime")

