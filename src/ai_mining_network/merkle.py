"""OpenZeppelin StandardMerkleTree-compatible per-unit commitment."""

from __future__ import annotations

from dataclasses import dataclass

from eth_abi import encode
from eth_utils import keccak


LEAF_ENCODING = ["bytes32", "bytes32", "bytes32"]


def _bytes32(value: str) -> bytes:
    raw = value.removeprefix("sha256:").removeprefix("0x")
    if len(raw) != 64:
        raise ValueError(f"expected a 32-byte hash, got {value!r}")
    return bytes.fromhex(raw)


def standard_leaf_hash(value: tuple[str, str, str]) -> bytes:
    return keccak(keccak(encode(LEAF_ENCODING, [_bytes32(part) for part in value])))


def _node_hash(left: bytes, right: bytes) -> bytes:
    first, second = sorted((left, right))
    return keccak(first + second)


def verify_proof(value: tuple[str, str, str], proof: list[str], root: str) -> bool:
    """Verify one inclusion proof without needing the full result tree."""
    try:
        cursor = standard_leaf_hash(value)
        for node in proof:
            sibling = bytes.fromhex(node.removeprefix("0x"))
            if len(sibling) != 32:
                return False
            cursor = _node_hash(cursor, sibling)
        return "0x" + cursor.hex() == root
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class MerkleTree:
    values: tuple[tuple[str, str, str], ...]
    tree: tuple[bytes, ...]
    leaf_indices: tuple[int, ...]

    @property
    def root(self) -> str:
        return "0x" + self.tree[0].hex()

    def proof(self, value_index: int) -> list[str]:
        if value_index < 0 or value_index >= len(self.values):
            raise IndexError(value_index)
        cursor = self.leaf_indices[value_index]
        proof: list[str] = []
        while cursor > 0:
            sibling = cursor + (1 if cursor % 2 else -1)
            proof.append("0x" + self.tree[sibling].hex())
            cursor = (cursor - 1) // 2
        return proof

    def verify(self, value_index: int, proof: list[str] | None = None) -> bool:
        if value_index < 0 or value_index >= len(self.values):
            return False
        cursor = standard_leaf_hash(self.values[value_index])
        for node in proof if proof is not None else self.proof(value_index):
            cursor = _node_hash(cursor, bytes.fromhex(node.removeprefix("0x")))
        return "0x" + cursor.hex() == self.root


def build_tree(values: list[tuple[str, str, str]]) -> MerkleTree:
    if not values:
        raise ValueError("a commitment must contain at least one execution unit")
    if len(set(values)) != len(values):
        raise ValueError("duplicate commitment leaves are not allowed")
    hashed = sorted((standard_leaf_hash(value), index) for index, value in enumerate(values))
    count = len(values)
    nodes = [b""] * (2 * count - 1)
    for sorted_index, (leaf_hash, _) in enumerate(hashed):
        nodes[len(nodes) - sorted_index - 1] = leaf_hash
    for index in range(len(nodes) - count - 1, -1, -1):
        nodes[index] = _node_hash(nodes[2 * index + 1], nodes[2 * index + 2])
    leaf_indices = [0] * count
    for sorted_index, (_, original_index) in enumerate(hashed):
        leaf_indices[original_index] = len(nodes) - sorted_index - 1
    return MerkleTree(tuple(values), tuple(nodes), tuple(leaf_indices))

