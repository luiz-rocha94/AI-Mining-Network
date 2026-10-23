"""Signed benchmark submissions for the invoice skill."""

from __future__ import annotations

import json
from pathlib import Path

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
    durations: list[int] = []
    successes: list[bool] = []
    in_tokens = out_tokens = 0
    ram_peaks: list[int] = []
    for invoice in inputs:
        result = extractor.extract_batch([invoice])[0]
        metrics = getattr(extractor, "last_metrics", {})
        durations.append(int(metrics.get("durationMs", 0)))
        in_tokens += int(metrics.get("inputTokens", 0))
        out_tokens += int(metrics.get("outputTokens", 0))
        try:
            successes.append(normalize_record(result) == truth_by_id[invoice.invoice_id])
        except (ValueError, KeyError):
            successes.append(False)
        try:
            import psutil

            ram_peaks.append(int(psutil.Process().memory_info().rss))
        except ImportError:
            pass
    summary = summarize_runs(durations, successes, in_tokens, out_tokens)
    account = Account.from_key(private_key) if private_key else Account.create()
    benchmark = SkillBenchmark(
        benchmarkVersion="invoice-synthetic-v1",
        skillHash=compute_skill_hash(skill_dir),
        datasetHash=dataset_hash,
        runtime=f"python-{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}",
        modelDigest=extractor.model_digest,
        hardwareClass="local-cpu-or-gpu-unverified",
        sampleCount=summary.samples,
        inputTokens=summary.input_tokens,
        outputTokens=summary.output_tokens,
        p50DurationMs=summary.p50_duration_ms,
        p90DurationMs=summary.p90_duration_ms,
        # The current process RSS is sampled between records. CPU and VRAM are
        # unknown here (Ollama is a separate process), so emit null rather than
        # implying those resources had zero utilization.
        cpuAverageMilliPct=None,
        peakRamBytes=max(ram_peaks) if ram_peaks else None,
        peakVramBytes=None,
        successRateBps=summary.success_rate_bps,
        outputValidationPasses=sum(successes),
        selfReportedMetrics=True,
        receiptId=content_hash({
            "benchmarkVersion": "invoice-synthetic-v1",
            "skillHash": compute_skill_hash(skill_dir),
            "datasetHash": dataset_hash,
            "modelDigest": extractor.model_digest,
            "runtime": f"python-{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}",
            "hardwareClass": "local-cpu-or-gpu-unverified",
            "sampleCount": summary.samples,
            "p50DurationMs": summary.p50_duration_ms,
            "p90DurationMs": summary.p90_duration_ms,
            "successRateBps": summary.success_rate_bps,
            "inputTokens": summary.input_tokens,
            "outputTokens": summary.output_tokens,
            "cpuAverageMilliPct": None,
            "peakRamBytes": max(ram_peaks) if ram_peaks else None,
            "peakVramBytes": None,
            "outputValidationPasses": sum(successes),
            "selfReportedMetrics": True,
        }),
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
        "p50DurationMs": summary.p50_duration_ms,
        "p90DurationMs": summary.p90_duration_ms,
        "successRateBps": summary.success_rate_bps,
        "inputTokens": summary.input_tokens,
        "outputTokens": summary.output_tokens,
        "cpuAverageMilliPct": None,
        "peakRamBytes": benchmark.peakRamBytes,
        "peakVramBytes": None,
        "modelDigest": extractor.model_digest,
        "metricsAreSelfReported": True,
        "signedBy": signed.signer,
        "signature": signed.signature,
    }
    (output_dir / "benchmark-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return benchmark, report

