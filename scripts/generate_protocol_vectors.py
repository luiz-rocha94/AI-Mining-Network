"""Rebuild deterministic cross-language protocol vectors; test key is public and unsafe."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from eth_account.messages import encode_typed_data
from eth_utils import keccak

from ai_mining_network.canonical import canonical_json_bytes, content_hash
from ai_mining_network.merkle import build_tree
from ai_mining_network.models import SkillRating
from ai_mining_network.signing import create_message, typed_message

ROOT = Path(__file__).resolve().parents[1]
TEST_KEY = "0x" + "0" * 63 + "1"
CHAIN_ID = 31337


def build_vectors() -> dict:
    canonical_value = {
        "z": "mañana",
        "a": [3, {"b": "x\ny", "a": "✓"}],
        "nullable": None,
    }
    payload = SkillRating(
        ratingId="rating-vector-1",
        taskId="task-vector-1",
        skillHash="sha256:" + "a" * 64,
        stars=5,
        comment="excellent",
    )
    created_at = datetime(2026, 1, 2, 3, 4, 5, 6000, timezone.utc)
    nonce = "0x" + "01" * 32
    message = create_message(
        payload,
        TEST_KEY,
        CHAIN_ID,
        principal_id="did:amn:vector:alice",
        key_epoch=2,
        message_id="00000000-0000-4000-8000-000000000001",
        nonce=nonce,
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=10),
    )
    typed = typed_message(message, CHAIN_ID)
    signable = encode_typed_data(full_message=typed)
    merkle_values = [
        [f"0x{index:064x}", f"0x{index + 1:064x}", f"0x{index + 2:064x}"]
        for index in (0, 3, 6)
    ]
    tree = build_tree([tuple(value) for value in merkle_values])
    return {
        "protocolVersion": "2",
        "warning": "All key material in this fixture is public test data. Never use it for funds or production identity.",
        "canonical": {
            "value": canonical_value,
            "utf8": canonical_json_bytes(canonical_value).decode("utf-8"),
            "sha256": content_hash(canonical_value),
        },
        "signedEnvelope": {
            "chainId": CHAIN_ID,
            "testOnlyPrivateKey": TEST_KEY,
            "typedDataHash": "0x" + keccak(b"\x19" + signable.version + signable.header + signable.body).hex(),
            "payloadHash": message.payloadHash,
            "message": message.model_dump(mode="json", exclude_none=True),
        },
        "merkle": {
            "standard": "OpenZeppelin StandardMerkleTree",
            "types": ["bytes32", "bytes32", "bytes32"],
            "values": merkle_values,
            "root": tree.root,
            "proofForIndex1": tree.proof(1),
        },
        "invalidCases": [
            "altered-payload",
            "wrong-eip712-chain-domain",
            "expired-envelope",
            "revoked-signer",
            "unknown-protocol-version",
            "replayed-message-id",
            "replayed-signer-nonce",
        ],
    }


def main() -> None:
    target = ROOT / "packages" / "protocol" / "v2" / "golden-vectors.json"
    target.write_text(json.dumps(build_vectors(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
