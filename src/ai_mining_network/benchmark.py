"""Small reproducible benchmark aggregation with explicit provenance."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from dataclasses import dataclass

from .canonical import content_hash
from .models import SignedMessage, SkillBenchmark


@dataclass(frozen=True)
class BenchmarkSummary:
    samples: int
    p50_duration_ms: int
    p90_duration_ms: int
    mean_duration_ms: int
    success_rate_bps: int
    input_tokens: int
    output_tokens: int
    duration_samples_ms: tuple[int, ...]
    duration_variance_ms2: int
    output_validation_results: tuple[bool, ...]
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
        duration_samples_ms=tuple(durations_ms),
        duration_variance_ms2=round(statistics.pvariance(durations_ms)),
        output_validation_results=tuple(successful),
    )


@dataclass(frozen=True)
class BenchmarkAggregationPolicy:
    """Thresholds for publicly describing a set of independent submissions."""

    minimum_contributors: int = 3
    minimum_samples_per_contributor: int = 20
    minimum_total_samples: int = 60
    minimum_success_rate_bps: int = 9_500
    maximum_contributors: int = 100

    def __post_init__(self):
        if self.minimum_contributors < 1:
            raise ValueError("minimum_contributors must be positive")
        if self.minimum_samples_per_contributor < 20:
            raise ValueError("minimum_samples_per_contributor must be at least 20")
        if self.minimum_total_samples < self.minimum_samples_per_contributor:
            raise ValueError("minimum_total_samples must cover at least one contributor")
        if not 0 <= self.minimum_success_rate_bps <= 10_000:
            raise ValueError("minimum_success_rate_bps must be in [0, 10000]")
        if self.maximum_contributors < self.minimum_contributors:
            raise ValueError("maximum_contributors must be >= minimum_contributors")


@dataclass(frozen=True)
class BenchmarkAggregate:
    benchmark_version: str
    skill_hash: str
    dataset_hash: str
    runtime_digest: str
    model_digest: str
    hardware_class: str
    warm_cold_policy: str
    contributor_count: int
    sample_count: int
    p50_duration_ms: int
    p90_duration_ms: int
    duration_variance_ms2: int
    success_rate_bps: int
    output_validation_passes: int
    receipt_ids: tuple[str, ...]
    contributor_signers: tuple[str, ...]
    source_receipts_hash: str
    aggregation_policy: BenchmarkAggregationPolicy
    trust_note: str = (
        "Distinct signatures identify distinct keys, not independent operators; "
        "these measurements remain contributor-reported and are not proof of inference."
    )


def _validate_benchmark_evidence(benchmark: SkillBenchmark) -> None:
    """Recompute signed summary claims from the included per-run observations."""
    durations = benchmark.durationSamplesMs
    results = benchmark.outputValidationResults
    if len(durations) != benchmark.sampleCount or len(results) != benchmark.sampleCount:
        raise ValueError("benchmark sample arrays do not match sampleCount")
    if benchmark.p50DurationMs != _percentile(durations, 0.50):
        raise ValueError("benchmark P50 does not match observed durations")
    if benchmark.p90DurationMs != _percentile(durations, 0.90):
        raise ValueError("benchmark P90 does not match observed durations")
    if benchmark.durationVarianceMs2 != round(statistics.pvariance(durations)):
        raise ValueError("benchmark variance does not match observed durations")
    passes = sum(results)
    if benchmark.outputValidationPasses != passes:
        raise ValueError("benchmark output validation count does not match its observations")
    if benchmark.successRateBps != round(10_000 * passes / len(results)):
        raise ValueError("benchmark success rate does not match its validation observations")
    receipt_content = benchmark.model_dump(mode="json", exclude={"kind", "receiptId"})
    if benchmark.receiptId != content_hash(receipt_content):
        raise ValueError("benchmark receipt ID does not match its content")


def aggregate_benchmark_receipts(
    receipts: Iterable[SignedMessage],
    policy: BenchmarkAggregationPolicy | None = None,
    *,
    expected_skill_hash: str | None = None,
    expected_model_digest: str | None = None,
) -> BenchmarkAggregate:
    """Aggregate independently signed, profile-matched benchmark observations.

    This checks EIP-712 signatures and arithmetic consistency. It cannot establish
    that contributors really ran the workload or that distinct keys belong to
    distinct operators.
    """
    from .signing import verify_message

    selected_policy = policy or BenchmarkAggregationPolicy()
    accepted: list[tuple[SignedMessage, SkillBenchmark]] = []
    seen_signers: set[str] = set()
    seen_receipt_ids: set[str] = set()
    for envelope in receipts:
        if not verify_message(envelope):
            raise ValueError(f"invalid benchmark envelope signature: {envelope.id}")
        if not isinstance(envelope.payload, SkillBenchmark):
            raise ValueError(f"envelope {envelope.id} is not a SkillBenchmark")
        benchmark = SkillBenchmark.model_validate(
            envelope.payload.model_dump(mode="json", exclude_none=False)
        )
        _validate_benchmark_evidence(benchmark)
        if benchmark.measurementProvenance.measurementMode != "self-reported":
            raise ValueError("no observer or hardware-attestation verifier is configured")
        signer = envelope.signer.lower()
        if signer in seen_signers:
            raise ValueError("benchmark aggregation requires distinct signer keys")
        if benchmark.receiptId in seen_receipt_ids:
            raise ValueError("duplicate benchmark receipt ID")
        if benchmark.sampleCount < selected_policy.minimum_samples_per_contributor:
            raise ValueError("a contributor receipt is below the per-contributor sample threshold")
        if benchmark.successRateBps < selected_policy.minimum_success_rate_bps:
            raise ValueError("a contributor receipt is below the output quality threshold")
        if expected_skill_hash is not None and benchmark.skillHash != expected_skill_hash:
            raise ValueError("benchmark receipt has the wrong skill digest")
        if expected_model_digest is not None and benchmark.modelDigest != expected_model_digest:
            raise ValueError("benchmark receipt has the wrong model digest")
        seen_signers.add(signer)
        seen_receipt_ids.add(benchmark.receiptId)
        accepted.append((envelope, benchmark))
        if len(accepted) > selected_policy.maximum_contributors:
            raise ValueError("benchmark aggregation exceeds maximum_contributors")

    if len(accepted) < selected_policy.minimum_contributors:
        raise ValueError(
            f"need {selected_policy.minimum_contributors} distinct contributors; found {len(accepted)}"
        )
    profiles = {
        (
            item.benchmarkVersion,
            item.skillHash,
            item.datasetHash,
            item.runtimeDigest,
            item.modelDigest,
            item.hardwareClass,
            item.warmColdPolicy,
        )
        for _, item in accepted
    }
    if len(profiles) != 1:
        raise ValueError("benchmark receipts have incompatible skill/model/runtime/hardware profiles")
    profile = next(iter(profiles))
    observations = [duration for _, item in accepted for duration in item.durationSamplesMs]
    validations = [result for _, item in accepted for result in item.outputValidationResults]
    if len(observations) < selected_policy.minimum_total_samples:
        raise ValueError("aggregate does not meet minimum_total_samples")
    pass_count = sum(validations)
    success_rate = round(10_000 * pass_count / len(validations))
    if success_rate < selected_policy.minimum_success_rate_bps:
        raise ValueError("aggregate output quality is below the configured threshold")
    ordered = sorted(observations)
    receipts_sorted = sorted(accepted, key=lambda pair: pair[1].receiptId)
    source_hash = content_hash(
        [
            {"messageId": envelope.id, "receiptId": benchmark.receiptId}
            for envelope, benchmark in receipts_sorted
        ]
    )
    return BenchmarkAggregate(
        benchmark_version=profile[0],
        skill_hash=profile[1],
        dataset_hash=profile[2],
        runtime_digest=profile[3],
        model_digest=profile[4],
        hardware_class=profile[5],
        warm_cold_policy=profile[6],
        contributor_count=len(accepted),
        sample_count=len(observations),
        p50_duration_ms=_percentile(ordered, 0.50),
        p90_duration_ms=_percentile(ordered, 0.90),
        duration_variance_ms2=round(statistics.pvariance(observations)),
        success_rate_bps=success_rate,
        output_validation_passes=pass_count,
        receipt_ids=tuple(item.receiptId for _, item in receipts_sorted),
        contributor_signers=tuple(sorted(seen_signers)),
        source_receipts_hash=source_hash,
        aggregation_policy=selected_policy,
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

