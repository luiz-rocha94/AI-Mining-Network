"""Signed benchmark submissions for the invoice skill."""

from __future__ import annotations

import json
import platform
import statistics
import sys
import time
from importlib import metadata
from pathlib import Path
from uuid import uuid4

from eth_account import Account

from .benchmark import summarize_runs
from .canonical import content_hash
from .invoice import Extractor, generate_synthetic_invoices, normalize_record
from .models import SkillBenchmark
from .registry import compute_skill_hash
from .signing import create_message


def run_invoice_benchmark(
    skill_dir: Path,
    output_dir: Path,
    extractor: Extractor,
    sample_count: int = 20,
    private_key: str | None = None,
) -> tuple[SkillBenchmark, dict]:
    if sample_count < 20:
        raise ValueError("P50/P90 benchmark receipts require at least 20 samples")
    sample_dir = output_dir / "benchmark-inputs"
    inputs = generate_synthetic_invoices(sample_dir, sample_count, seed=810_2026)
    truth = json.loads((sample_dir / "ground-truth.json").read_text(encoding="utf-8"))
    dataset_hash = content_hash(truth)
    truth_by_id = {record["invoiceId"]: record for record in truth}

    # Exclude one explicit warm-up call from the signed sample distribution.
    # Each subsequent run is still a self-reported observation from this worker.
    extractor.extract_batch([inputs[0]])
    durations: list[int] = []
    successes: list[bool] = []
    in_tokens = out_tokens = 0
    cpu_samples: list[int] = []
    ram_samples: list[int] = []
    process = None
    try:
        import psutil

        process = psutil.Process()
    except ImportError:
        pass
    resource_observations_complete = process is not None
    for invoice in inputs:
        started_ns = time.perf_counter_ns()
        cpu_before = None
        if process is not None:
            try:
                times = process.cpu_times()
                cpu_before = times.user + times.system
            except Exception:  # noqa: BLE001 - metrics are optional self-reports.
                resource_observations_complete = False
        result = extractor.extract_batch([invoice])[0]
        wall_seconds = max(1, time.perf_counter_ns() - started_ns) / 1_000_000_000
        metrics = getattr(extractor, "last_metrics", {})
        durations.append(int(metrics.get("durationMs", 0)))
        in_tokens += int(metrics.get("inputTokens", 0))
        out_tokens += int(metrics.get("outputTokens", 0))
        try:
            successes.append(normalize_record(result) == truth_by_id[invoice.invoice_id])
        except (ValueError, KeyError):
            successes.append(False)
        if process is not None and cpu_before is not None:
            try:
                times = process.cpu_times()
                cpu_after = times.user + times.system
                cpu_samples.append(max(0, round((cpu_after - cpu_before) / wall_seconds * 100_000)))
                ram_samples.append(int(process.memory_info().rss))
            except Exception:  # noqa: BLE001 - metrics are optional self-reports.
                resource_observations_complete = False
    if not resource_observations_complete or len(cpu_samples) != sample_count or len(ram_samples) != sample_count:
        cpu_samples = []
        ram_samples = []
    summary = summarize_runs(durations, successes, in_tokens, out_tokens)
    account = Account.from_key(private_key) if private_key else Account.create()
    runtime = f"python-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    package_versions = {}
    for package in ("ai-mining-network", "pydantic", "pypdf", "openpyxl"):
        try:
            package_versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            package_versions[package] = "not-installed-as-distribution"
    runtime_descriptor = {
        "runtime": runtime,
        "implementation": sys.implementation.name,
        "packages": package_versions,
        "benchmarkHarness": "invoice-synthetic-v2",
        "extractor": f"{type(extractor).__module__}.{type(extractor).__qualname__}",
    }
    runtime_digest = content_hash(runtime_descriptor)
    logical_cpus = 0
    try:
        import psutil

        logical_cpus = psutil.cpu_count(logical=True) or 0
    except ImportError:
        pass
    machine = platform.machine().lower() or "unknown"
    hardware_class = f"cpu-{machine}-{logical_cpus}-logical-gpu-unverified"
    environment_descriptor = {
        "runtimeDigest": runtime_digest,
        "hardwareClass": hardware_class,
        "system": platform.system(),
        "release": platform.release(),
        "machine": machine,
        "processor": platform.processor() or "unknown",
    }
    skill_hash = compute_skill_hash(skill_dir)
    benchmark_payload = {
        "benchmarkVersion": "invoice-synthetic-v2",
        "skillHash": skill_hash,
        "datasetHash": dataset_hash,
        "runtime": runtime,
        "runtimeDigest": runtime_digest,
        "modelDigest": extractor.model_digest,
        "hardwareClass": hardware_class,
        "warmColdPolicy": "warm-after-single-warmup",
        "sampleCount": summary.samples,
        "durationSamplesMs": list(summary.duration_samples_ms),
        "durationVarianceMs2": summary.duration_variance_ms2,
        "outputValidationResults": list(summary.output_validation_results),
        "inputTokens": summary.input_tokens,
        "outputTokens": summary.output_tokens,
        "p50DurationMs": summary.p50_duration_ms,
        "p90DurationMs": summary.p90_duration_ms,
        # Resource observations cover this benchmark client only. The Ollama
        # inference daemon is separate, so its CPU/GPU/VRAM remain unknown.
        "cpuAverageMilliPct": round(statistics.mean(cpu_samples)) if cpu_samples else None,
        "cpuSamplesMilliPct": cpu_samples or None,
        "peakRamBytes": max(ram_samples) if ram_samples else None,
        "ramSamplesBytes": ram_samples or None,
        "peakVramBytes": None,
        "successRateBps": summary.success_rate_bps,
        "outputValidationPasses": sum(successes),
        "measurementProvenance": {
            "measurementMode": "self-reported",
            "collectorVersion": "amn-benchmark-v2",
            "measurementId": str(uuid4()),
            "environmentHash": content_hash(environment_descriptor),
            "limitations": [
                "environment and hardware descriptions are contributor-reported",
                "distinct signing keys do not prove distinct operators",
                "CPU and RSS cover only the benchmark client; Ollama process usage is not measured",
                "GPU and VRAM are unknown because the local adapter cannot attribute Ollama inference usage",
                "a signature binds this report but does not prove inference occurred",
            ],
        },
        "selfReportedMetrics": True,
    }
    benchmark = SkillBenchmark(
        **benchmark_payload,
        receiptId=content_hash(benchmark_payload),
    )
    signed = create_message(benchmark, account.key.hex())
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "benchmark-receipt.json").write_text(
        signed.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
    )
    report = {
        "sampleCount": summary.samples,
        "benchmarkVersion": benchmark.benchmarkVersion,
        "datasetHash": benchmark.datasetHash,
        "skillHash": benchmark.skillHash,
        "runtime": benchmark.runtime,
        "runtimeDigest": benchmark.runtimeDigest,
        "hardwareClass": benchmark.hardwareClass,
        "warmColdPolicy": benchmark.warmColdPolicy,
        "durationSamplesMs": benchmark.durationSamplesMs,
        "durationVarianceMs2": benchmark.durationVarianceMs2,
        "outputValidationResults": benchmark.outputValidationResults,
        "p50DurationMs": summary.p50_duration_ms,
        "p90DurationMs": summary.p90_duration_ms,
        "successRateBps": summary.success_rate_bps,
        "inputTokens": summary.input_tokens,
        "outputTokens": summary.output_tokens,
        "cpuAverageMilliPct": benchmark.cpuAverageMilliPct,
        "cpuSamplesMilliPct": benchmark.cpuSamplesMilliPct,
        "peakRamBytes": benchmark.peakRamBytes,
        "ramSamplesBytes": benchmark.ramSamplesBytes,
        "peakVramBytes": None,
        "modelDigest": extractor.model_digest,
        "metricsAreSelfReported": benchmark.selfReportedMetrics,
        "measurementProvenance": benchmark.measurementProvenance.model_dump(mode="json"),
        "signedBy": signed.signer,
        "signature": signed.signature,
    }
    (output_dir / "benchmark-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return benchmark, report

