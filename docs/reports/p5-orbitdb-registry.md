# P5 OrbitDB registry report

**Status:** local P5 acceptance suite and two-peer demo passed on 2026-10-23.
The adapter is not connected to the customer/miner workflow and is not
production-ready.

## Implemented

- `@ai-mining-network/registry` provides append/get/query/subscribe/status over
  an OrbitDB v4 append-only events database and P4 Helia.
- P4 Helia can borrow the P3 libp2p node. P3's GossipSub topic allowlist remains
  restrictive by default; `allowOrbitDbTopics: true` is an explicit option for
  peers that also host OrbitDB.
- AMN v2 EIP-712 signatures, envelope expiry, replay IDs/nonces, key bindings,
  per-kind JSON Schemas, policy callbacks, and a 256 KiB cap are checked before
  compliant adapter writes and when materializing replicated records.
- Public types include skill manifests, benchmark submissions, ratings,
  author-bound skill revocations, and policy-authorized audit checkpoints.
  Private task kinds are not accepted.
- Append-only signed claims are materialized into stable query/search views.
  Exact retries, duplicate IDs, nonce replays, revocations, and concurrent
  skill-version claims have deterministic behavior.
- `todo.md`, architecture, production readiness, threat model, integration
  matrix, README, and this report describe scope and remaining risks.

## Verification

`pnpm test:registry` passed 2 test cases. The multi-peer test used two independent
P3 libp2p + P4 Helia + OrbitDB peers and verified concurrent publication,
deterministic conflict views, duplicate retry, forged signature rejection,
unauthorized revocation/checkpoint rejection, private task-state rejection,
author revocation, offline partition writes, process restart, peer reconnection,
and identical final record sets. The policy test verified malformed schema,
principal/signer mismatch, stable conflict ordering, and the absence of private
task sentinel values from the public registry view.

`pnpm demo:registry` passed. It printed the OrbitDB database address, two peer
IDs, and the skill manifest independently discovered by the reader peer.

The full `pnpm test:all` suite passed with the repository virtual environment
on `PATH`: 4 UI tests, 6 protocol tests and typecheck, 10 networking tests, 6
storage tests, 2 registry tests, 4 Hardhat contract tests, and 69 Python tests.

## Runtime integration

The standalone demo still uses a two-peer local loopback network. No public
bootstrap peers, DHT rendezvous, or third-party operators were used. OrbitDB
uses wildcard writes in the immutable access controller so independent authors
can add events. The adapter validates data at its boundary, but a direct OrbitDB
client can bypass that boundary and append arbitrary events or flood the log.
Validated API views converge only when peers share a compatible identity
resolver and authorization policy. Historical key epochs require a resolver
that can still identify the signer at the event's creation time.

OrbitDB's database metadata contains public records only. It does not carry task
prompts, documents, ground truth, outputs, or settlement. Customer workflow
state remains in its existing local SQLite runtime, and private task artifacts
remain outside public databases. The public registry does not authenticate
claims beyond their signatures and declared identity policy.

## Upstream and license

Compatibility was checked against OrbitDB's official v4 docs and current
package metadata. `@orbitdb/core` 4.0.0 is MIT licensed and requires Node >=22;
the tested host used Node 24.22.1. The existing P4 Helia stack is 7.1.16 and
uses `@libp2p/gossipsub` 17.2.1 and DAG-CBOR 10.0.2. P5 adds explicit Helia DAG-CBOR support because
OrbitDB manifests and log blocks use DAG-CBOR. The updated full dependency
inventory is [here](dependency-licenses.md).
