"""Development identities. Production nodes should use an external signer."""

from __future__ import annotations

import os
import re
from pathlib import Path

from eth_account import Account


def generate_keypair() -> tuple[str, str]:
    """Create a local EVM keypair; the returned private key must be secured by the caller."""

    account = Account.create()
    return account.key.hex(), account.address


def load_or_create_private_key(root: Path, role: str) -> str:
    validate_signing_configuration(os.environ.get("AMN_ENV", "development"), "local-keyfile")
    if role not in {"miner", "skill-author", "customer", "marketplace", "benchmark-contributor"} and not re.fullmatch(r"validator-[1-9][0-9]*", role):
        raise ValueError("unsupported local identity role")
    path = root / ".amn" / "identities" / f"{role}.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        private_key = path.read_text(encoding="ascii").strip()
        Account.from_key(private_key)
        return private_key
    account = Account.create()
    path.write_text(account.key.hex(), encoding="ascii")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return account.key.hex()


def validate_signing_configuration(environment: str, signer_backend: str) -> None:
    """Fail closed rather than loading development file keys in production."""

    normalized = environment.strip().lower()
    if normalized == "production" and signer_backend in {"local-keyfile", "development", "test"}:
        raise RuntimeError("production mode rejects development key files; configure an external signer backend")
    if normalized not in {"development", "test", "production"}:
        raise ValueError(f"unsupported AMN_ENV value: {environment!r}")
