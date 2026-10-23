# Protocol v1

All JSON schemas are generated at [`packages/protocol/v1/messages.schema.json`](../packages/protocol/v1/messages.schema.json) from `models.py`. Each wire payload is carried in `SignedMessage`, which supplies `protocolVersion`, message `id`, `createdAt`, EVM `signer`, `nonce`, `payloadHash`, and `signature`. `ExecutionUnit` describes a committed unit and is represented in the task result artifact; signed protocol envelopes wrap the network messages.

## Canonical hashing and signatures

- Signed payloads use the protocol's integer-only RFC 8785-compatible subset: UTF-8 JSON, recursively sorted ASCII object keys, compact separators, no floats, and integers within JavaScript's safe integer range. Monetary amounts use integer atomic units. Canonical JSON is SHA-256 hashed with a `sha256:` prefix.
- EIP-712 domain: name `AI Mining Network`, version `1`, configured chain ID (local demo: `31337`). The `ProtocolMessage` structure binds protocol version, SHA-256 message identifier, UTC timestamp in milliseconds, payload kind, payload hash, and 32-byte nonce. `signing.py` recalculates the canonical payload hash and recovers the ECDSA signer before accepting an envelope.
- Ethereum ECDSA signatures prove control of the signing key for that typed message. They do not prove identity, independence, eligibility, task correctness, or that inference ran.
- The skill package digest is SHA-256 over `AMN-SKILL-PACKAGE-V1\0`, then sorted normalized relative paths and file bytes; `manifest.json` is excluded to avoid hash self-reference. The manifest separately includes that digest and is signed by the author CLI.
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

The customer API accepts a multipart `/api/quote` request containing optional `files`, customer `prompt`, a versioned `workflow` JSON object, `model`, `sampleCount`, and `validatorCount`. The legacy `skills` array/single `skill` fields remain available for clients without graph support. A workflow has a fixed Input and Final output node, one to ten unique skill nodes, and directed edges. Each skill node has one upstream input from Input or an earlier skill node, so the graph is acyclic; Output joins all terminal skill nodes. For one terminal node, the committed result is that node's structured output. For multiple terminal nodes, the result is keyed by their skill names. The quote sums each skill node's local execution and verification estimate and returns node-level line items. Prompt-only requests are represented by one stable synthetic task-input unit; the prompt itself is separately signed into the task envelope. Local uploads are restricted to text-based PDF, TXT, Markdown, CSV, and JSON, with 100 files maximum, 10 MiB per file, 25 MiB aggregate upload, and a 2,000,000-character aggregate extraction bound. Private input bytes remain under `.amn/jobs/<task-id>/private-input/` and are filtered from the artifact endpoint. Remote Ollama is disabled unless explicitly enabled with `AMN_ALLOW_REMOTE_OLLAMA=1`.

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

## Local escrow rules

The Python demo ledger is keyed by `task:<task-id>:settle:v1`. Repeating the same settlement returns the original record; reusing that key with different settlement content is rejected. The local contract has task-ID replay protection, exact token transfers via OpenZeppelin `SafeERC20`, typed validator-quorum decision signatures, a stored decision digest, and deadline refund. A repeated identical decision is a no-op; a conflicting decision fails.

The Solidity primitive deliberately does not verify drand, randomness-derived validator selection, EAS, task artifacts, or consensus correctness. `assignVerifiers` only enforces assignment after a committed root and binds an evidence hash; the caller must validate that evidence in the protocol layer. Do not deploy this contract as a complete trustless marketplace.

## Event audit

Local events are append-only through the API and include sequence, payload, previous hash, and event hash per task. Any alteration within the stored chain changes a later digest. This is tamper-evident only if an independently retained head hash exists; a machine administrator able to rewrite the complete SQLite database can rewrite the chain. Production peers should gossip signed events and anchor checkpoints in independently replicated storage/attestations.
