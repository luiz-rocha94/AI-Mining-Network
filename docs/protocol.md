# Protocol v2

P7 adds an optional `ExecutionContract.backendBinding` for local integration
work. This changes the signed payload of newly created contracts; historical
contracts may omit it. There is no production v2 peer network yet. Before
external peers deploy this field, coordinate an all-peer schema upgrade or
introduce a new protocol version. Mixed v2 peers must not assume that they can
sign or verify the same contract payload during this transition.

The earlier P0 envelope was an unreleased local v1 draft. P1 adds signed
expiry, stable principal identity, key epoch, and new EIP-712 fields, so the
active runtime uses protocol v2 instead of reinterpreting v1 signatures. The
v1 JSON schema is retained at `packages/protocol/v1/messages.schema.json` as
an archival draft only; current nodes reject it and no automatic migration is
provided. No production peer network was released on v1.

All message schemas are generated at
[`packages/protocol/v2/messages.schema.json`](../packages/protocol/v2/messages.schema.json)
from `models.py`. A `SignedMessage` carries `protocolVersion`, unique message
`id`, UTC millisecond `createdAt` and `expiresAt`, stable `principalId`,
`keyEpoch`, EVM `signer`, 32-byte `nonce`, typed `payload`, `payloadHash`, and
`signature`. `ExecutionUnit` is represented in the private task result
artifact. Versioned external adapter operations are described in
[`protocol/adapter-contracts.md`](protocol/adapter-contracts.md) and their
descriptor shape is defined in
[`packages/protocol/v2/adapters.schema.json`](../packages/protocol/v2/adapters.schema.json).

## Canonical hashing and signatures

- Signed payloads use the protocol's integer-only RFC 8785-compatible subset: UTF-8 JSON, recursively sorted ASCII object keys, compact separators, no floats, and integers within JavaScript's safe integer range. Monetary amounts use integer atomic units. Optional `null` fields are omitted from the semantic payload before hashing; schema-defaulted fields are materialized and must be present in the signed payload. Canonical JSON is SHA-256 hashed with a `sha256:` prefix. Shared vectors and the TypeScript implementation are in `packages/protocol/v2/`.
- EIP-712 domain: name `AI Mining Network`, version `2`, configured chain ID (local demo: `31337`). The `ProtocolMessage` structure binds protocol version, SHA-256 message identifier, UTC creation and expiry milliseconds, stable principal ID, recovered EVM signer address, key epoch, payload kind, payload hash, and 32-byte nonce. The signer address is checked against the recovered ECDSA key; the typed signature binds the principal and key epoch to that key. Python and TypeScript use the same field order and EIP-712 digest.
- The exact EIP-712 member order is `ProtocolMessage(uint16 protocolVersion,bytes32 messageId,uint64 createdAtMs,uint64 expiresAtMs,string principalId,address signer,uint64 keyEpoch,string messageType,bytes32 payloadHash,bytes32 nonce)`. `messageId` is `SHA256(UTF8(id))`; hash strings drop their `sha256:` prefix when encoded as `bytes32`; nonce is exactly 32 bytes. Domain separation includes `chainId`. `AMN_CHAIN_ID` selects the configured chain; development defaults to Hardhat `31337`, while production requires the variable explicitly.
- Ethereum ECDSA signatures prove control of the signing key for that typed message. They do not prove identity, independence, eligibility, task correctness, or that inference ran.
- `validate_envelope` checks `protocolVersion == "2"`, payload hash/signature, bounded expiry (7 day maximum), and future clock skew (2 minutes). A live receiver MUST additionally call `IdentityDirectory.authenticate` (Python) or provide `identityResolver` (TypeScript) to enforce registered signer/key-epoch status; these checks are opt-in because the library has no global identity authority. Historical `verify_message` checks signature integrity without requiring that an archived receipt remain unexpired. Version negotiation is exact: v2 peers reject unknown versions and do not silently downgrade.
- Replay protection consumes both `(chainId, signer, nonce)` and `(chainId, messageId)` until expiry. The Python local peer stores these in SQLite transactions; the Node adapter stores atomic, exclusive per-key files. Restart tests verify persistence. Production peers still need a network-wide retention and partition policy; local replay stores alone do not coordinate independent machines.
- A skill package digest is SHA-256 over `AMN-SKILL-PACKAGE-V2\0`, the normalized canonical JSON manifest without its `contentHash` (prefixed by an unsigned 64-bit big-endian byte length), then every package file except root `manifest.json` in sorted normalized POSIX-path order. Manifest defaults and the description from `SKILL.md` frontmatter are materialized before hashing. Each path is prefixed by its unsigned 32-bit big-endian UTF-8 byte length; each file body is prefixed by its unsigned 64-bit big-endian byte length. The digest therefore binds version, schemas, tool allow-list, resource requirements, `SKILL.md`, and all package assets. `scripts/update_skill_hash.py <skill-directory>` refreshes the embedded digest. The AMN canonical manifest subset permits only safe integers, strings, booleans, arrays, objects, and null; use exact decimal strings for monetary or fractional outputs.
- General document `CustomerTask` payloads additionally sign hashes of the exact customer prompt and output schema, the selected Ollama model name and digest, and (for graph requests) `workflowHash`. The prompt text stays in the private job record; execution rechecks these bindings, graph edges, skill package versions, and instructions before calling the model.
- A workflow hash is `content_hash({version, nodes, edges})`, where each skill node includes its content-addressed `skillHash`; connection order is preserved. This binds both the selected skill versions and the workflow topology without adding skill names to the customer's prompt.

## Unit commitments and proof rules

Each committed work-unit leaf (one uploaded document in the general customer flow) is the OpenZeppelin `StandardMerkleTree` leaf for `bytes32,bytes32,bytes32` values:

```text
unitIdHash = SHA256(UTF8(stableUnitId))
inputHash  = SHA256(original input file bytes)
outputHash = SHA256(canonical JSON of the committed per-unit result)
leaf       = keccak256(keccak256(abi.encode(unitIdHash, inputHash, outputHash)))
node       = keccak256(min(left, right) || max(left, right))
```

Leaves are sorted as in OpenZeppelin's standard tree. The Python implementation and the installed OpenZeppelin JS package share a golden root in their automated tests. An inclusion proof proves that the input/output hash tuple is a leaf under the signed root. It does not prove PDF semantics, inference, completeness, or output correctness.

The artifact hash commits to a canonical file manifest of the JSON and XLSX output file hashes. The signed `ExecutionCommitment` includes task/contract IDs, root, artifact hash, unit count, leaf encoding, commit timestamp, and future challenge round. The validator audit CLI recomputes the evidence hash and proof from each receipt's committed output.

Python and TypeScript commitment interoperability vectors include the
OpenZeppelin `StandardMerkleTree` leaf inputs, root, and inclusion proof. A
proof only demonstrates membership under the supplied root; it is not a
correctness proof.

## Identity and signing-key lifecycle

An identity has a stable `principalId`, current EVM signer address, monotonic
`keyEpoch`, an independent recovery signer, and `ACTIVE` or `REVOKED` status.
The local `IdentityDirectory` keeps this binding and the ordered signed
transitions in SQLite:

- **Create:** generate an EVM key and a different recovery key; register the
  public addresses through the configured genesis trust process. The directory
  does not store either private key.
- **Rotate:** the currently bound key signs an `IdentityTransition` naming the
  old key, next sequence, and replacement address. The replacement must differ
  from both current and recovery keys. The new key signs messages at the
  incremented epoch.
- **Revoke:** the current key signs a revocation transition. Live admission
  rejects that identity until recovery.
- **Recover:** the separately registered recovery key signs a recovery
  transition after revocation, naming a new key and the previous current key.
  The epoch advances again. Loss or compromise of both keys has no automated
  recovery path.

The signed transition and the current binding are checked atomically, and
duplicate/stale transition sequence numbers fail. This provides local key
rotation and revocation mechanics, not a global identity or Sybil authority.
The genesis registry and recovery-key custody remain trust assumptions.

`AMN_ENV=production` rejects the local `.amn/identities/*.key` file signer.
Production use therefore fails closed until an external/hardware signer and
an independently governed identity directory are configured. The local
identity files are development-only plaintext private keys protected by
filesystem permissions where supported.

The customer API accepts a multipart `/api/quote` request containing optional `files`, customer `prompt`, a versioned `workflow` JSON object, `model`, `sampleCount`, and `validatorCount`. The legacy `skills` array/single `skill` fields remain available for clients without graph support. A workflow has a fixed Input and Final output node, one to ten unique skill nodes, and directed edges. Each skill node has one upstream input from Input or an earlier skill node, so the graph is acyclic; Output joins all terminal skill nodes. For one terminal node, the committed result is that node's structured output. For multiple terminal nodes, the result is keyed by their skill names. P6 quotes include execution, verification, protocol, storage, and settlement lines, offer-source label, selected-offer snapshot, assumptions, expiry, and failure/refund rules. Current customer workflow offers remain `local-fixture` estimates, not live market prices. Prompt-only requests are represented by one stable synthetic task-input unit; the prompt itself is separately signed into the task envelope. Local uploads are restricted to text-based PDF, TXT, Markdown, CSV, and JSON, with 100 files maximum, 10 MiB per file, 25 MiB aggregate upload, and a 2,000,000-character aggregate extraction bound. Private input bytes remain under `.amn/jobs/<task-id>/private-input/` and are filtered from the artifact endpoint. Remote Ollama requires an HTTPS `OLLAMA_HOST` and explicit `AMN_ALLOW_REMOTE_OLLAMA=1` authorization.

## Challenge derivation and sampling

The production derivation is specified as:

```text
seed = SHA256("AI-MINING-NETWORK-POUW-V1\0" || taskId || 0x00 || commitmentRoot || 0x00 || round || 0x00 || randomness)
```

Validator and unit order are deterministic SHA-256 rankings over domain-separated seed/identity tuples. The executor and declared conflicts are excluded. The first `sampleCount` distinct ranked units are distributed round-robin over the selected validators. The eligible set and conflict policy must be independently auditable and Sybil-resistant in production.

The local implementation computes this deterministically after the miner commits but obtains randomness from an in-process `SimulatedBeacon`. It sets `randomness.verified: false`. That adapter is for tests only. Production must fetch and cryptographically verify a future drand round, bind the chain hash/round/previous signature and preserve the proof. A random selection is only unpredictable if the randomness source is independent of the executor/selector and the eligible identities are credible.

Sampling gives probabilistic detection. For `N` total units, `k` corrupted units, and `s` uniformly sampled distinct units, probability of detecting at least one is:

```text
1 - C(N-k, s) / C(N, s)
```

The equation assumes uniform unbiased selection and that a sampled corruption is detectable by the assigned verification policy. It does not bound collusion, shared model errors, or false evidence.

## Verification receipts and consensus

Each assignment is a signed `VerificationAssignment` created only after commitment; each receipt is a signed `VerificationReceipt` containing task/root, validator, assigned unit, result, evidence hash, proof, reason code, model digest, and duration. Invoice-demo evidence identifies the committed and independently observed output plus the deterministic synthetic source field reference. General document-task evidence records an independent seeded Ollama replay and Merkle membership result. The raw evidence is private to the local task directory in this demo and not published to a public store.

Policies:

- `PASS`: input fields, independently extracted fields, and committed record agree; the proof verifies.
- `FAIL`: source evidence or commitment membership establishes an objective mismatch.
- `INCONCLUSIVE`: independent extraction disagrees, a validator is absent, a receipt is missing, or evidence cannot be resolved.
- `DISPUTED`: validators return conflicting outcomes for the same unit. An additional audit is required; the MVP leaves funds held.
- A verified result requires the expected sample receipt count and every assigned unit to pass. A source-proven failed unit refunds the execution reward; valid validators are paid their accepted receipt fees. Inconclusive results refund execution and pay valid receipts. Unresolved disputes have no automatic payment in the local Python ledger.

Majority vote is not used as sole proof. The invoice MVP anchors verification on exact synthetic source fields plus an independent model re-extraction; this narrow evidence is only suitable for its synthetic labeled corpus. General document replay checks that assigned units reproduce the committed structured output using the selected model and fixed sampler seed. A match does not establish factual correctness, and independent runs can still disagree; those samples are inconclusive and follow the quote's refund policy. Arbitrary free-form AI tasks do not gain a cryptographic correctness proof from a repeated answer.

## State transitions

```text
CREATED → QUOTED → FUNDED → ASSIGNED → EXECUTED → VERIFYING → VERIFIED → SETTLED
                                                        ├→ FAILED → REFUNDED
                                                        ├→ INCONCLUSIVE → REFUNDED
                                                        └→ DISPUTED (held for audit)
```

Quotes expire. Execution and verification failure rules are carried in the fixed quote. The quote maximum is immutable after acceptance; changing it requires a new quote and explicit customer acceptance. Every atomic amount is an integer.

P6 `SkillBenchmark` receipts carry `runtimeDigest`, `warmColdPolicy`, raw
`durationSamplesMs`, integer `durationVarianceMs2`, raw
`outputValidationResults`, and `measurementProvenance` in addition to skill,
model, runtime label, hardware class, sample count and summaries. P50 and P90
use nearest-rank percentiles; variance is the population variance rounded to
integer ms². The receipt validator recomputes these summaries from the signed
observations. The current benchmark implementation emits at least 20 samples
and labels every local observation as self-reported. A contributor signature
authenticates a key and payload, not inference or hardware.

`FixedPriceQuote` binds `offerSnapshotHash`, `priceBasis`, and `assumptions`,
then separately prices execution, verification, protocol, storage, and
settlement. `priceBasis=local-fixture` must be shown as such and is not a live
market quote. At acceptance the local customer service verifies the signed
quote, task maximum, selected offer signatures, selected offer amounts,
currency/skill match, expiry, distinct peers, and snapshot hash. If an offer
expires or changes, the customer must accept a new quote. Local settlement
checks every payout against the accepted component budgets and conserves the
full funded quote by paying or refunding it.

Protocol v2 atomic values do not exceed `2^53 - 1`. This limit keeps JSON number
arithmetic exact in both Python and JavaScript clients. A future protocol that
needs larger values must encode them as canonical decimal strings and update
the cross-language signing schema.

## Local escrow rules

The Python demo ledger is keyed by `task:<task-id>:settle:v1`. Repeating the same settlement returns the original record; reusing that key with different settlement content is rejected. The local contract has task-ID replay protection, exact token transfers via OpenZeppelin `SafeERC20`, typed validator-quorum decision signatures, a stored decision digest, and deadline refund. A repeated identical decision is a no-op; a conflicting decision fails.

The Solidity primitive deliberately does not verify drand, randomness-derived validator selection, EAS, task artifacts, or consensus correctness. `assignVerifiers` only enforces assignment after a committed root and binds an evidence hash; the caller must validate that evidence in the protocol layer. Do not deploy this contract as a complete trustless marketplace.

## Event audit

Local events are append-only through the API and include sequence, payload, previous hash, and event hash per task. Any alteration within the stored chain changes a later digest. This is tamper-evident only if an independently retained head hash exists; a machine administrator able to rewrite the complete SQLite database can rewrite the chain. Production peers should gossip signed events and anchor checkpoints in independently replicated storage/attestations.

## Peer messaging and delivery (P3)

The `@ai-mining-network/networking` package uses libp2p TCP + Noise + Yamux,
identify, strict-signed GossipSub, and mDNS. `SkillManifest`,
`SkillBenchmark`, miner/validator capabilities and offers, quotes, contracts,
commitments, assignments, verification receipts/decisions, settlements,
ratings, and identity transitions are allowlisted for the public
`amn/protocol/v2/public` topic. `CustomerTask` and unknown/private kinds are
direct-only. Private messages are not stored in the public event log. The
direct RPC uses a readiness handshake and a bounded four-byte big-endian
length-prefixed canonical JSON frame; Noise encrypts the peer connection.

The application must provide an active EIP-712 identity resolver, a mapping
from authenticated libp2p PeerId to AMN principal, and private sender/recipient
authorization callbacks. The direct sender and recipient must map to their
connected peers, and a response must be signed by the responder's mapped
principal. libp2p PeerId authentication and AMN EIP-712 signatures are separate
checks: neither establishes personhood or Sybil resistance.

GossipSub delivery is best effort. On connection, one deterministically chosen
peer initiates bounded public-event anti-entropy in both directions. Active,
unexpired, non-revoked signed events are paged in stable timestamp/signer/ID
order. The local durable event store rejects changed bodies under an existing
message ID. Reconnection repairs missed public events; this is eventual
peer-to-peer synchronization, not a global consensus log. Operators can call
`syncPublicEvents(peerId)` to reconcile a connected peer again. mDNS is limited
to a local network; remote participants need bootstrap addresses or another
discovery module. There is no DHT, relay, NAT traversal, or network-wide
identity/Sybil policy in P3. See
[`peer-networking.md`](architecture/peer-networking.md) for configuration and
operating limits.
