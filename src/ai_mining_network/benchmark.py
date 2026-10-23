"""Small reproducible benchmark aggregation with explicit provenance."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class BenchmarkSummary:
    samples: int
    p50_duration_ms: int
    p90_duration_ms: int
    mean_duration_ms: int
    success_rate_bps: int
    input_tokens: int
    output_tokens: int
    self_reported_metrics: bool = True


def _percentile(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return int(ordered[index])


def summarize_runs(durations_ms: list[int], successful: list[bool], input_tokens: int = 0, output_tokens: int = 0) -> BenchmarkSummary:
    if not durations_ms or len(durations_ms) != len(successful):
        raise ValueError("durations and success flags must have equal non-zero lengths")
    if any(duration < 0 for duration in durations_ms):
        raise ValueError("durations cannot be negative")
    success_rate = round(10_000 * sum(successful) / len(successful))
    return BenchmarkSummary(
        samples=len(durations_ms),
        p50_duration_ms=_percentile(durations_ms, 0.50),
        p90_duration_ms=_percentile(durations_ms, 0.90),
        mean_duration_ms=int(statistics.mean(durations_ms)),
        success_rate_bps=success_rate,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def fraud_detection_probability(unit_count: int, corrupt_count: int, sample_count: int) -> float:
    """Exact probability for uniform sampling without replacement."""
    if not 0 <= corrupt_count <= unit_count:
        raise ValueError("corrupt_count must be in [0, unit_count]")
    if not 0 <= sample_count <= unit_count:
        raise ValueError("sample_count must be in [0, unit_count]")
    if corrupt_count == 0 or sample_count == 0:
        return 0.0
    if sample_count > unit_count - corrupt_count:
        return 1.0
    undetected = math.comb(unit_count - corrupt_count, sample_count) / math.comb(unit_count, sample_count)
    return 1.0 - undetected

