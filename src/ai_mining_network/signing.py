"""EIP-712 envelopes for auditable protocol messages."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
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
        {"name": "messageType", "type": "string"},
        {"name": "payloadHash", "type": "bytes32"},
        {"name": "nonce", "type": "bytes32"},
    ]
}


def _hex32(value: str) -> bytes:
    return bytes.fromhex(value.removeprefix("0x").removeprefix("sha256:"))


def typed_message(message: SignedMessage, chain_id: int = 31337) -> dict:
    payload = message.payload.model_dump(mode="json", exclude_none=True)
    return {
        "domain": {"name": "AI Mining Network", "version": "1", "chainId": chain_id},
        "types": MESSAGE_TYPES,
        "primaryType": "ProtocolMessage",
        "message": {
            "protocolVersion": 1,
            "messageId": hashlib.sha256(message.id.encode("utf-8")).digest(),
            "createdAtMs": int(message.createdAt.timestamp() * 1000),
            "messageType": payload["kind"],
            "payloadHash": _hex32(message.payloadHash),
            "nonce": _hex32(message.nonce),
        },
    }


def create_message(payload: Payload, private_key: str, chain_id: int = 31337) -> SignedMessage:
    account = Account.from_key(private_key)
    now = datetime.now(timezone.utc)
    payload_dict = payload.model_dump(mode="json", exclude_none=True)
    unsigned = SignedMessage(
        signer=account.address,
        nonce="0x" + secrets.token_hex(32),
        payload=payload,
        payloadHash=sha256_hex(canonical_json_bytes(payload_dict)),
        signature="0x" + "00" * 65,
        createdAt=now,
        id=str(uuid4()),
    )
    signable = encode_typed_data(full_message=typed_message(unsigned, chain_id))
    signature = Account.sign_message(signable, private_key=private_key).signature.hex()
    unsigned.signature = signature if signature.startswith("0x") else "0x" + signature
    return unsigned


def verify_message(message: SignedMessage, chain_id: int = 31337) -> bool:
    payload_dict = message.payload.model_dump(mode="json", exclude_none=True)
    expected_hash = sha256_hex(canonical_json_bytes(payload_dict))
    if expected_hash != message.payloadHash:
        return False
    try:
        signable = encode_typed_data(full_message=typed_message(message, chain_id))
        recovered = Account.recover_message(signable, signature=message.signature)
        return recovered.lower() == message.signer.lower()
    except Exception:
        return False

