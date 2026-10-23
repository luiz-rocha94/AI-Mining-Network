# Local protocol performance report

Captured: `2026-10-22T21:46:10.103577+00:00`

## Scope

This run measures local protocol overhead for 100 synthetic invoices using the deterministic **test-only** `FixtureExtractor`. It measures no AI inference, GPU work, decentralized network transfer, or production payment. Do not use these timings to predict Ollama performance or miner revenue.

Environment: `Windows-11-10.0.26300-SP0` · Python `3.12.14` · 16 logical CPU count reported.

## Measurements

| Measure | Result |
|---|---:|
| Input PDF generation | 0.581 s |
| Fixture extraction, 100 records | 0.001 s |
| Fixture extraction per-record P50 / P90 | 0 / 0 ms |
| Standard Merkle commitment construction | 0.0312 s |
| Independent fixture checks, 30 assigned samples | 0.0024 s |
| Serialized sample proof set | 14206 bytes |
| Synthetic PDFs / JSON / XLSX | 169151 / 17952 / 8638 bytes |
| Local demonstration quote | 160000 atomic TESTUSD (6 decimals, no value) |

The demo quote uses the local adapter's minimum offers. It is an exercise of fixed-price accounting, not a market price or a measured compute cost. The `OllamaExtractor` benchmark stores real local-model duration and token receipts when Ollama is installed; this report deliberately does not substitute fixture timings for that benchmark.

## Sampling detection probability

For uniformly selected distinct units, detection is `1 - C(N-k,s)/C(N,s)` for N units, k corrupted units, and s samples. These probabilities assume uniform sampling and at least one sampled corrupted unit can be objectively detected. Sampling is probabilistic and never proves that all unsampled outputs are correct.

See [fraud-detection.svg](fraud-detection.svg) and [fraud-detection.csv](fraud-detection.csv) for the complete curve at 1, 5, and 10 corrupt units out of 100.

## Not measured in this offline environment

- Model inference throughput, Ollama token costs, CPU/GPU utilization and VRAM: Ollama was unavailable during this report run.
- Real miner offer / quote accuracy, network overhead, multi-peer scaling, content-addressed storage overhead, and on-chain settlement latency.
- Production fraud detection; the curve is a mathematical model, not a security benchmark.
