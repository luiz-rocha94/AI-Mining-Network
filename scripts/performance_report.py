"""Measure local protocol overhead with a deterministic test-only extractor."""

from __future__ import annotations

import csv
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai_mining_network.benchmark import fraud_detection_probability
from ai_mining_network.canonical import content_hash
from ai_mining_network.invoice import FixtureExtractor, generate_synthetic_invoices, normalize_record, write_workbook
from ai_mining_network.merkle import build_tree
from ai_mining_network.randomness import select_units
from ai_mining_network.verification import verify_invoice_unit


def percentile(values: list[int], quantile: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int((len(ordered) - 1) * quantile + 0.999999)))]


def run() -> dict:
    output_dir = ROOT / "docs" / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="amn-benchmark-", dir=ROOT))
    count, sample_count, validator_count = 100, 30, 3
    try:
        started = time.perf_counter()
        invoices = generate_synthetic_invoices(work / "inputs", count, seed=810_2026)
        dataset_seconds = time.perf_counter() - started

        extractor = FixtureExtractor()
        durations = []
        records = []
        execution_started = time.perf_counter()
        for invoice in invoices:
            tick = time.perf_counter()
            records.extend(normalize_record(item) for item in extractor.extract_batch([invoice]))
            durations.append(int((time.perf_counter() - tick) * 1000))
        execution_seconds = time.perf_counter() - execution_started
        records.sort(key=lambda item: item["invoiceId"])

        results_path = work / "results.json"
        xlsx_path = work / "invoices.xlsx"
        results_path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        write_workbook(records, xlsx_path)
        input_by_id = {invoice.invoice_id: invoice for invoice in invoices}
        leaves = [
            (
                "0x" + __import__("hashlib").sha256(record["invoiceId"].encode()).hexdigest(),
                input_by_id[record["invoiceId"]].input_hash,
                content_hash(record),
            )
            for record in records
        ]
        commitment_started = time.perf_counter()
        tree = build_tree(leaves)
        commit_seconds = time.perf_counter() - commitment_started

        verification_started = time.perf_counter()
        seed = bytes.fromhex("4f" * 32)
        assignment = select_units(seed, [invoice.invoice_id for invoice in invoices], sample_count, ["validator-a", "validator-b", "validator-c"])
        by_index = {record["invoiceId"]: index for index, record in enumerate(records)}
        record_by_id = {record["invoiceId"]: record for record in records}
        checked = 0
        for peer, unit_ids in assignment.items():
            observations = extractor.extract_batch([input_by_id[unit_id] for unit_id in unit_ids])
            observed = {item.invoiceId: item for item in observations}
            for unit_id in unit_ids:
                result = verify_invoice_unit(by_index[unit_id], input_by_id[unit_id], record_by_id[unit_id], observed[unit_id], tree)
                if result["result"] != "PASS":
                    raise RuntimeError(f"test-only verifier failed for {unit_id}: {result['reasonCode']}")
                checked += 1
        verification_seconds = time.perf_counter() - verification_started
        proof_json = json.dumps([tree.proof(by_index[unit]) for units in assignment.values() for unit in units], separators=(",", ":")).encode()

        probabilities = []
        with (output_dir / "fraud-detection.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["units", "corrupt", "samples", "detection_probability"])
            writer.writeheader()
            for corrupt in (1, 5, 10):
                for samples in range(0, count + 1, 5):
                    probability = fraud_detection_probability(count, corrupt, samples)
                    writer.writerow({"units": count, "corrupt": corrupt, "samples": samples, "detection_probability": f"{probability:.8f}"})
                    probabilities.append((corrupt, samples, probability))
        _write_chart(output_dir / "fraud-detection.svg", probabilities)

        input_bytes = sum(Path(invoice.path).stat().st_size for invoice in invoices)
        records_bytes = results_path.stat().st_size
        workbook_bytes = xlsx_path.stat().st_size
        quote_atomic = 160_000  # local minimum TESTUSD offer: 100k execution + 3*20k validation
        measured = {
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "profile": "test-only deterministic fixture extractor; no model inference",
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpuCount": os.cpu_count(),
            "units": count,
            "samples": sample_count,
            "validators": validator_count,
            "verifiedSamples": checked,
            "datasetGenerationSeconds": dataset_seconds,
            "fixtureExtractTotalSeconds": execution_seconds,
            "fixtureExtractP50Ms": percentile(durations, 0.50),
            "fixtureExtractP90Ms": percentile(durations, 0.90),
            "commitmentBuildSeconds": commit_seconds,
            "verificationTotalSeconds": verification_seconds,
            "merkleRoot": tree.root,
            "proofBytesForSamples": len(proof_json),
            "inputPdfBytes": input_bytes,
            "jsonOutputBytes": records_bytes,
            "xlsxOutputBytes": workbook_bytes,
            "localQuoteAtomic": quote_atomic,
            "localQuoteCurrency": "TESTUSD (6 decimals, no value)",
        }
        _write_report(output_dir / "performance-local-fixture.md", measured)
        (output_dir / "performance-local-fixture.json").write_text(json.dumps(measured, indent=2) + "\n", encoding="utf-8")
        return measured
    finally:
        import shutil

        shutil.rmtree(work)


def _write_report(path: Path, data: dict) -> None:
    lines = [
        "# Local protocol performance report",
        "",
        f"Captured: `{data['createdAt']}`",
        "",
        "## Scope",
        "",
        "This run measures local protocol overhead for 100 synthetic invoices using the deterministic **test-only** `FixtureExtractor`. It measures no AI inference, GPU work, decentralized network transfer, or production payment. Do not use these timings to predict Ollama performance or miner revenue.",
        "",
        f"Environment: `{data['platform']}` · Python `{data['python']}` · {data['cpuCount']} logical CPU count reported.",
        "",
        "## Measurements",
        "",
        "| Measure | Result |",
        "|---|---:|",
        f"| Input PDF generation | {data['datasetGenerationSeconds']:.3f} s |",
        f"| Fixture extraction, 100 records | {data['fixtureExtractTotalSeconds']:.3f} s |",
        f"| Fixture extraction per-record P50 / P90 | {data['fixtureExtractP50Ms']} / {data['fixtureExtractP90Ms']} ms |",
        f"| Standard Merkle commitment construction | {data['commitmentBuildSeconds']:.4f} s |",
        f"| Independent fixture checks, {data['verifiedSamples']} assigned samples | {data['verificationTotalSeconds']:.4f} s |",
        f"| Serialized sample proof set | {data['proofBytesForSamples']} bytes |",
        f"| Synthetic PDFs / JSON / XLSX | {data['inputPdfBytes']} / {data['jsonOutputBytes']} / {data['xlsxOutputBytes']} bytes |",
        f"| Local demonstration quote | {data['localQuoteAtomic']} atomic {data['localQuoteCurrency']} |",
        "",
        "The demo quote uses the local adapter's minimum offers. It is an exercise of fixed-price accounting, not a market price or a measured compute cost. The `OllamaExtractor` benchmark stores real local-model duration and token receipts when Ollama is installed; this report deliberately does not substitute fixture timings for that benchmark.",
        "",
        "## Sampling detection probability",
        "",
        "For uniformly selected distinct units, detection is `1 - C(N-k,s)/C(N,s)` for N units, k corrupted units, and s samples. These probabilities assume uniform sampling and at least one sampled corrupted unit can be objectively detected. Sampling is probabilistic and never proves that all unsampled outputs are correct.",
        "",
        "See [fraud-detection.svg](fraud-detection.svg) and [fraud-detection.csv](fraud-detection.csv) for the complete curve at 1, 5, and 10 corrupt units out of 100.",
        "",
        "## Not measured in this offline environment",
        "",
        "- Model inference throughput, Ollama token costs, CPU/GPU utilization and VRAM: Ollama was unavailable during this report run.",
        "- Real miner offer / quote accuracy, network overhead, multi-peer scaling, content-addressed storage overhead, and on-chain settlement latency.",
        "- Production fraud detection; the curve is a mathematical model, not a security benchmark.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_chart(path: Path, values: list[tuple[int, int, float]]) -> None:
    width, height = 780, 440
    left, top, plot_w, plot_h = 75, 40, 640, 320
    colors = {1: "#4b83d1", 5: "#6da64f", 10: "#d88a39"}
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="#101a17"/>',
             '<text x="75" y="24" fill="#edf3ef" font-family="Arial" font-size="16">Fraud detection probability: uniform sampling, N = 100</text>']
    for percent in range(0, 101, 20):
        y = top + plot_h * (1 - percent / 100)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left+plot_w}" y2="{y:.1f}" stroke="#2b3b34"/>')
        parts.append(f'<text x="{left-12}" y="{y+4:.1f}" text-anchor="end" fill="#9dac9f" font-size="10">{percent}%</text>')
    for sample in range(0, 101, 20):
        x = left + plot_w * sample / 100
        parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top+plot_h}" stroke="#202f29"/>')
        parts.append(f'<text x="{x:.1f}" y="{top+plot_h+20}" text-anchor="middle" fill="#9dac9f" font-size="10">{sample}</text>')
    for corrupt, color in colors.items():
        points = []
        for _, sample, probability in [item for item in values if item[0] == corrupt]:
            x = left + plot_w * sample / 100
            y = top + plot_h * (1 - probability)
            points.append(f"{x:.1f},{y:.1f}")
        parts.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="3"/>')
        parts.append(f'<text x="{left+corrupt*8+6}" y="{top+plot_h+53}" fill="{color}" font-size="11">{corrupt} corrupt unit(s)</text>')
    parts += [f'<text x="{left+plot_w/2}" y="{height-17}" text-anchor="middle" fill="#b8c5bb" font-size="11">Number of distinct units sampled</text>',
              f'<text transform="translate(18 {top+plot_h/2}) rotate(-90)" text-anchor="middle" fill="#b8c5bb" font-size="11">Probability of detecting ≥1 corruption</text>',
              "</svg>"]
    path.write_text("\n".join(parts), encoding="utf-8")


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
