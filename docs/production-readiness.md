# Production readiness checklist

The MVP is **not production-ready**. Check each item with code, independent tests, and operator documentation before handling real user data or funds.

## Network and identity

- [ ] Deploy a libp2p peer mesh with authenticated transports, discovery, peer scoring, replay protection, signed message relays, and recovery after network partitions.
- [ ] Wire the tested P4 Helia storage adapter into skill publication, task artifacts, and peer discovery; configure and validate public bootstrap/DHT or another decentralized provider index, multi-operator availability, backup, and rejoin.
- [ ] Wire the tested P5 OrbitDB public metadata adapter into authoring, benchmark, and rating flows instead of the remaining SQLite indexes; add multi-operator discovery, spam resistance, key-history resolution, and operational recovery beyond the local two-peer tests.
- [ ] Define identity issuance, key rotation/revocation, hardware-backed or external signers, capability authenticity, and a documented Sybil-resistance policy.
- [ ] Ensure skill authors can publish to the public registry without a central administrator while metadata conflicts converge deterministically.

## Compute and execution

- [ ] Build supported, reproducible task images or another hardened isolated runtime; the local worker now accepts declarative skills only and does not run package scripts, but it runs as the miner's OS user.
- [ ] Add kernel/container/VM filesystem and network isolation, CPU/RAM/VRAM limits, syscall/resource controls, secret isolation, killable deadlines, Ollama/model-process accounting, and independent cleanup tests. Current worker CPU/RSS checks are monitored process-tree limits, not hard quotas; GPU/VRAM and the separate Ollama daemon are outside the limit.
- [ ] Integrate a compatible decentralized marketplace backend. For Golem, validate Linux x86-64 requestor/provider constraints, KVM and image/runtime requirements, GPU flags/provider availability, GLM accounting, and end-to-end task return behavior. Do not assume this Windows configuration is supported.
- [ ] Integrate P4 encrypted artifact bytes and grants with P3 authenticated transport and signed protocol envelopes; add recipient identity/key management, expiry, and distributed revocation semantics. Never publish customer PDFs, ground truth, prompts, or outputs publicly without explicit authorization.
- [ ] Add OCR only with a declared model/version, resource requirements, and verification plan. The current skill handles text PDFs only.

## Verification and reputation

- [ ] Implement a production drand client that selects a future round and cryptographically verifies chain hash, round, signature, and chaining before deriving assignments.
- [ ] Prove the eligible set was fixed independently of the executor before randomness reveal; define stake/identity, conflicts, quorum, slashing, and collusion assumptions.
- [ ] Define task-specific verification strategies, tolerance, evidence availability, dispute windows, audit escalation, and refund rules before quoting.
- [ ] Validate randomized assignment fairness and sampling probabilities under adversarial timing, selective retries, unavailable validators, and collusion.
- [ ] Wire P6 benchmark receipts and aggregates into the P5 OrbitDB flow; reproduce them across independent operators, define Sybil/attestation policy, verify model/runtime/hardware profiles, and publish only validated public benchmark metadata. Current receipts and hardware/resource claims are self-reported.
- [ ] Define public signed reputation schemas and anti-Sybil/rating abuse policies. Never feed subjective ratings into execution settlement.

## Payments and operations

- [ ] Audit the Solidity contract, assignment authority, typed-data domain, timeout/refund logic, reentrancy, token edge cases, fee arithmetic, and chain-specific behavior.
- [ ] Connect the policy engine to escrow only after independently verifying decision quorum and assignment/randomness evidence. Set and test dispute resolution and post-deadline refund behavior.
- [ ] Use an existing supported asset with exact decimals, integer-safe quote accounting, accepted currency/chain checks, gas accounting, and idempotent transaction tracking. No new token.
- [ ] Publish EAS schemas/attestations only after privacy review. Keep private evidence off-chain and reference encrypted content by authorized capability.
- [ ] Define monitoring, abuse response, incident handling, secure backups, data retention/deletion, upgrade control, dependency review, and independent operational key custody.
- [ ] Complete a third-party security review, threat-model exercise, benchmark reproduction, and production runbook before real-value tasks.

## Current checklist result

The local software tests also exercise Agent Skills package validation and hashing, declarative worker boundaries, process-tree deadline/CPU/RSS termination, MCP input validation and cancellation, Ollama tool allow-listing, P3 libp2p peer messaging, and P4 local Helia artifact storage. The standalone peer and storage adapters are not connected to the task runtime, and their tests do not satisfy public DHT/bootstrap, distributed storage/revocation, verified randomness, hardened OS sandbox, production signer, GPU accounting, or real market integration items above.
