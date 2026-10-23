"""Explicit protocol-major negotiation; unknown wire versions are never parsed as v2."""

from __future__ import annotations

from collections.abc import Iterable

SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2"})


def negotiate_protocol_version(local: Iterable[str], remote: Iterable[str]) -> str:
    local_set = set(local)
    remote_set = set(remote)
    if any(not isinstance(value, str) or not value.isdecimal() for value in local_set | remote_set):
        raise ValueError("protocol versions must be decimal major-version strings")
    common = local_set & remote_set & SUPPORTED_PROTOCOL_VERSIONS
    if not common:
        raise ValueError("no mutually supported protocol version")
    return max(common, key=int)
