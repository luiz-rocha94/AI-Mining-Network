import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from eth_account import Account

from ai_mining_network.identity import generate_keypair, load_or_create_private_key
from ai_mining_network.identity_store import IdentityDirectory
from ai_mining_network.models import IdentityTransition, SkillRating
from ai_mining_network.protocol_version import negotiate_protocol_version
from ai_mining_network.replay_guard import ReplayError, SQLiteReplayGuard
from ai_mining_network.signing import (
    configured_chain_id,
    create_message,
    validate_envelope,
    verify_message,
)

ROOT = Path(__file__).resolve().parents[1]
VECTOR_FILE = ROOT / "packages" / "protocol" / "v2" / "golden-vectors.json"


@pytest.fixture
def isolated_dir():
    path = Path(tempfile.mkdtemp(prefix="amn-protocol-", dir=ROOT))
    try:
        yield path
    finally:
        shutil.rmtree(path)


def sample_rating():
    return SkillRating(
        ratingId="rating-vector-1",
        taskId="task-vector-1",
        skillHash="sha256:" + "a" * 64,
        stars=5,
        comment="excellent",
    )


def test_python_matches_shared_protocol_golden_vectors():
    vectors = json.loads(VECTOR_FILE.read_text(encoding="utf-8"))
    assert vectors["protocolVersion"] == "2"
    from eth_account.messages import encode_typed_data

    from ai_mining_network.canonical import canonical_json_bytes, content_hash
    from ai_mining_network.merkle import build_tree
    from ai_mining_network.signing import typed_message

    canonical = vectors["canonical"]
    assert canonical_json_bytes(canonical["value"]).decode("utf-8") == canonical["utf8"]
    assert content_hash(canonical["value"]) == canonical["sha256"]

    envelope = vectors["signedEnvelope"]["message"]
    from ai_mining_network.models import SignedMessage

    signed = SignedMessage.model_validate(envelope)
    assert verify_message(signed, vectors["signedEnvelope"]["chainId"])
    assert Account.recover_message(
        encode_typed_data(full_message=typed_message(signed, vectors["signedEnvelope"]["chainId"])),
        signature=signed.signature,
    ).lower() == signed.signer.lower()
    assert signed.payloadHash == vectors["signedEnvelope"]["payloadHash"]
    from ai_mining_network.canonical import sha256_hex

    recreated = create_message(
        SkillRating.model_validate(envelope["payload"]),
        vectors["signedEnvelope"]["testOnlyPrivateKey"],
        vectors["signedEnvelope"]["chainId"],
        principal_id=envelope["principalId"],
        key_epoch=envelope["keyEpoch"],
        message_id=envelope["id"],
        nonce=envelope["nonce"],
        created_at=datetime.fromisoformat(envelope["createdAt"].replace("Z", "+00:00")),
        expires_at=datetime.fromisoformat(envelope["expiresAt"].replace("Z", "+00:00")),
    )
    assert recreated.signature == signed.signature
    assert sha256_hex(canonical_json_bytes(envelope["payload"])) == signed.payloadHash

    merkle = vectors["merkle"]
    values = [tuple(value) for value in merkle["values"]]
    tree = build_tree(values)
    assert tree.root == merkle["root"]
    assert tree.proof(1) == merkle["proofForIndex1"]
    assert tree.verify(1, merkle["proofForIndex1"])


def test_live_envelope_rejects_expiry_wrong_chain_and_future_clock():
    account = Account.create()
    message = create_message(sample_rating(), account.key.hex())
    assert verify_message(message)
    with pytest.raises(ValueError, match="signature or payload hash"):
        validate_envelope(message, chain_id=1)

    old = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=2)
    expired = create_message(
        sample_rating(),
        account.key.hex(),
        created_at=old,
        expires_at=old + timedelta(hours=1),
    )
    assert verify_message(expired)  # Historical signature is still auditable.
    with pytest.raises(ValueError, match="expired"):
        validate_envelope(expired)

    future = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    from ai_mining_network.models import SignedMessage

    future_message = SignedMessage.model_validate(
        create_message(
            sample_rating(),
            account.key.hex(),
            created_at=future,
            expires_at=future + timedelta(hours=1),
        ).model_dump(mode="json")
    )
    with pytest.raises(ValueError, match="future"):
        validate_envelope(future_message)


def test_wire_timestamps_use_canonical_utc_z_and_millisecond_precision():
    from pydantic import ValidationError

    message = create_message(sample_rating(), Account.create().key.hex())
    raw = message.model_dump(mode="json")
    assert raw["createdAt"].endswith("Z")
    assert raw["expiresAt"].endswith("Z")
    raw["createdAt"] = raw["createdAt"].replace("Z", "+00:00")
    with pytest.raises(ValidationError, match="ending in Z"):
        type(message).model_validate(raw)


def test_replay_guard_rejects_duplicate_nonce_and_message_after_restart(isolated_dir):
    account = Account.create()
    message = create_message(sample_rating(), account.key.hex())
    first_process = SQLiteReplayGuard(isolated_dir / "replay.sqlite3")
    first_process.consume(message)

    restarted_process = SQLiteReplayGuard(isolated_dir / "replay.sqlite3")
    with pytest.raises(ReplayError):
        restarted_process.consume(message)

    same_nonce_new_id = create_message(
        sample_rating(), account.key.hex(), nonce=message.nonce
    )
    with pytest.raises(ReplayError):
        restarted_process.consume(same_nonce_new_id)


def test_identity_key_rotation_revocation_recovery_and_restart(isolated_dir):
    initial = Account.from_key(generate_keypair()[0])
    recovery = Account.from_key(generate_keypair()[0])
    replacement = Account.from_key(generate_keypair()[0])
    recovered = Account.from_key(generate_keypair()[0])
    db_path = isolated_dir / "identity.sqlite3"
    directory = IdentityDirectory(db_path)
    directory.register("did:amn:alice", initial.address, recovery.address)

    original_message = create_message(
        sample_rating(), initial.key.hex(), principal_id="did:amn:alice", key_epoch=0
    )
    directory.authenticate(original_message)
    rotation = directory.transition_message(
        "did:amn:alice", "ROTATE", initial.key.hex(), replacement.address
    )
    assert directory.apply_transition(rotation)["key_epoch"] == 1

    restarted = IdentityDirectory(db_path)
    active_message = create_message(
        sample_rating(), replacement.key.hex(), principal_id="did:amn:alice", key_epoch=1
    )
    restarted.authenticate(active_message)
    with pytest.raises(ValueError, match="active identity binding"):
        restarted.authenticate(original_message)

    revoke = restarted.transition_message("did:amn:alice", "REVOKE", replacement.key.hex())
    assert restarted.apply_transition(revoke)["status"] == "REVOKED"
    with pytest.raises(ValueError, match="revoked"):
        restarted.authenticate(active_message)

    recover = restarted.transition_message(
        "did:amn:alice", "RECOVER", recovery.key.hex(), recovered.address
    )
    final_identity = restarted.apply_transition(recover)
    assert final_identity["status"] == "ACTIVE"
    assert final_identity["current_signer"] == recovered.address.lower()
    assert final_identity["key_epoch"] == 3
    recovered_message = create_message(
        sample_rating(), recovered.key.hex(), principal_id="did:amn:alice", key_epoch=3
    )
    restarted.authenticate(recovered_message)
    with pytest.raises(ValueError, match="already applied"):
        restarted.apply_transition(recover)


def test_transition_rejects_unauthorized_or_stale_signer(isolated_dir):
    current = Account.create()
    recovery = Account.create()
    attacker = Account.create()
    new_key = Account.create()
    directory = IdentityDirectory(isolated_dir / "identities.sqlite3")
    directory.register("did:amn:alice", current.address, recovery.address)
    forged = create_message(
        IdentityTransition(
            identityId="did:amn:alice",
            sequence=1,
            action="ROTATE",
            previousSigner=current.address,
            newSigner=new_key.address,
        ),
        attacker.key.hex(),
        principal_id="did:amn:alice",
        key_epoch=0,
    )
    with pytest.raises(ValueError, match="not authorized"):
        directory.apply_transition(forged)


def test_development_file_keys_are_refused_in_production(isolated_dir, monkeypatch):
    monkeypatch.setenv("AMN_ENV", "test")
    key = load_or_create_private_key(isolated_dir, "miner")
    assert Account.from_key(key).address
    monkeypatch.setenv("AMN_ENV", "production")
    with pytest.raises(RuntimeError, match="rejects development key files"):
        load_or_create_private_key(isolated_dir, "miner")
    account = Account.create()
    with pytest.raises(RuntimeError, match="rejects local private-key signing"):
        create_message(sample_rating(), account.key.hex())
    monkeypatch.delenv("AMN_CHAIN_ID", raising=False)
    with pytest.raises(RuntimeError, match="requires an explicit AMN_CHAIN_ID"):
        configured_chain_id()


def test_configured_chain_id_is_domain_bound(monkeypatch):
    monkeypatch.setenv("AMN_ENV", "test")
    monkeypatch.setenv("AMN_CHAIN_ID", "11155111")
    message = create_message(sample_rating(), Account.create().key.hex())
    assert verify_message(message, chain_id=11155111)
    assert not verify_message(message, chain_id=31337)


def test_protocol_version_negotiation_is_explicit_and_unknown_versions_fail():
    assert negotiate_protocol_version(["2"], ["3", "2"]) == "2"
    with pytest.raises(ValueError, match="no mutually supported"):
        negotiate_protocol_version(["2"], ["3"])
    account = Account.create()
    message = create_message(sample_rating(), account.key.hex())
    for version in ("1", "3"):
        unsupported = message.model_copy(update={"protocolVersion": version})
        assert not verify_message(unsupported)
        with pytest.raises(ValueError, match="unsupported protocol version"):
            validate_envelope(unsupported)


def test_adapter_contract_schema_is_versioned_and_covers_required_interfaces():
    schema = json.loads((ROOT / "packages" / "protocol" / "v2" / "adapters.schema.json").read_text())
    interfaces = schema["properties"]["interface"]["enum"]
    assert schema["properties"]["protocolVersion"]["const"] == "2"
    assert set(interfaces) == {
        "ComputeBackend",
        "PeerTransport",
        "PublicMetadataStore",
        "ArtifactStore",
        "RandomnessSource",
        "AttestationPublisher",
        "SettlementBackend",
    }
