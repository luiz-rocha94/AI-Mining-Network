# Threat model and guarantees

## Assets

- Customer task inputs, ground truth, outputs, and payment quote.
- Skill package contents and version identity.
- Miner/validator signing keys, offers, assignment records, receipts, reputation records, and settlement state.
- Public randomness proof and post-commit challenge schedule.

## Threats and current mitigations

| Threat | Current control | Remaining limitation |
|---|---|---|
| Miner edits output after committing | Signed artifact manifest hash and root; worker checks files; tamper scenario refunds | A root cannot prove the miner ran AI. A malicious miner can commit a bad answer before the challenge. |
| Miner corrupts selected records | Per-document leaves and source-backed sampled checks | Unsampled corrupt units may pass; detection is probabilistic. |
| Miner chooses favorable validators/samples | Local worker selects only after commitment from a predeclared eligible set | Beacon is in-process and unverified; all identities are local and Sybil resistance is absent. |
| Validator lies, colludes, or disagrees | Signed receipts, signer-to-validator identity binding, evidence hashes, duplicate-unit dispute detection | Signature only binds a key; no independent personhood, stake, hardware, or collusion resistance. |
| Validator outage | Missing receipts become inconclusive; executor reward is refunded; valid receipt workers are paid | No distributed timeout/retry transport or reassignment mesh yet. |
| Prompt injection in document text or skill guidance | Inputs and skill text are labeled untrusted; the model has no shell, filesystem, or user-provided tools; one versioned Ollama call returns schema-validated JSON | Prompt text is not a security boundary; a model can return misleading data. Containment relies on the fixed tool surface, not model obedience. |
| Malformed or path-traversal skill package | Safe YAML loader rejects duplicate keys; JSON Schema 2020-12 validates inline schemas; paths, links, package size, file count, secret-like files, and symlinks are checked before publish and execution; package digest binds manifest and all package content | Supported manifest values are the protocol's integer-only canonical JSON subset; external `$ref` schemas and script execution are unsupported. |
| Malicious downloaded skill code | Skills are declarative `SKILL.md` guidance plus data; the runtime never invokes package scripts; versioned allow-list exposes only `ollama.chat@1.0.0` | The local process is not a hardened OS sandbox. Do not enable script execution; a production provider must add a container/VM or equivalent boundary before accepting arbitrary executable packages. |
| Worker timeout or resource exhaustion | Separate Python worker process and descendants are killed on cancellation/deadline; process-tree CPU time and resident memory are monitored and bounded; stdout/input sizes and scratch lifetime are bounded | CPU/RSS limits are monitor-and-kill checks, not kernel quotas. Polling can overshoot; external Ollama memory, GPU/VRAM, and daemon lifetime are outside those limits. Local GPU/VRAM requirements are rejected. |
| Worker file or network access | Worker receives JSON data through stdin, has no task-file path or inherited secret variables, uses a temporary working/home directory, and the only implemented network tool targets the configured Ollama endpoint | The worker uses the miner's OS identity; scratch `cwd` is not a filesystem jail and application allow-list is not OS-level egress enforcement. Ollama is a separate process. |
| Local browser DNS rebinding | API loopback bind, loopback Host/Origin allowlist, request-size limit | No authentication; assumes one trusted local OS user. Do not expose the port on a LAN. |
| Private task artifacts leak into public storage | Helia `putPrivate` encrypts bytes before block storage with AES-256-GCM; assigned recipients receive X25519/HKDF-wrapped keys; public writes are explicitly authorized and kind-limited | The adapter is not wired into task execution. CIDs and artifact sizes can leak metadata; recipients can retain plaintext; a party that learns a CID can retrieve ciphertext. Local grants/revocations are not replicated or backed by a network identity service. |
| Private input replication | Task inputs stay under ignored `.amn/jobs`; the artifact API filters local private input; the optional Helia adapter stores only ciphertext for its `putPrivate` path | Local job files remain plaintext and same-user processes can read them. Helia cannot revoke remote copies or prove deletion from every peer. |
| Artifact tampering or false retrieval | The Helia store requires signed expected CID and SHA-256 inputs, enforces size/time bounds, and verifies bytes before returning them; Bitswap block CIDs detect altered blocks | Caller signature validation is the caller's responsibility; byte integrity does not prove authorship or semantic correctness. |
| Artifact unavailable after pin | Persistent local blockstore, explicit pin metadata/status, configured peer connections, optional bootstrap DHT, and retention cleanup | A pin is local only. There is no global persistence guarantee, gateway fallback, or validated public DHT deployment. Unpin/GC cannot erase other peers' copies. |
| Private input goes to remote inference | Ollama defaults to localhost; remote Ollama requires HTTPS plus `AMN_ALLOW_REMOTE_OLLAMA=1` | Explicit opt-in discloses task text and selected fields to the configured endpoint. The customer must trust that endpoint. |
| Benchmark poisoning | Signed contributor receipt and private synthetic ground truth for local benchmark | All measurements are self-reported; no independent hardware attestation, duplicate-run policy, or production aggregate exists. |
| Settlement replay/double pay | Python SQLite idempotency key; EVM task replay state, decision hash, exact integer amounts | Python settlement is not a distributed consensus system; Solidity contract is not connected to the demo. |
| Audit-log rewriting | Per-task event hash chain detects internal edits when head hash is retained | SQLite owner can rewrite the entire chain; no external checkpoint/attestation in the MVP. |
| Quote manipulation | Customer max checked and all amounts fixed as integer atomic values | Local offers are synthetic model-derived offers, not marketplace prices. |
| Peer impersonation or plaintext interception | libp2p Noise authenticates the transport PeerId and encrypts the peer connection; GossipSub strict-signing authenticates public libp2p messages; AMN separately checks EIP-712 signer binding and peer-to-principal policy for private RPC | Peer keys do not prove personhood or independence. Identity resolvers/authorization are supplied by the application; no Sybil policy, DHT, relay, or NAT traversal is implemented. Noise cannot protect compromised endpoints or authorized recipients. |
| Missed, duplicate, or conflicting peer events | Signed public records are persisted locally, deduplicated by ID/body, and reconciled in bounded deterministic pages on peer connection | GossipSub is best effort and P3 has no global consensus log. Anti-entropy is eventual and currently runs on connection or explicit `syncPublicEvents`, not a continuous global repair service. |
| Private task broadcast | `CustomerTask` and unknown/private kinds are direct RPC only and are excluded from GossipSub and the public event store; Helia's separate private write path encrypts before storing | The P3 networking and P4 storage adapters are not yet wired into the customer/miner flow. Do not put plaintext task content in public records. |

## Cryptographic guarantees (narrow)

- SHA-256 content digests detect changes to the specific bytes/JSON that were hashed.
- Helia CIDs and SHA-256 detect a mismatch against the requested content identity; AES-GCM detects ciphertext/plaintext modification for a holder of the relevant key. X25519 envelope key agreement protects the data key only when recipient private keys remain secret and principal binding is correctly authorized.
- EIP-712 + ECDSA recovery verifies that the matching private key signed the typed payload under the configured domain.
- libp2p Noise authenticates the remote transport PeerId and encrypts bytes in transit on that connection; GossipSub strict-signing binds a public pubsub message to a PeerId key.
- Merkle inclusion verifies a supplied tuple's membership in a root under the specified leaf/node encoding.
- EVM storage, checked arithmetic, `SafeERC20`, reentrancy protection, quorum signatures, terminal task state, and decision hash prevent the tested replay/double-payout path for the deployed contract code.
- The local event hash chain identifies changed records when compared with a trusted retained chain head.

None of those primitives proves an AI model executed, an output is semantically correct, two keys belong to independent entities, a validator is honest, a benchmark is true, or an unselected output is correct. Noise transport authentication does not establish an AMN role unless the application's peer-to-principal and authorization policies are correct.

## Economic and probabilistic assumptions

Sampling is probabilistic and depends on unbiased future randomness, unmanipulated eligible identities, correctly committed input/output digests, and detectable sampled mistakes. Increasing the sample size increases detection probability but does not eliminate collusion. The report's formula does not model coordinated validators, correlated model errors, or a malicious verifier selection set.

No slashing mechanism is active. Reputation aggregates are separate: customer ratings affect only skill quality; objective execution outcomes affect miner reliability; later adjudication affects validator reliability; reproducibility affects benchmark-contributor credibility. Reputation values are not used in the MVP settlement path.

## Privacy and retention

Synthetic ground truth and PDFs remain local and are omitted from artifact listing. The example's public skill and benchmark record can be published because it contains no customer data. The Helia adapter can store private bytes only through its explicit encrypted `putPrivate` API; the existing customer task flow does not call it, and public OrbitDB is not integrated. Local job files are plaintext local-development data and require normal OS disk permissions/volume encryption. Helia grant revocations persist only on the local issuing store; remote peers do not receive revocation updates and an already authorized recipient can retain plaintext. Role signing keys persist as local development identities in `.amn/identities/*.key`; their key values are removed from persisted task records. Protect these plaintext keys and never reuse them for production.

## Production blockers

Before production: hardened per-job OS isolation (filesystem and outbound-network enforcement, kernel/VM CPU/RAM/GPU limits, and Ollama/model-process accounting), secure secret storage and recipient identity/key management, application/runtime integration for peer task messages and artifact transfer, public bootstrap/DHT interoperability, distributed grant revocation and retention policy, verified drand client, independently governed validator eligibility/Sybil policy, durable recovery beyond peer-connect anti-entropy, appeal/dispute resolution, an audited settlement/assignment contract, benchmark replication and poisoning defense, privacy review, and independent red-team/security review are mandatory work. See [production readiness](../production-readiness.md).
