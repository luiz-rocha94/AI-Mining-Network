# Version 2 adapter contracts

The adapter descriptor format is
[`adapters.schema.json`](../../packages/protocol/v2/adapters.schema.json).
Every implementation MUST declare `protocolVersion`, interface and contract
version, its supported operations, idempotency behavior, retryable errors,
and trust notes. Implementations MUST return structured errors rather than
silently switching backends. A deployment marked production MUST fail startup
when any required production adapter is absent or reports
`productionReady: false`.

All requests use a signed v2 envelope where the operation changes public task
state, identity, evidence, or payment. Each call has a unique request/message
ID, a bounded expiry, explicit chain/domain, and an idempotency key for
mutations. Results identify the request ID and operation state. Private
artifacts travel only over an authenticated, authorized channel and are not
embedded in public metadata records.

| Interface | Required operations | Contract guarantees and errors |
|---|---|---|
| `ComputeBackend` | `capabilities`, `offers`, `submit`, `status`, `cancel`, `fetchArtifacts` | Bind selected offer, image/runtime digest, resource limits, deadline, and accepted quote. Report `UNAVAILABLE`, `OFFER_EXPIRED`, `CANCELLED`, `TIMEOUT`, `PROVIDER_FAILED`, and `ARTIFACT_MISSING`. Dispatch uses a stable execution-contract ID. |
| `PeerTransport` | `localIdentity`, `publish`, `request`, `subscribe`, `close` | Authenticate peers, preserve the signed message bytes, bound message size/rate, and report `PEER_UNAVAILABLE`, `UNAUTHORIZED`, `DUPLICATE`, or `BACKPRESSURE`. It does not decide Sybil eligibility or task truth. |
| `PublicMetadataStore` | `append`, `get`, `query`, `subscribe` | Store signed, public records only; return record IDs and observed replication status. Handle `CONFLICT`, `NOT_FOUND`, and `REPLICATION_PENDING`. Conflict resolution is deterministic but does not make a signed claim true. P5 uses an append-only OrbitDB event log; task/private payload kinds are rejected by the adapter, while wildcard OrbitDB writes still allow direct clients to bypass AMN validation. |
| `ArtifactStore` | `put`, `get`, `pin`, `status`, `deleteLocal` | Verify content hash/CID on read, report availability and retention, and preserve encrypted bytes for private artifacts. `put` requires an explicit public/private policy. Errors include `HASH_MISMATCH`, `NOT_FOUND`, `UNAVAILABLE`, and `ACCESS_DENIED`. `deleteLocal` does not claim global deletion. |
| `RandomnessSource` | `chainInfo`, `futureRound`, `verifyRound` | Return chain identity, round, randomness, signature/chaining proof, and verification status. Reject stale, wrong-chain, replayed, or invalid rounds. A simulated source MUST return `verified: false`. |
| `AttestationPublisher` | `schema`, `publish`, `fetch`, `verify` | Bind schema UID, chain ID, signer, subject digest, and revocation/expiry status. Publish only customer-authorized digests; do not publish task plaintext. EAS availability does not imply correctness. |
| `SettlementBackend` | `quote`, `fund`, `settle`, `refund`, `status` | Require the accepted quote ceiling, decision digest, task/domain binding, replay-safe idempotency key, integer amounts, and explicit terminal state. Report `INSUFFICIENT_FUNDS`, `DEADLINE`, `RPC_UNAVAILABLE`, `REORG`, `INVALID_DECISION`, and `ALREADY_SETTLED`. |

## Version negotiation

An implementation MUST advertise the exact supported protocol version set
before exchanging state-changing messages. Version 2 peers MUST reject a
message whose signed `protocolVersion` is not `"2"`; they must not reinterpret
unknown fields or downgrade an unsupported version. Schema evolution that
changes canonical hashing or signed fields requires a new protocol version.
Adapter `contractVersion` is independent: compatible minor/patch contracts
retain the v2 message encoding and operation semantics.

## Delivery and retries

Reads MAY be retried. Mutating calls MUST use idempotency keys and MUST return
the original result for an exact retry; reuse with different request content
MUST fail. A transport delivery acknowledgement means only that a peer
accepted the message for processing. It is not an execution, verification, or
settlement attestation. Implementations retain replay state at least until the
signed message expires, and longer if needed for task-level idempotency.
