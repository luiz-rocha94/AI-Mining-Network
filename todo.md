# TODO: Sequential implementation roadmap to production readiness

This file is the sequential feature plan from the current local MVP through a
production-readiness review. Each P step has its own implementation checklist
and a validation gate that must pass before the next step begins. The table
below records open-source project status and planned contribution; a listed
project is not considered integrated until its step's checks pass. The current
application is a local MVP, not a production decentralized network.

## Current implementation snapshot

| Project or standard | Status in this repository | Planned contribution to AMN | Roadmap step |
|---|---|---|---|
| [OpenZeppelin Merkle Tree](https://github.com/OpenZeppelin/merkle-tree) | Compatibility checked | Keep commitment roots and inclusion proofs interoperable between AMN implementations written in different languages. | P1 — Shared protocol vectors; existing compatibility is baseline |
| [Agent Skills](https://openagentskills.dev/docs/specification) | Partial | Provide portable, versioned skill packages that authors can publish, customers can discover and compose into workflows, and miners can interpret under explicit permissions. | P2 — Skills and tool execution |
| [Ollama](https://ollama.com/) | Integrated locally | Provide local model discovery and structured inference so customers and miners can use installed models without requiring a proprietary cloud inference backend. | P2 — Skills and tool execution; validate existing adapter |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | Adapter code present; optional | Expose approved skill tools through a standard agent-tool protocol, with AMN enforcing schemas, permissions, timeouts, and audit records. | P2 — Skills and tool execution |
| [libp2p](https://libp2p.io/) | Not integrated | Let peers discover each other and exchange signed tasks, offers, commitments, assignments, receipts, and decisions without a required central coordinator. | P3 — Peer discovery and task messaging |
| [Helia / IPFS](https://github.com/ipfs/helia) | Not integrated | Store and retrieve public skills and benchmark artifacts by content hash, plus explicitly authorized encrypted task artifacts. | P4 — Content-addressed artifact storage |
| [OrbitDB](https://github.com/orbitdb/orbitdb) | Not integrated | Replicate public skill metadata, benchmark submissions, ratings, and selected audit records across peers with deterministic conflict rules. | P5 — Replicated public registry (after P3–P4) |
| Benchmark and quote pipeline (AMN feature; no new dependency) | Partial/local | Collect reproducible benchmark evidence and calculate fixed quotes using cost components, policy, and compatible offers. | P6 — Benchmark evidence and fixed quotes |
| [Golem Yagna](https://docs.golem.network/) | Not integrated | Connect miners to decentralized compute offers and task execution; feed compatible live offers into fixed quotes and execution assignments. | P7 — Decentralized compute backend |
| [drand](https://drand.love/) | Not integrated | Supply verified, unpredictable post-commit randomness for reproducible validator and sample assignment. | P8 — Verified randomness |
| [OpenZeppelin Contracts](https://github.com/OpenZeppelin/openzeppelin-contracts) | Primitive implemented; disconnected | Settle accepted quotes through an existing EVM asset using escrow, safe token transfers, signed decisions, refunds, and replay protection. | P9 — EVM escrow integration |
| [Ethereum Attestation Service (EAS)](https://docs.attest.org/) | Not integrated | Publish verifiable attestations about commitments, benchmark receipts, and decisions while keeping private task contents out of public records. | P10 — Public attestations and reputation |
| [scikit-learn](https://scikit-learn.org/) | Not used; deferred | Estimate runtime and cost from independently verified benchmark and offer history, with calibrated uncertainty and a hard cap at the customer's accepted quote. | P12 — Optional benchmark-driven estimates |

### Integration order at a glance

P0 establishes the current local MVP. P1 validates the protocol and the
existing OpenZeppelin Merkle compatibility. P2 completes Agent Skills/MCP
support and validates the existing Ollama adapter. The new integrations are
then sequenced by dependency: P3 **libp2p**, P4 **Helia/IPFS**, P5 **OrbitDB**,
P7 **Golem Yagna**, P8 **drand**, P9 **OpenZeppelin escrow**, and P10 **EAS**.
P6 is the AMN benchmark/quote feature between registry setup and live Golem
offers, not a third-party integration. P11 validates resilience across the
integrations, P12 optionally adds **scikit-learn** after enough reliable data
exists, and P13 is the production-readiness gate.

Complete each P in order. Do not start the next P until the current P's
validation checklist passes and its result is recorded.

For the detailed compatibility notes and license inventory, see
[`docs/architecture/integration-matrix.md`](docs/architecture/integration-matrix.md)
and [`docs/reports/dependency-licenses.md`](docs/reports/dependency-licenses.md).

## How to execute this roadmap

The P numbers are sequential implementation steps. Each step adds one user- or
operator-visible capability on top of the current local MVP. Finish its
implementation checklist, run every validation gate, record the command and
result, and only then start the next step. A checkbox is complete only when the
code, automated checks, and documentation agree; a mocked adapter does not
count as a completed real integration. Keep local adapters for offline tests,
but make production mode fail closed when a required production adapter is
missing.

For every external project, recheck official documentation, supported systems,
API versions, and licenses before integration; pin tested versions and update
the license report. Never put private inputs, prompts, ground truth, or outputs
on public storage without explicit customer authorization. A content hash,
Merkle proof, signature, model replay, benchmark, resource metric, or EAS record
alone is not proof of AI correctness. Do not introduce a token. Keep EVM work
on local test chains until the production review gates below pass.

## P0 — Reproducible local MVP baseline

**Implementation and automated validation:** PASS on 2026-10-23. The
maintainer validated P0 on 2026-10-23; P1 is now implemented and awaiting its
own review. See
[`docs/reports/p0-local-baseline.md`](docs/reports/p0-local-baseline.md) for
commands, test results, private artifact locations, and known follow-ups.

**Feature added:** a repeatable reference implementation that contributors can
run offline and use as the acceptance baseline for all later network adapters.

**Existing contribution:** the repository already has the workflow UI, skill
packages, Ollama adapter, synthetic invoice demo, quote approval, execution
commitment, local sample verification, signed receipts, reputation separation,
simulated settlement, and a separately tested local EVM escrow primitive.

- [x] Reconcile README claims, actual CLI/API behavior, protocol schemas, and
  tests; clearly mark every local or simulated component.
- [x] Ensure one documented install and test path works from a clean checkout
  without Ollama, network services, production RPC, or real funds.
- [x] Keep the 100-invoice synthetic corpus and ground truth reproducible and
  private to the local test workspace.
- [x] Record the baseline test and report outputs; record follow-up work for
  any mismatch rather than silently changing acceptance behavior.

**Validate before P1:**

- [x] Run `pnpm test:all` from a clean environment and save the result in the
  release notes/CI artifact.
- [x] Run the 100-invoice demo with 30 samples and three validators; confirm
  the output, commitment, assignments, receipts, decision, and settlement can
  be audited from the emitted records.
- [x] Run the fault scenarios for corruption, post-commit tampering, validator
  outage/disagreement, timeout, low customer rating, and repeated settlement.
- [x] Confirm the accepted quote remains the charge ceiling and that a low
  customer rating cannot change objective miner payment.

**Planned open-source contribution:** existing Ollama, Agent Skills, MCP,
OpenZeppelin Merkle Tree, and OpenZeppelin Contracts components remain easy to
exercise locally while adapters are built incrementally.

**P0 follow-up recorded for P2:** the installed `qwen3.5:0.8b` Ollama model
returned empty structured output during skill benchmarking and was rejected;
the documented `qwen3:4b-instruct` completed the demo. P2 must cover malformed
or empty model responses and preflight each model/skill pair. This does not
change the offline test gate, which runs with Ollama unavailable.

## P1 — Interoperable protocol identity and message safety

**Implementation and automated validation:** PASS on 2026-10-23. Maintainer
review is pending; P2 has not started. See
[`docs/reports/p1-protocol-interoperability.md`](docs/reports/p1-protocol-interoperability.md)
for evidence, reproduction commands, compatibility decisions, and limitations.

**Feature added:** independent implementations can exchange and verify AMN
messages without relying on one runtime's serialization or identity shortcuts.

- [x] Finalize versioned schemas and adapter contracts for compute, transport,
  public metadata, artifact storage, randomness, attestations, and settlement.
- [x] Specify canonical serialization, domain-separated hashes, signer identity
  binding, timestamps/expiry, replay windows, and protocol-version negotiation.
- [x] Publish Python/TypeScript golden vectors for canonical JSON, message
  hashes, Merkle leaves/roots, EIP-712 signatures, and invalid/replayed messages.
- [x] Define key creation, rotation, revocation, recovery, and development-key
  rejection in production configuration.

**Validate before P2:**

- [x] Python and TypeScript produce byte-identical canonical encodings and
  hashes for all shared vectors.
- [x] Both implementations reject altered payloads, wrong domains, expired
  messages, revoked signers, unknown versions, and replayed identifiers.
- [x] Restarting a peer does not reset replay protection or change its identity
  binding unexpectedly.

**Planned open-source contribution:** AMN's protocol schemas define how Agent
Skills, MCP tools, and OpenZeppelin-compatible signatures interoperate across
language and adapter boundaries.

## P2 — Validated skill packages and isolated workflow execution

**Feature added:** authors can publish reusable workflow nodes, and miners can
run supported skills with bounded permissions and resources.

- [ ] Check the current Agent Skills specification and add parser conformance
  tests; validate manifests, references, paths, package sizes, version hashes,
  and input/output schemas at publication and execution.
- [ ] Keep declarative skills as the default. If scripts are enabled, require
  an explicit signed package, declared tool permissions, and sandbox execution.
- [ ] Add an isolated worker boundary with CPU/RAM/VRAM quotas, filesystem
  scoping, network-egress policy, secret isolation, killable deadlines, and
  cleanup after success or failure.
- [ ] Generalize the current Ollama workflow and optional MCP SDK integration
  to versioned tools, structured errors, cancellation, deadlines, and bounded
  inputs/outputs. Keep Ollama local by default.
- [ ] Treat uploaded files and model-generated tool requests as untrusted;
  deny undeclared shell, filesystem, network, and credential access.

**Validate before P3:**

- [ ] Run supported declarative skills end-to-end and reject malformed or
  path-traversal packages before execution.
- [ ] Adversarial tests prove timeout termination, resource limits, network
  denial, secret/file isolation, prompt-injection containment, and cleanup.
- [ ] MCP tests cover malformed arguments, denied tools, cancellation, and
  worker failure; core tests still pass with the MCP extra absent.
- [ ] Document that the sandbox's actual isolation guarantees depend on its
  host OS/runtime and are not stronger than the tested boundary.

**Planned open-source contribution:** Agent Skills provides portable skill
format conventions; Ollama provides local model inference; the MCP Python SDK
provides interoperable tool calls. AMN supplies validation, permissions,
sandboxing, quotas, and audit events.

## P3 — Coordinator-free peer discovery and task messaging

**Feature added:** customers, miners, and validators can discover one another
and exchange signed public protocol messages over a peer mesh.

- [ ] Implement `PeerTransport` using a maintained libp2p implementation;
  support authenticated/encrypted connections, discovery, pub/sub, and
  request/response for offers, tasks, commitments, assignments, receipts, and
  decisions.
- [ ] Add expiry, rate and size limits, duplicate suppression, peer scoring,
  deterministic event ordering, and restart-safe replay protection.
- [ ] Define which messages are public and which require authenticated,
  encrypted, authorized delivery; do not broadcast private task content.
- [ ] Keep loopback/local transport as a test adapter and reject it when a
  deployment declares production peer networking required.

**Validate before P4:**

- [ ] Two- and multi-peer tests cover discovery, message validation, process
  restart, delayed/duplicate messages, partition, recovery, and rejoin.
- [ ] Peers converge on the same signed task and public event state without an
  always-on coordinator.
- [ ] Unauthorized peers cannot read private task messages or impersonate a
  registered signer; document remaining Sybil limits.

**Planned open-source contribution:** libp2p provides peer transport and
discovery. It does not provide AMN's application identity policy, Sybil
resistance, task correctness, authorization, or payment finality.

## P4 — Helia/IPFS content-addressed artifact storage

**Feature added:** independently operated nodes can publish and retrieve
versioned public skill packages and approved artifacts by content address.

- [ ] Implement persistent Helia/IPFS `ArtifactStore` with explicit pinning,
  retention, and availability status. Verify bytes against signed expected
  hashes/CIDs before parsing or execution.
- [ ] Publish skill packages, public benchmark artifacts, and explicitly
  approved public receipts; keep customer inputs and outputs local by default.
- [ ] For authorized private artifact exchange, encrypt before storage, grant
  decryption capability only to assigned parties, and define expiry/revocation.

**Validate before P5:**

- [ ] Two independent peers retrieve the same pinned artifact after restart;
  tests cover missing blocks, altered bytes, unavailable peers, and retention.
- [ ] Privacy tests verify that public records and logs contain no private
  prompt, source document, ground truth, or plaintext result.
- [ ] Document that content addressing gives integrity, not confidentiality,
  deletion, guaranteed persistence, or access control.

**Planned open-source contribution:** Helia/IPFS supplies content-addressed
storage and retrieval. It does not provide confidentiality, guaranteed
persistence, authorization, or deletion from every replica.

## P5 — OrbitDB replicated public skill registry

**Feature added:** public skill and benchmark metadata becomes discoverable and
eventually consistent across independently operated peers.

- [ ] Implement OrbitDB `PublicMetadataStore` on the P3 libp2p and P4 Helia
  adapters; replicate signed immutable skill versions, benchmark submissions,
  ratings, revocations, and authorized audit checkpoints.
- [ ] Specify deterministic conflict, duplicate, revocation, and tombstone
  handling. Derive search indexes from replicated records; do not treat one
  writable index as authoritative state.
- [ ] Keep private task state and private artifacts outside public OrbitDB
  databases. Include encrypted references only when customer-authorized.

**Validate before P6:**

- [ ] Two independent OrbitDB peers converge after concurrent publication,
  partition, restart, and rejoin; forged and unauthorized records are rejected.
- [ ] Duplicate, revocation, and concurrent-version conflicts produce the
  same deterministic result on every peer.
- [ ] Privacy tests find no private prompt, source document, ground truth, or
  plaintext result in public records.
- [ ] Document eventual consistency and the fact that OrbitDB does not decide
  which conflicting signed claim is truthful.

**Planned open-source contribution:** OrbitDB supplies eventually consistent
replicated metadata on top of the peer/storage layers. AMN defines its signed
record format, authorization, conflict rules, and identity validation.

## P6 — Attestable benchmarks and offer-based fixed quotes

**Feature added:** skill contributors can submit reproducible performance
evidence, and customers receive a fixed quote grounded in compatible capacity.

- [ ] Extend `SkillBenchmark` receipts with skill/model/runtime digests,
  hardware class, warm/cold policy, sample count, variance, output validation,
  and measurement provenance. Label self-reported metrics as such.
- [ ] Add independent benchmark submission and configurable aggregation; emit
  P50/P90 only above minimum sample and quality thresholds.
- [ ] Price execution, verification, protocol, storage, and settlement costs
  with integer-safe arithmetic. Include assumptions, currency, expiry, failure
  and refund rules in the quote.
- [ ] Define the compatible-offer input contract and test pricing with clearly
  labeled fixtures/local offers. P7 supplies the real Golem offers; never label
  a fixture price as a live market quote.
- [ ] Fix the accepted quote ceiling; re-quote if an offer expires or changes
  and require customer approval for any new price.
- [ ] Keep learned pricing out of this feature. The optional scikit-learn
  estimator is P12 and requires enough independently verifiable observations.

**Validate before P7:**

- [ ] Reproduce benchmark receipts on independent workers and reject wrong
  digests, malformed evidence, and unqualified P50/P90 claims.
- [ ] Test quote calculations at boundaries, overflow cases, no-offer cases,
  expired offers, verification budget changes, and protocol-fee changes.
- [ ] Prove no execution or settlement path can charge above an accepted quote.
- [ ] Compare formula estimates with held-out benchmark/offer fixtures and
  report error and assumptions. P7 must additionally validate fixed quotes
  against actual Golem offers before that integration is accepted.

**Planned open-source contribution:** this AMN feature turns benchmark
submissions into traceable skill/runtime comparisons. The optional
scikit-learn cost/latency model belongs to P12; neither benchmarks nor models
prove that inference occurred or that an answer is factually correct.

## P7 — Golem Yagna decentralized compute backend

**Feature added:** tasks can be dispatched to a real provider marketplace
through the provider-neutral `ComputeBackend` contract.

- [ ] Implement Golem Yagna requestor integration only against its current
  documented APIs; map capabilities, offers, execution lifecycle, artifacts,
  timeout, cancellation, and provider failure into AMN states.
- [ ] Validate supported OS, architecture, runtime/image, KVM, GPU availability,
  account/payment model, and provider restrictions on a supported Linux setup.
  Do not claim Windows provider or arbitrary consumer GPU support.
- [ ] Bind selected offer, cost, runtime/image digest, resource limits, and
  deadline into the signed execution contract before dispatch.
- [ ] Retain a local fake backend for deterministic offline and fault tests;
  production mode must report unavailable instead of silently falling back.

**Validate before P8:**

- [ ] Contract tests run against the local fake; integration tests run against
  a real local Yagna requestor/provider on a documented supported host.
- [ ] Demonstrate offer discovery, successful execution, result retrieval,
  provider disappearance, cost/offer change, cancellation, timeout, and retry.
- [ ] Reconcile Yagna cost with the accepted AMN quote and test idempotent
  recovery from duplicate or delayed lifecycle events.
- [ ] Record exact versions, OS prerequisites, limitations, and reproduced
  commands; no integration is marked complete from mocks alone.

**Planned open-source contribution:** Golem Yagna supplies decentralized
compute offers and provider execution where its documented runtime and
hardware support the task. AMN retains quoting, verification, privacy, and
settlement policy.

## P8 — drand verified randomness and independent validator assignment

**Feature added:** a production task commitment can trigger unpredictable,
auditable sample and validator assignments after the executor commits.

- [ ] Implement `RandomnessSource` with a maintained drand client or the
  documented verification protocol; check chain identity, round, signature,
  and chaining proof.
- [ ] Bind the future round and eligible validator set before output commitment
  or randomness reveal. Exclude the executor and declared conflicts.
- [ ] Derive sample indices and validator assignments deterministically from
  task ID, output root, verified round, eligible-set snapshot, and domain
  separator; include proof and selection inputs in the audit record.
- [ ] Define validator eligibility and Sybil-resistance assumptions, minimum
  stake/identity policy (if any), availability replacement, sampling rate,
  quorum, escalation, timeout, and dispute windows.
- [ ] Give each validator only its assigned units and necessary evidence.
  Preserve pass/fail/inconclusive/timeout/dispute outcomes and signed receipts.

**Validate before P9:**

- [ ] Known vectors and adversarial tests reject invalid signatures, wrong
  chain/round, replay, altered roots, executor selection, and late commit.
- [ ] Simulations reproduce assignments and show sampling detection
  probabilities across fraud rates; report that detection is probabilistic.
- [ ] Tests cover unavailable validators, duplicate identities, conflicts,
  colluding quorums, disagreement, and escalation without a silent pass.
- [ ] Confirm Merkle inclusion proves membership only; deterministic replay or
  validator signatures do not by themselves establish AI factual correctness.

**Planned open-source contribution:** drand supplies verifiable public
randomness. AMN must still establish eligible-set integrity, independent
identities, task-specific evidence, and collusion assumptions.

## P9 — OpenZeppelin EVM escrow and settlement integration

**Feature added:** verified task decisions can settle an existing supported
asset through an auditable EVM escrow rather than the local Python ledger.

- [ ] Implement `EvmSettlementBackend` against the existing OpenZeppelin-based
  escrow contract. Bind funding, immutable quote, executor/verifier rewards,
  verified decision evidence, deadlines, refunds, and dispute paths.
- [ ] Verify signer, task, domain, assignment, quorum, and replay protection
  before settlement calls; retain transaction hashes, confirmations, and
  idempotency keys for recovery.
- [ ] Handle dropped/replaced transactions, RPC outage, duplicate requests,
  reorgs, deadline boundaries, token decimals/transfers, fee arithmetic, and
  verifier shortfall.
- [ ] Keep simulated settlement for offline tests and fail closed if a
  production profile is configured without the required chain/asset backend.
- [ ] Use only an existing supported asset; do not issue a token. Require
  independent contract review before real-value deployment.

**Validate before P10:**

- [ ] Python-to-local-EVM tests cover the complete state machine, successful
  payment, refund, dispute, timeout, invalid signatures, replay, and duplicate
  settlement request.
- [ ] Repeated settlement is idempotent and cannot pay twice; hostile token
  and arithmetic boundary tests pass.
- [ ] A low customer rating after valid execution changes skill reputation
  only and cannot alter a settled or eligible miner reward.
- [ ] Document chain, asset, confirmation depth, gas, keys, dispute authority,
  and upgrade policy; no real funds are used in CI.

**Planned open-source contribution:** OpenZeppelin Contracts supplies reusable
EVM security primitives. AMN's contract and policy must still validate the
protocol's evidence and define correct settlement conditions.

## P10 — Ethereum Attestation Service and separated reputation

**Feature added:** peers can independently inspect signed public benchmark,
execution, verification, and reputation claims without exposing task content.

- [ ] Define versioned EAS schemas and choose on-chain/off-chain mode for
  benchmark receipts, commitments, verification decisions, and settlement
  references.
- [ ] Implement `AttestationPublisher`; publish only authorized digests,
  signer/schema/chain identity, and references—not prompts, private inputs,
  ground truth, or plaintext output.
- [ ] Verify schema, chain ID, signer, digest, revocation, expiry, and replay
  before an attestation affects discovery or reputation.
- [ ] Keep skill quality, miner reliability, validator reliability, and
  benchmark contributor credibility separate. Base each dimension on its
  declared signed evidence; keep customer rating payment-neutral.
- [ ] Add basic duplicate/rating abuse controls and document their Sybil and
  collusion limitations.

**Validate before P11:**

- [ ] Tests reject wrong schema, changed digest, revoked/expired claims,
  duplicate publication, private-data leakage, and unavailable EAS service.
- [ ] Independent peers verify published records and compute the same
  evidence-based reputation from the same public inputs.
- [ ] The local signed-receipt path still works when EAS is disabled; no EAS
  attestation is described as a correctness proof.

**Planned open-source contribution:** EAS provides an interoperable attestation
format and verification flow. AMN defines the meaning, privacy boundary, and
reputation effect of each claim.

## P11 — Network resilience, privacy, and adversarial recovery

**Feature added:** the multi-peer network recovers predictably from failures
and protects task data at every transfer and storage boundary.

- [ ] Test partition/rejoin, delayed and reordered events, duplicate delivery,
  peer churn, storage loss, beacon outage, verifier shortfall, provider failure,
  RPC failure, and conflicting signed records.
- [ ] Add bounded retries, deadlines, durable queues/checkpoints, recovery
  procedures, backup/restore, data expiry, and explicit refund/dispute outcomes.
- [ ] Exercise malicious skills, compromised miners, validator collusion,
  selective disclosure, benchmark poisoning, replay, spam, and identity
  duplication against the threat model.
- [ ] Complete privacy review for encrypted artifacts, access grants, logs,
  retention/deletion, and customer authorization. Add incident reporting,
  abuse response, and key revocation runbooks.
- [ ] Ensure failures never cause unbounded charges, silent centralized
  fallback, unauthorized data replication, or automatic miner punishment from
  subjective customer feedback.

**Validate before P12:**

- [ ] Fault-injection suites reach a documented terminal or recoverable state
  for every failure above and preserve idempotent payment behavior.
- [ ] Independent security tests reproduce documented protections and clearly
  record unresolved economic/trust assumptions.
- [ ] Restore a peer from backup and reconcile its signed state after a
  simulated network partition without losing audit history.

**Planned open-source contribution:** P11 validates the integrations introduced
in P3–P10 (libp2p, Helia/IPFS, OrbitDB, Golem, drand, OpenZeppelin, and EAS)
under failures and adversarial conditions. Those libraries provide building
blocks; resilience, privacy, and conflict resolution remain AMN features.

## P12 — Scale, cost, and optional scikit-learn estimates

**Feature added:** measured operating limits and quote accuracy for increasing
task sizes and network participation.

- [ ] Benchmark full task execution separately from protocol mechanics;
  measure verification cost, network/storage overhead, quote error, settlement
  latency, and fraud detection probability.
- [ ] Run reproducible workloads across increasing miner/validator counts,
  input sizes, skill/model versions, and failure rates; publish hardware,
  versions, raw data, scripts, and report.
- [ ] Set documented resource and throughput limits from measurements; test
  backpressure and overload behavior.
- [ ] Train/evaluate an optional scikit-learn estimate only when sufficient
  qualified benchmark/offer/settlement data exists; use time-based holdouts,
  calibration/drift gates, and an explicit uncertainty range.

**Validate before P13:**

- [ ] Another contributor can reproduce the report from the pinned environment
  and scripts.
- [ ] Quotes remain within the authorized ceiling at tested load; latency,
  cost, and overload behavior meet published targets or are documented as
  blockers.
- [ ] The fraud-detection curve includes sampling assumptions and is not
  presented as an absolute guarantee.

**Planned open-source contribution:** scikit-learn can provide a versioned,
reproducible cost/latency estimator after enough quality observations exist;
the deterministic quote ceiling remains authoritative.

## P13 — Production readiness review and staged deployment

**Feature added:** a reviewed release candidate with operators able to deploy,
monitor, upgrade, and recover the network without relying on development
shortcuts.

- [ ] Complete independent reviews of contracts, protocol signatures,
  randomness/assignment, sandbox, networking, privacy, and economic attacks.
- [ ] Deploy a public test network using production adapters and non-value test
  assets; exercise the complete customer-to-settlement flow with independent
  operators and no mandatory coordinator.
- [ ] Define governance and release process for protocol/schema upgrades,
  contract upgrades, adapter compatibility, emergency pause, and recovery.
- [ ] Add monitoring/alerts, secure operator key custody, backups, data
  retention/deletion, incident response, dependency/license scans, and
  reproducible release builds.
- [ ] Update installation, API/protocol schemas, threat model, integration
  matrix, license report, performance report, and production runbooks.
- [ ] Review the production-readiness checklist. Any unresolved high-severity
  finding or simulated mandatory component blocks real-user-data/real-fund
  launch.

**Validate before production:**

- [ ] Clean-install and upgrade tests pass for supported operator platforms.
- [ ] Independent operators reproduce task assignment, verification, and
  settlement evidence and can recover from a peer/network outage.
- [ ] Security reviewers sign off or document accepted residual risks and
  accountable owners; no unsupported OS/GPU/network claims remain.
- [ ] Launch decision, supported configuration, asset, limits, incident
  contacts, and rollback procedure are published before enabling real tasks.

**Planned open-source contribution:** verified integrations with the listed
projects are packaged as replaceable adapters; AMN's own protocol, marketplace
policy, and operational documentation remain openly inspectable.

## Useful local references

- [`docs/architecture/integration-matrix.md`](docs/architecture/integration-matrix.md)
- [`docs/architecture/architecture.md`](docs/architecture/architecture.md)
- [`docs/security/threat-model.md`](docs/security/threat-model.md)
- [`docs/production-readiness.md`](docs/production-readiness.md)
- [`docs/reports/dependency-licenses.md`](docs/reports/dependency-licenses.md)
