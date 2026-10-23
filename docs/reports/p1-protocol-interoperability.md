# P1 Protocol Interoperability Validation

**Date:** 2026-10-23
**Result:** implementation and automated validation passed; maintainer review is
pending. P2 has not started.
**Protocol:** v2 is the active local protocol implementation.

## Scope delivered

- Added strict v2 signed-message and adapter-descriptor schemas, version
  negotiation, and adapter contracts for compute, transport, public metadata,
  artifacts, randomness, attestations, and settlement.
- Specified canonical UTF-8 JSON, SHA-256 content hashes, UTC timestamps and
  expiry, EIP-712 domain separation, stable principal/key-epoch binding, nonce
  replay protection, and the local identity lifecycle in
  [`../protocol.md`](../protocol.md) and
  [`../protocol/adapter-contracts.md`](../protocol/adapter-contracts.md).
- Added Python and TypeScript/JavaScript protocol implementations with shared
  golden vectors for canonical JSON bytes/hashes, EIP-712 digest/signature,
  OpenZeppelin-compatible Merkle roots/proofs, and invalid/replayed messages.
- Added a persistent SQLite identity directory and replay guard for Python, and
  a restart-safe local Node replay store. These are local stores, not a global
  identity or cross-peer replay authority.
- Added key generation, rotation, revocation, recovery transitions, chain
  configuration checks, and rejection of local development-key creation in
  production mode.
- Retained the prior v1 message schema as an archival P0 draft. Because v2
  changes signed fields, runtime peers require exact v2 negotiation and reject
  v1 rather than silently reinterpret or downgrade signed messages. No deployed
  network migration is claimed.

## Validation evidence

The full offline command completed with exit code 0 on 2026-10-23:

```powershell
$env:PATH = (Resolve-Path .venv\Scripts).Path + [IO.Path]::PathSeparator + $env:PATH
$env:OLLAMA_HOST = 'http://127.0.0.1:1'
$env:AMN_OLLAMA_MODEL = 'offline-test'
pnpm test:all
```

Results:

| Gate | Result |
|---|---:|
| Customer web tests | 4 passed |
| Shared protocol tests | 6 passed |
| Protocol declaration type-check (`tsc --noEmit --strict`) | passed |
| Local Hardhat escrow tests | 4 passed |
| Python tests | 49 passed |
| Total automated tests | 63 passed, 0 failed |
| Ruff on P1 Python implementation and tests | passed |
| `git diff --check` | passed |

The tests ran with Ollama pointed at an unavailable loopback endpoint and a
placeholder model name, so this gate did not call an inference service. The
web, protocol, contract, and Python test outputs were captured locally at
`.amn/p1-validation/full-suite.log` (ignored local validation data).

The protocol tests also check Python/TypeScript byte and signature agreement,
wrong chain/domain, altered payload, expired and future-dated messages,
revoked signer resolution, unknown protocol versions, duplicate message IDs
and nonces, restart persistence, replay races, identity rotation/revocation/
recovery, and OpenZeppelin Merkle compatibility.

To regenerate the vectors and run the cross-language test gate:

```powershell
python scripts/generate_protocol_vectors.py
pnpm test:protocol
```

The vector's signing key is explicitly public test data and must never be used
for funds or production identity.

## Compatibility and dependency notes

- Python 3.12.14, `eth-account` 0.13.7, and Pydantic 2.14.1 were used for the
  passing Python environment. Node.js 24.22.1, pnpm 11.25.0, ethers 6.17.0,
  and the already-installed OpenZeppelin Merkle Tree package were used by the
  JavaScript checks. See the lockfiles and
  [`../architecture/integration-matrix.md`](../architecture/integration-matrix.md)
  for API compatibility references.
- P1 uses EIP-712 typed signing (`eth-account` and ethers), EIP-712 as defined
  by [EIP-712](https://eips.ethereum.org/EIPS/eip-712), and the
  [OpenZeppelin StandardMerkleTree format](https://github.com/OpenZeppelin/merkle-tree/blob/master/README.md).
  The Python/TypeScript vector test is the interoperability contract.
- No proprietary service or production network is required by the P1 tests.
  The protocol package reuses the repository's existing Ethereum dependencies;
  no new external identity or coordination service was integrated.

## Security and production limits

- A valid EIP-712 signature establishes that a key signed the bound message
  under the configured chain/domain. It does not prove that AI inference ran,
  that the result is correct, that the key holder is honest, or that the
  claimed hardware/resources were used.
- Expiry and local replay checks are enforced by each receiver using its local
  state. SQLite and Node file stores do not synchronize across machines,
  partitions, or independently operated peers. Production deployment still
  needs a durable, coordinated retention/finality policy that preserves
  coordinator-free operation.
- Signer status is checked only when the caller supplies the local identity
  directory/resolver. There is no global decentralized identity authority or
  Sybil-resistance mechanism in P1. Recovery depends on the configured
  recovery key and its custody.
- `AMN_CHAIN_ID` is required in production mode to prevent accidental signing
  against the development chain. No production chain, signer backend, EAS,
  peer transport, or shared replay service was deployed or validated here.
- v2 expiry validation bounds message lifetime and future clock skew, but
  operators must keep clocks accurate. Peers reject unknown versions instead
  of negotiating a silent fallback.
- Public golden vectors include a known private key solely to make signatures
  reproducible. It is insecure by design and cannot protect assets.

P1 closes its local interoperability gate. P2 must still be reviewed and
approved before implementation begins.
