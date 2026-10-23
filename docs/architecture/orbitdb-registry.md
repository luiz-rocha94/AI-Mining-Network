# Public metadata registry (P5)

`@ai-mining-network/registry` implements the v2 `PublicMetadataStore` adapter
with OrbitDB 4's append-only `events` database. It uses the P4
`HeliaArtifactStore` for IPFS blocks and can borrow the P3 libp2p node so that
OrbitDB synchronization and AMN peer messaging share one encrypted peer
connection and one GossipSub service.

```mermaid
flowchart LR
  Author[Skill author] -->|EIP-712 signed v2 record| Adapter[AMN PublicMetadataStore]
  Adapter -->|append immutable event| OrbitDB[(OrbitDB events log)]
  OrbitDB --> Helia[P4 Helia blockstore]
  Helia --> P3[P3 libp2p node with OrbitDB topics enabled]
  P3 <-->|GossipSub + Bitswap| Peer[Independent registry peer]
  Peer --> Verify[AMN signature, identity, schema, replay, policy]
  Verify --> Index[Derived in-memory search view]
```

## Opening a peer

OrbitDB 4 requires Node.js 22 or newer. Persist the Helia and OrbitDB
directories, and retain the P3 transport for the lifetime of the store:

```js
import { createPeerTransport } from "@ai-mining-network/networking";
import { HeliaArtifactStore } from "@ai-mining-network/storage";
import { OrbitDbPublicMetadataStore } from "@ai-mining-network/registry";

const peer = await createPeerTransport({
  storageDirectory: "./data/network",
  identityResolver,
  peerPrincipalResolver,
  authorizePrivateSender,
  authorizePrivateRecipient,
  allowOrbitDbTopics: true,
});
const artifacts = await new HeliaArtifactStore({
  storageDir: "./data/helia",
  libp2pNode: peer.node,
}).open();
const registry = await OrbitDbPublicMetadataStore.create({
  artifactStore: artifacts,
  directory: "./data/orbitdb",
  identityId: "publisher-key-name",
  identityResolver,
  authorizeRecord,
  // Supply databaseAddress when joining an existing registry.
});
```

`allowOrbitDbTopics` is opt-in because OrbitDB topic names derive from the
database manifest and are not known when the libp2p node starts. With the
option enabled, libp2p accepts other validly signed pubsub topics, while AMN's
P3 event handler still processes only its own AMN public-event topic. OrbitDB
uses the remaining topics for its own synchronization. The P3 peer transport
must remain open until after the registry and Helia store close.

## Record format and validation

Every OrbitDB event value is the canonical JSON representation of an AMN
protocol v2 EIP-712 envelope. OrbitDB signs its own database operation too;
that log signature authenticates the writer key to OrbitDB, while the nested
AMN envelope binds the public claim to an AMN principal and key epoch. The
adapter validates the AMN signature, time window, replay ID/nonce, active or
historical signer binding, per-kind schema, local authorization callback, and
256 KiB record-size cap before appending. When reading replicated log entries,
the adapter rebuilds replay state in canonical order and validates them at
their signed creation time; `identityResolver(principalId, envelope)` should
resolve the signing key for that historical key epoch.

Accepted payload kinds:

| Kind | Public fields and handling |
|---|---|
| `SkillManifest` | Reuses the protocol v2 JSON schema. Each immutable content hash remains attributable to its signed AMN principal. |
| `SkillBenchmark` | Reuses the v2 schema. Metrics remain signed claims; the registry does not attest that a benchmark ran. |
| `SkillRating` | Reuses the v2 schema. Ratings and comments are public and should not contain private or identifying task data. |
| `SkillRevocation` | Registry schema v1: `revocationId`, target `contentHash`, and optional short `reason`. Only the principal that published a matching manifest can append a revocation that hides that version from the default skill-search view. The append-only log retains the original manifest and revocation. |
| `PublicAuditCheckpoint` | Registry schema v1: `checkpointId`, `recordsRoot`, `recordCount`, and a bounded list of covered record IDs. Denied by default; a deployment must explicitly authorize checkpoint signers through `authorizeRecord`. A checkpoint is a signed claim, not a consensus oracle. |

Unknown record kinds and task-state payload kinds such as `CustomerTask`,
`ExecutionContract`, `VerificationReceipt`, and `PaymentSettlement` are rejected
by this adapter. Customer prompts, PDFs, ground truth, and result bodies belong
in authenticated task channels or encrypted artifacts, never public OrbitDB.
The adapter cannot stop a client from bypassing AMN and writing directly to
OrbitDB; peers still receive those log entries, but compliant AMN readers reject
them. Do not use the public database as a private-data control.

## Deterministic views

- The log is append-only. Search results are derived from validated events and
  are never written back as authoritative mutable indexes.
- Exact retries with the same AMN message ID and canonical envelope return the
  prior AMN record ID; an ID reused with another envelope is a conflict.
- If peers replicate two signed envelopes with the same message ID, each reader
  picks the lexicographically first `payloadHash`, then canonical envelope
  bytes. An AMN nonce reused under another message ID is rejected by deterministic
  replay reconstruction.
- Conflicting skill claims are retained. For the same author/name/version,
  `conflicts()` exposes every version claim in `payloadHash` then canonical-byte
  order; `discoverSkills()` returns all claims sorted by name, version, author,
  content hash, and message ID. No claim silently overwrites another.
- A revocation is effective only for a matching content hash and the same
  principal as the signed manifest. Other authors publishing the same bytes
  remain independently discoverable.
- OrbitDB/Helia converge eventually when peers exchange the database address,
  can connect, and retain/fetch the necessary IPFS blocks. A local pin does not
  guarantee global persistence or immediate convergence.

`status()` reports local valid and rejected counts and a snapshot of connected
peers. `subscribe()` reports local OrbitDB update events. Neither is a
replication quorum or finality guarantee.

## Reproduction and trust boundaries

```powershell
pnpm test:registry
pnpm demo:registry
```

The acceptance test creates two independent P3 libp2p identities and P4 Helia
stores. It checks concurrent publication, two-peer conflict agreement,
duplicates, signature/identity/schema/policy rejection, author-only
revocation, private task-record rejection, concurrent offline writes, node
restart, rejoin, and convergence. The demo publishes a public sample skill and
discovers it from a second peer.

The database manifest uses OrbitDB's immutable IPFS access controller with
`write: ["*"]`; OrbitDB's current controller does not provide the adapter's
per-record application policy. This enables permissionless authoring without a
central write coordinator but allows peers to flood the public log or bypass
AMN validation. `authorizeRecord` must be configured consistently across
peers. The default callback permits manifests, benchmarks, ratings, and
revocations, and rejects audit checkpoints.

Cryptography establishes that an AMN key signed a bounded canonical claim and
that OrbitDB's log entries are signed by OrbitDB identities. It does not prove
the claim is true, establish independent authorship, prevent collusion, prove a
benchmark ran, or provide global consensus. OrbitDB replication is eventual;
conflicting signed claims remain claims. Historical key resolution, author
identity policy, public spam resistance, bootstrap discovery, and integration
into the customer/miner runtime remain production work.

## Upstream compatibility and license

- [OrbitDB 4 getting started and Helia/libp2p setup](https://github.com/orbitdb/orbitdb/blob/main/docs/GETTING_STARTED.md)
- [OrbitDB access controller: wildcard public writes](https://github.com/orbitdb/orbitdb/blob/main/docs/ACCESS_CONTROLLERS.md)
- [OrbitDB database and event-log API](https://github.com/orbitdb/orbitdb/blob/main/docs/DATABASES.md)
- [OrbitDB replication](https://github.com/orbitdb/orbitdb/blob/main/docs/REPLICATION.md)
- [OrbitDB package metadata (version, Node engine, MIT license)](https://github.com/orbitdb/orbitdb/blob/main/package.json)

The tested OrbitDB package is `@orbitdb/core` 4.0.0 (MIT, Node >=22). The P5
adapter uses the existing `@libp2p/gossipsub` 17.x dependency (MIT), Helia
7.1.x (MIT), DAG-CBOR 10.0.2 (Apache-2.0 OR MIT), AJV (MIT), and AMN's P3/P4 packages.
See the [generated dependency license report](../reports/dependency-licenses.md).
