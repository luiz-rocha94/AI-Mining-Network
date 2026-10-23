# P6 benchmark and fixed-quote report

Generated: 2026-10-23

## Held-out fixture comparison

The P6 offline offer formula takes a skill benchmark P90 and task sizes. It
estimates executor price as `max(100000, P90_ms * units * 4)` atomic units and
each validator price as `max(20000, P90_ms * ceil(samples / validators) * 4)`.
Rows compare that formula against a small hand-authored held-out fixture set.

| Fixture | P90 ms | Units | Samples / validators | Formula total (atomic) | Fixture total (atomic) | Relative error |
|---|---:|---:|---:|---:|---:|---:|
| small-fast | 80 | 10 | 3/2 | 140,000 | 154,000 | -9.1% |
| medium-fast | 600 | 50 | 10/2 | 160,000 | 158,000 | +1.3% |
| large-steady | 1,200 | 100 | 30/3 | 624,000 | 600,000 | +4.0% |
| large-slow | 4,500 | 100 | 30/3 | 2,340,000 | 2,145,000 | +9.1% |

Mean absolute percentage error on these four fixtures: **5.9%**.

## Assumptions and limits

- These held-out prices are synthetic fixtures, not peer observations, Golem
  offers, production currency prices, or evidence of market quote accuracy.
- The P90 input comes from signed benchmark receipts. Signatures bind claims;
  they do not prove a worker ran inference or measured the claimed hardware.
- Current local execution, verification, storage, and SQLite settlement offers
  are labeled `local-fixture`. Storage, settlement, and protocol fees are zero
  in this local rate card because no metered provider or chain is involved.
- Quotes select only unexpired offers matching task skill, currency, declared
  resource requirements, model/runtime digests where requested, and validator
  strategy. P7 must measure forecast error against real provider offers before
  a live-market quote can be claimed.
- All v2 atomic fields are bounded by `2^53 - 1` so JavaScript and Python
  consumers preserve exact integer arithmetic. This is a protocol JSON limit,
  not an EVM uint256 limit.
- The customer's accepted signed quote is revalidated at acceptance and before
  settlement. Offer expiry or changed terms requires a new quote and explicit
  customer approval.
