"""Canonical JSON for the protocol's RFC 8785-compatible integer-only subset."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _validate(value: Any, path: str = "$ROOT") -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > 9_007_199_254_740_991:
            raise ValueError(f"{path}: integer exceeds interoperable JSON safe range")
        return
    if isinstance(value, float):
        raise TypeError(f"{path}: floating-point values are prohibited in signed protocol payloads")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path}: object keys must be strings")
            if not key.isascii():
                raise ValueError(f"{path}: protocol object keys must be ASCII")
            _validate(item, f"{path}.{key}")
        return
    raise TypeError(f"{path}: unsupported canonical JSON value {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize the protocol JSON subset identically in Python and TypeScript.

    Protocol schemas restrict object keys to ASCII and numeric values to safe
    integers. Under those constraints sorted compact UTF-8 JSON matches the
    RFC 8785 canonical representation used for content hashes.
    """

    _validate(value)
    text = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return text.encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def content_hash(value: Any) -> str:
    return sha256_hex(canonical_json_bytes(value))

