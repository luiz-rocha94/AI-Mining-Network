"""Regenerate the clearly non-live P6 held-out offer fixture report."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from ai_mining_network.pricing import local_fixture_offer_prices

# Held-out values are synthetic offers used only to estimate local formula error.
# They are not Golem data and must never be described as a live market sample.
HELD_OUT_FIXTURES = (
    {"name": "small-fast", "p90_ms": 80, "units": 10, "samples": 3, "validators": 2, "execution": 110_000, "validator_each": 22_000},
    {"name": "medium-fast", "p90_ms": 600, "units": 50, "samples": 10, "validators": 2, "execution": 114_000, "validator_each": 22_000},
    {"name": "large-steady", "p90_ms": 1_200, "units": 100, "samples": 30, "validators": 3, "execution": 450_000, "validator_each": 50_000},
    {"name": "large-slow", "p90_ms": 4_500, "units": 100, "samples": 30, "validators": 3, "execution": 1_650_000, "validator_each": 165_000},
)


def build_report() -> str:
    rows = []
    task_errors = []
    for fixture in HELD_OUT_FIXTURES:
        execution_estimate, validator_estimate = local_fixture_offer_prices(
            fixture["p90_ms"],
            fixture["units"],
            fixture["samples"],
            fixture["validators"],
        )
        estimate_total = execution_estimate + validator_estimate * fixture["validators"]
        fixture_total = fixture["execution"] + fixture["validator_each"] * fixture["validators"]
        relative_error_pct = (estimate_total - fixture_total) * 100 / fixture_total
        task_errors.append(abs(relative_error_pct))
        rows.append(
            "| {name} | {p90_ms:,} | {units:,} | {samples:,}/{validators} | "
            "{estimate_total:,} | {fixture_total:,} | {error:+.1f}% |".format(
                name=fixture["name"],
                p90_ms=fixture["p90_ms"],
                units=fixture["units"],
                samples=fixture["samples"],
                validators=fixture["validators"],
                estimate_total=estimate_total,
                fixture_total=fixture_total,
                error=relative_error_pct,
            )
        )
    mean_absolute_error = sum(task_errors) / len(task_errors)
    return f"""# P6 benchmark and fixed-quote report

Generated: {date.today().isoformat()}

## Held-out fixture comparison

The P6 offline offer formula takes a skill benchmark P90 and task sizes. It
estimates executor price as `max(100000, P90_ms * units * 4)` atomic units and
each validator price as `max(20000, P90_ms * ceil(samples / validators) * 4)`.
Rows compare that formula against a small hand-authored held-out fixture set.

| Fixture | P90 ms | Units | Samples / validators | Formula total (atomic) | Fixture total (atomic) | Relative error |
|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

Mean absolute percentage error on these four fixtures: **{mean_absolute_error:.1f}%**.

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
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/reports/p6-benchmark-quotes.md"),
    )
    args = parser.parse_args()
    report = build_report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
