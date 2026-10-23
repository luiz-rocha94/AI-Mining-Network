# P6 benchmark evidence and fixed quotes

P6 implements the local benchmark/quote feature between public registry setup
and live provider-market integration. It adds no new third-party dependency.
The v2 message schema is in
[`messages.schema.json`](../../packages/protocol/v2/messages.schema.json).

## Benchmark receipt

`amn benchmark --samples 20` runs the versioned
`invoice-synthetic-v2` workload against a selected skill and model. Fewer than
20 samples are rejected because the published P50/P90 profile has a minimum
sample requirement. The receipt includes:

- skill package, synthetic dataset, model, and software runtime SHA-256 digests;
- a declared CPU hardware class, warm-up policy, unique measurement ID, and
  environment fingerprint;
- the per-sample duration array, population variance rounded to integer ms²,
  per-sample output-validation booleans, input/output token counts, and
  recomputable nearest-rank P50/P90 values;
- per-sample benchmark-process CPU and RSS observations where `psutil` can
  measure them. GPU/VRAM and the separate Ollama inference daemon remain
  unobserved by this adapter and are `null` rather than reported as zero;
- collector version and explicit limitations. Current receipts are always
  `self-reported`.

The benchmark service makes one warm-up call, then measures the declared sample
set. The summary fields are checked against their signed observation arrays
when the receipt is parsed and again before aggregation. A receipt ID hashes its
content, and its envelope signature authenticates the contributor key. Neither
proves the inference ran, hardware descriptions are true, or outputs are
factually correct.

## Independent submissions and aggregation

Each worker writes its own signed `benchmark-receipt.json`. Share those files
with other contributors and run:

```powershell
amn benchmark-aggregate .amn/worker-a/benchmark-receipt.json .amn/worker-b/benchmark-receipt.json .amn/worker-c/benchmark-receipt.json --minimum-contributors 3 --minimum-total-samples 60 --output .amn/aggregate.json
```

The configurable policy defaults to three distinct signer keys, at least 20
samples per receipt, at least 60 pooled observations, and at least 95% output
validation success. Aggregation requires the same skill, dataset, model,
software runtime, hardware class, benchmark version, and warm/cold policy. It
checks EIP-712 signatures, unique signers, receipt content hashes, all sample
counts, P50/P90, variance, and validation arithmetic. A missing or mismatched
digest, malformed sample set, duplicate key, or failed quality threshold
rejects the aggregate instead of silently dropping the claim.

Distinct signing keys do not establish distinct operators. There is no
stake/personhood rule, hardware attestation, benchmark anti-Sybil authority,
or independently observed execution in this step. The aggregate output
explicitly carries that trust note. P5 OrbitDB can replicate signed public
benchmark claims, but P6 does not automatically publish customer data or wire
the benchmark flow to OrbitDB.

## Offer compatibility and fixed pricing

`fixed_quote` receives explicit executor and validator offers. It selects the
lowest-priced unexpired offers matching the requested currency and skill,
distinct executor/validator identities, and optional declared CPU/RAM/VRAM,
model digest, runtime digest, and validator strategy constraints. The offer
capability statements are signed claims, not trusted hardware measurements.
P7 will adapt real Yagna offers into this contract.

The quote binds a hash of the ordered selected offers and breaks out execution,
verification, protocol, storage, and settlement amounts. It also includes
currency, expiry, assumptions, verification policy, and failure/refund rules.
The local invoice and document flows use `local-fixture` offers and label them
in both the quote and customer UI; they are not live market quotes. Local
protocol, storage, and settlement costs are explicitly zero because this
development composition uses no paid protocol service, metered storage provider,
or chain transaction. General document workflows disclose that their per-skill
rates are a local text/model-size estimate, not an independently benchmarked
market price.

Protocol v2 atomic amounts are bounded by JavaScript's maximum safe integer
(`2^53 - 1`) so JSON consumers retain exact arithmetic. Adding the five quote
components uses checked integer arithmetic. The customer task supplies the
maximum; a quote above it is rejected. At acceptance AMN re-verifies the signed
quote, the task binding and ceiling, all selected offer signatures, their
expiry and compatibility, offer amounts, and the offer snapshot hash. A stale
or changed offer requires a newly signed quote and another customer acceptance.
Before local settlement, payout totals and per-purpose amounts are checked
against the accepted quote, and the complete funded total must be paid or
refunded. Subjective ratings are not an input.

The local P6 fixture formula is intentionally simple and is only a reproducible
test baseline. Held-out synthetic offer fixture error and all assumptions are
in [`p6-benchmark-quotes.md`](../reports/p6-benchmark-quotes.md); regenerate it
with `python scripts/p6_pricing_report.py`. The fixture set cannot predict real
Golem prices. P7 must compare the estimator against actual compatible provider
offers before any quote is described as live or market-grounded.

## Validation boundary

The test suite reproduces receipt signing/aggregation with three independent
test keys and a deterministic extractor, and rejects signature, digest,
profile, resource, expiry, overflow, and quote-mutation cases. These tests
validate protocol arithmetic and policy; they do not prove the test signers are
independent operators or that a production provider will honor an offer.
