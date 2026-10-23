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
| Prompt injection in invoice text | System prompt treats document text as untrusted; strict JSON schema; no user-supplied tools exposed to the model | Prompting is not a security boundary; model extraction can still be wrong. |
| Malicious downloaded skill | MVP does not execute downloaded code; SKILL.md is data, fixed Python runtime performs work | Third-party runtime scripts/tools are not yet sandboxed; do not execute them in this MVP. |
| Local browser DNS rebinding | API loopback bind, loopback Host/Origin allowlist, request-size limit | No authentication; assumes one trusted local OS user. Do not expose the port on a LAN. |
| Private input replication | Task inputs stay under ignored `.amn/jobs`; artifacts API filters private input; no public IPFS/OrbitDB integration | Local disk data is not encrypted, and same-user processes can read it. |
| Private input goes to remote inference | Ollama defaults to localhost; non-local endpoint requires `AMN_ALLOW_REMOTE_OLLAMA=1` | Explicit opt-in can disclose text. User must trust the remote endpoint. |
| Benchmark poisoning | Signed contributor receipt and private synthetic ground truth for local benchmark | All measurements are self-reported; no independent hardware attestation, duplicate-run policy, or production aggregate exists. |
| Settlement replay/double pay | Python SQLite idempotency key; EVM task replay state, decision hash, exact integer amounts | Python settlement is not a distributed consensus system; Solidity contract is not connected to the demo. |
| Audit-log rewriting | Per-task event hash chain detects internal edits when head hash is retained | SQLite owner can rewrite the entire chain; no external checkpoint/attestation in the MVP. |
| Quote manipulation | Customer max checked and all amounts fixed as integer atomic values | Local offers are synthetic model-derived offers, not marketplace prices. |

## Cryptographic guarantees (narrow)

- SHA-256 content digests detect changes to the specific bytes/JSON that were hashed.
- EIP-712 + ECDSA recovery verifies that the matching private key signed the typed payload under the configured domain.
- Merkle inclusion verifies a supplied tuple's membership in a root under the specified leaf/node encoding.
- EVM storage, checked arithmetic, `SafeERC20`, reentrancy protection, quorum signatures, terminal task state, and decision hash prevent the tested replay/double-payout path for the deployed contract code.
- The local event hash chain identifies changed records when compared with a trusted retained chain head.

None of those primitives proves an AI model executed, an output is semantically correct, two keys belong to independent entities, a validator is honest, a benchmark is true, or an unselected output is correct.

## Economic and probabilistic assumptions

Sampling is probabilistic and depends on unbiased future randomness, unmanipulated eligible identities, correctly committed input/output digests, and detectable sampled mistakes. Increasing the sample size increases detection probability but does not eliminate collusion. The report's formula does not model coordinated validators, correlated model errors, or a malicious verifier selection set.

No slashing mechanism is active. Reputation aggregates are separate: customer ratings affect only skill quality; objective execution outcomes affect miner reliability; later adjudication affects validator reliability; reproducibility affects benchmark-contributor credibility. Reputation values are not used in the MVP settlement path.

## Privacy and retention

Synthetic ground truth and PDFs remain local and are omitted from artifact listing. The example's public skill and benchmark record can be published because it contains no customer data. There is no upload of private data to public IPFS/OrbitDB. Private files are plaintext local-development data and require normal OS disk permissions/volume encryption. Role signing keys persist as local development identities in `.amn/identities/*.key`; their key values are removed from persisted task records. Protect these plaintext keys and never reuse them for production.

## Production blockers

Before production: isolated per-job execution, secure secret storage, encrypted authenticated artifact transport, verified drand client, independently governed validator eligibility/Sybil policy, distributed challenge/event convergence, appeal/dispute resolution, an audited settlement/assignment contract, benchmark replication and poisoning defense, privacy retention controls, and independent red-team/security review are mandatory work. See [production readiness](../production-readiness.md).
