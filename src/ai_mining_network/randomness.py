"""Randomness source adapters. The built-in source is explicitly simulated."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Protocol


class RandomnessSource(Protocol):
    verified: bool

    def read(self, round_number: int, task_id: str, commitment_root: str) -> str: ...


@dataclass
class SimulatedBeacon:
    """Offline development beacon; not a public verifiable randomness source."""

    seed: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    verified: bool = False

    def read(self, round_number: int, task_id: str, commitment_root: str) -> str:
        material = b"AMN-DEV-BEACON-V1\0" + self.seed + str(round_number).encode()
        material += task_id.encode() + commitment_root.encode()
        return "0x" + hashlib.sha256(material).hexdigest()


def challenge_seed(task_id: str, commitment_root: str, round_number: int, randomness: str) -> bytes:
    material = b"AI-MINING-NETWORK-POUW-V1\0"
    material += task_id.encode("utf-8") + b"\0"
    material += commitment_root.encode("ascii") + b"\0"
    material += str(round_number).encode("ascii") + b"\0"
    material += randomness.encode("ascii")
    return hashlib.sha256(material).digest()


def select_validators(seed: bytes, eligible: list[str], executor: str, count: int, conflicts: set[str] | None = None) -> list[str]:
    excluded = {executor.lower(), *(item.lower() for item in (conflicts or set()))}
    pool = sorted(set(eligible), key=lambda peer: hashlib.sha256(seed + b"\0validator\0" + peer.encode()).digest())
    selected = [peer for peer in pool if peer.lower() not in excluded][:count]
    if len(selected) < count:
        raise ValueError(f"only {len(selected)} eligible independent validators; {count} required")
    return selected


def select_units(seed: bytes, unit_ids: list[str], sample_count: int, validators: list[str]) -> dict[str, list[str]]:
    if sample_count < 1 or sample_count > len(unit_ids):
        raise ValueError("sample count must be between one and the number of units")
    if len(set(unit_ids)) != len(unit_ids):
        raise ValueError("unit identifiers must be unique")
    if not validators:
        raise ValueError("at least one validator is required")
    ordered = sorted(unit_ids, key=lambda unit: hashlib.sha256(seed + b"\0unit\0" + unit.encode()).digest())
    chosen = ordered[:sample_count]
    result = {validator: [] for validator in validators}
    for index, unit in enumerate(chosen):
        result[validators[index % len(validators)]].append(unit)
    return result

