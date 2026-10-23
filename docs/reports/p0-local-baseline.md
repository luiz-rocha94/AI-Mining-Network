# P0 Local MVP Baseline Report

**Date:** 2026-10-23
**Source revision:** `c8bb475` plus the P0 README and pnpm workspace fixes
**Result:** implementation and automated validation pass; maintainer review is
pending before P1 begins.

## Reproducible setup and offline tests

Environment used: Windows PowerShell, Python 3.12.14, Node.js v24.22.1, pnpm
11.25.0. A clean source copy was assembled from tracked files without a `.venv`,
`node_modules`, `.amn` task artifacts, or Ollama access. A new Python virtual
environment was created and `pip install -e '.[dev]'` completed. The Python
packages were available from the local pip cache; pnpm installed its locked
dependencies with `pnpm install --offline --frozen-lockfile` using the local
pnpm store (305 packages reused, none downloaded).

With Ollama deliberately pointed at an unreachable local endpoint, the
following command passed from the clean source copy:

```powershell
$env:OLLAMA_HOST = 'http://127.0.0.1:1'
$env:AMN_OLLAMA_MODEL = 'offline-test'
pnpm test:all
```

Results: 4 customer-web tests passed, 4 local EVM contract tests passed, and 39
Python tests passed (47 total). The Python suite includes the invoice workflow,
corrupted results, post-commit mutation, unavailable validators, validator
disagreement, timeout, rating/payment separation, quote ceilings, and
settlement idempotency. The loopback-origin API test required local socket
permission; it passed in the approved run.

The workspace package build policy now explicitly allows the `esbuild`
postinstall binary check and `keccak` install hook. These are needed by the
web and local EVM test dependencies; `keccak` retains its documented
JavaScript fallback when its native build is unavailable.

## 100-invoice end-to-end execution

The local CLI generated the synthetic invoices and ground truth in the ignored
`.amn` workspace, then ran the invoice skill with the installed local Ollama
model `qwen3:4b-instruct`:

```powershell
\.venv\Scripts\python.exe -m ai_mining_network.cli demo --root . --model qwen3:4b-instruct --count 100 --samples 30 --validators 3 --accept --wait-seconds 7200
\.venv\Scripts\python.exe -m ai_mining_network.validator_cli audit 870c0348-618c-41fc-b5a2-7689738d24e3 --root .
```

The task reached `SETTLED` with 100 inputs and 100 committed units. The output
Merkle root was
`0xc52b97eb7384ce342a907843db6886e5329f40bef16246d5d9d1cce9c6d76b7f`.
After commitment, the local development randomness adapter selected 30
distinct units and assigned 10 to each of three local validator workers. All
30 receipts validated; the decision and simulated settlement records are
auditable. The charge was 300560 `TESTUSD` atomic units, equal to the accepted
quote total. Repeating settlement was idempotent, and the audit reports
`ratingPaymentIndependent=true`.

The audit reports `randomness.verified=false`. This is a local development
beacon, not a verified drand integration. Validators are local development
workers rather than independently operated network peers, and `TESTUSD` is a
simulated ledger unit rather than a real asset. The result demonstrates the
local workflow, not decentralized production security.

Private run evidence remains under the git-ignored `.amn/p0-validation/` and
`.amn/jobs/` paths. The run log is `invoice-100-qwen3-4b.log`, the machine
audit is `invoice-100-audit.json`, and the fraud simulation is
`fraud-report.json`. Inputs and ground truth are not committed or published.

## Fault and payment checks

All listed cases passed through the full `pnpm test:all` suite:

- Corrupted invoice fields fail verification and refund the execution reward.
- Post-commit mutation is detected and cannot replace the committed result.
- Validator unavailability produces an inconclusive result and refund.
- Conflicting validators leave the task disputed with funds held.
- Execution timeout refunds without a commitment.
- A low customer rating does not change an otherwise valid miner payment.
- Retried settlement cannot pay twice.
- A charge cannot exceed the customer's accepted fixed quote.

## Sampling detection simulation

The reproducible command uses uniform sampling without replacement over 100
units with 5 corrupted units:

```powershell
\.venv\Scripts\python.exe -m ai_mining_network.cli fraud-report --units 100 --corrupt 5 --samples 5 10 20 30 50
```

| Samples checked | Probability of selecting at least one of 5 corrupt units |
|---:|---:|
| 5 | 23.04% |
| 10 | 41.62% |
| 20 | 68.07% |
| 30 | 83.92% |
| 50 | 97.19% |

This is probabilistic detection, not a guarantee. It assumes uniformly random
selection and does not protect against correlated or colluding validators,
incorrect ground truth, or a flawed verification procedure.

## Follow-up and current limits

The installed Ollama model `qwen3.5:0.8b` returned empty structured output
during the initial benchmark attempt; the CLI rejected it before task creation.
The `qwen3:4b-instruct` run above succeeded. P2 records the follow-up to test
empty/malformed model output and preflight model/skill compatibility.

The following remain simulated or unintegrated and block production readiness:
verified drand randomness, independently operated validators, decentralized
peer transport, persistent IPFS/OrbitDB storage, Golem provider execution,
EAS publication, and real EVM escrow integration. The separate local EVM
contract tests do not mean the end-to-end Python demo uses on-chain escrow.
