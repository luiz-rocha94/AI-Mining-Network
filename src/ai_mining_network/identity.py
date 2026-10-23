"""Development identities. Production nodes should use an external signer."""

from __future__ import annotations

import os
import re
from pathlib import Path

from eth_account import Account


def load_or_create_private_key(root: Path, role: str) -> str:
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
