from __future__ import annotations

import shutil
import statistics
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from eth_account import Account

from ai_mining_network.benchmark import aggregate_benchmark_receipts
from ai_mining_network.canonical import content_hash
from ai_mining_network.models import (
    MAX_ATOMIC,
    BenchmarkMeasurementProvenance,
    Currency,
    MinerOffer,
    OfferCapabilities,
    ResourceRequirements,
    SignedMessage,
    SkillBenchmark,
)
from ai_mining_network.orchestrator import DemoError, DemoService
from ai_mining_network.pricing import PricingError, fixed_quote
from ai_mining_network.settlement import (
    create_settlement,
    validate_settlement_within_quote,
)
from ai_mining_network.signing import create_message


@pytest.fixture
def tmp_path():
    directory = Path(tempfile.mkdtemp(prefix="amn-p6-test-", dir=Path(__file__).resolve().parents[1]))
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def _benchmark_for(index: int) -> SkillBenchmark:
    durations = [100 + index + sample for sample in range(20)]
    results = [True] * 20
    dataset_hash = "sha256:" + "d" * 64
    payload = {
        "benchmarkVersion": "invoice-synthetic-v2",
        "skillHash": "sha256:" + "a" * 64,
        "datasetHash": dataset_hash,
        "runtime": "python-3.12.1",
        "runtimeDigest": "sha256:" + "b" * 64,
        "modelDigest": "sha256:" + "c" * 64,
        "hardwareClass": "cpu-x86_64-8-logical-gpu-unverified",
        "warmColdPolicy": "warm-after-single-warmup",
        "sampleCount": 20,
        "durationSamplesMs": durations,
        "durationVarianceMs2": round(statistics.pvariance(durations)),
        "outputValidationResults": results,
        "inputTokens": 2000,
        "outputTokens": 200,
        "p50DurationMs": durations[9],
        "p90DurationMs": durations[17],
        "successRateBps": 10_000,
        "outputValidationPasses": 20,
        "measurementProvenance": BenchmarkMeasurementProvenance(
            collectorVersion="test-v2",
            measurementId=f"fixture-{index}",
            environmentHash="sha256:" + "e" * 64,
            limitations=["test fixture", "not proof of inference"],
        ),
        "selfReportedMetrics": True,
    }
    candidate = SkillBenchmark(**payload, receiptId="pending")
    receipt_id = content_hash(candidate.model_dump(mode="json", exclude={"kind", "receiptId"}))
    return candidate.model_copy(update={"receiptId": receipt_id})


def _signed_benchmarks(count: int = 3) -> list[SignedMessage]:
    return [create_message(_benchmark_for(index), Account.create().key.hex()) for index in range(count)]


def test_p6_benchmark_aggregation_requires_independent_signers_and_qualified_samples():
    receipts = _signed_benchmarks()
    aggregate = aggregate_benchmark_receipts(receipts)

    assert aggregate.contributor_count == 3
    assert aggregate.sample_count == 60
    assert aggregate.p50_duration_ms == 110
    assert aggregate.p90_duration_ms == 118
    assert len(aggregate.receipt_ids) == 3
    assert aggregate.trust_note.startswith("Distinct signatures identify distinct keys")

    with pytest.raises(ValueError, match="distinct contributors"):
        aggregate_benchmark_receipts(receipts[:2])
    with pytest.raises(ValueError, match="wrong skill digest"):
        aggregate_benchmark_receipts(receipts, expected_skill_hash="sha256:" + "f" * 64)


def test_p6_benchmark_aggregation_rejects_duplicate_signer_and_mixed_profile():
    same_key = Account.create().key.hex()
    duplicate_signer = [
        create_message(_benchmark_for(index), same_key) for index in range(2)
    ]
    duplicate_signer.append(_signed_benchmarks(1)[0])
    with pytest.raises(ValueError, match="distinct signer"):
        aggregate_benchmark_receipts(duplicate_signer)

    receipts = _signed_benchmarks()
    changed = receipts[2].payload.model_copy(update={"hardwareClass": "gpu-other"})
    changed = changed.model_copy(update={
        "receiptId": content_hash(changed.model_dump(mode="json", exclude={"kind", "receiptId"}))
    })
    receipts[2] = create_message(changed, Account.create().key.hex())
    with pytest.raises(ValueError, match="incompatible"):
        aggregate_benchmark_receipts(receipts)


def test_p6_benchmark_aggregation_rejects_modified_signed_payload():
    receipts = _signed_benchmarks()
    altered_payload = receipts[0].payload.model_copy(update={"p90DurationMs": 9999})
    receipts[0] = receipts[0].model_copy(update={"payload": altered_payload})
    with pytest.raises(ValueError, match="signature"):
        aggregate_benchmark_receipts(receipts)


def test_p6_benchmark_receipt_recomputes_percentiles_and_requires_minimum_samples():
    benchmark = _benchmark_for(0)
    values = benchmark.model_dump(mode="python")
    values["p90DurationMs"] += 1
    with pytest.raises(ValueError, match="P50/P90"):
        SkillBenchmark.model_validate(values)
    values = benchmark.model_dump(mode="python")
    values["durationSamplesMs"] = values["durationSamplesMs"][:-1]
    with pytest.raises(ValueError):
        SkillBenchmark.model_validate(values)


def _offer(
    offer_id: str,
    peer: str,
    price: int,
    currency: Currency,
    *,
    role: str = "executor",
    expiry: datetime | None = None,
    source: str = "local-fixture",
    capabilities: OfferCapabilities | None = None,
) -> MinerOffer:
    return MinerOffer(
        offerId=offer_id,
        peerId=peer,
        skillHash="sha256:" + "a" * 64,
        priceAtomic=price,
        currency=currency,
        estimatedDurationMs=100,
        expiresAt=expiry or datetime.now(timezone.utc) + timedelta(minutes=5),
        requirements=ResourceRequirements(),
        offerSource=source,
        availableResources=capabilities,
    )


def test_p6_quote_adds_each_atomic_cost_and_fixes_offer_snapshot():
    currency = Currency(chainId=31337, tokenAddress="0x" + "1" * 40, decimals=6, symbol="TESTUSD")
    executor_capability = OfferCapabilities(
        cpuCores=4,
        ramBytes=8_000_000_000,
        modelDigests=["model-digest"],
        runtimeDigests=["runtime-digest"],
    )
    validator_capability = OfferCapabilities(
        cpuCores=2,
        ramBytes=4_000_000_000,
        modelDigests=["model-digest"],
        runtimeDigests=["runtime-digest"],
        verificationStrategies=["per-document"],
    )
    quote = fixed_quote(
        task_id="task",
        currency=currency,
        executor_offers=[
            _offer("e", "executor", 100, currency, capabilities=executor_capability)
        ],
        validator_offers=[
            _offer("v1", "validator-1", 10, currency, role="validator", capabilities=validator_capability),
            _offer("v2", "validator-2", 20, currency, role="validator", capabilities=validator_capability),
        ],
        sample_count=4,
        validator_count=2,
        max_price_atomic=150,
        required_skill_hash="sha256:" + "a" * 64,
        required_resources=ResourceRequirements(cpuCores=2, ramBytes=2_000_000_000),
        required_model_digest="model-digest",
        required_runtime_digest="runtime-digest",
        required_verification_strategy="per-document",
        protocol_fee_atomic=3,
        storage_budget_atomic=4,
        settlement_budget_atomic=5,
        assumptions={"benchmark": "held-out fixture"},
    )
    assert quote.totalAtomic == 142
    assert quote.priceBasis == "local-fixture"
    assert quote.offerSnapshotHash.startswith("sha256:")
    assert quote.assumptions["caller"]["benchmark"] == "held-out fixture"

    peer_quote = fixed_quote(
        task_id="task-peer",
        currency=currency,
        executor_offers=[_offer("pe", "executor", 100, currency, source="provider-offer")],
        validator_offers=[_offer("pv", "validator", 10, currency, source="provider-offer")],
        sample_count=1,
        validator_count=1,
        max_price_atomic=110,
    )
    assert peer_quote.priceBasis == "compatible-offers"
    assert peer_quote.priceBasis != "live-market-quote"

    with pytest.raises(PricingError, match="exceeds customer maximum"):
        fixed_quote(
            task_id="task",
            currency=currency,
            executor_offers=[_offer("e", "executor", 100, currency)],
            validator_offers=[_offer("v", "validator", 10, currency)],
            sample_count=1,
            validator_count=1,
            max_price_atomic=109,
        )


def test_p6_quote_rejects_expired_missing_or_incompatible_offers_and_overflow():
    currency = Currency(chainId=31337, tokenAddress="0x" + "1" * 40, decimals=6, symbol="TESTUSD")
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(PricingError, match="no compatible executor"):
        fixed_quote(
            "task", currency, [_offer("old", "miner", 10, currency, expiry=expired)],
            [_offer("v", "validator", 1, currency)], 1, 1, 100,
        )


def test_p6_settlement_conserves_the_accepted_quote_ceiling():
    currency = Currency(chainId=31337, tokenAddress="0x" + "1" * 40, decimals=6, symbol="TESTUSD")
    quote = fixed_quote(
        task_id="task",
        currency=currency,
        executor_offers=[_offer("e", "miner", 100, currency)],
        validator_offers=[_offer("v", "validator", 10, currency)],
        sample_count=1,
        validator_count=1,
        max_price_atomic=110,
    )
    settlement = create_settlement(
        task_id="task",
        quote_id=quote.quoteId,
        idempotency_key="settle:task",
        outcome="VERIFIED",
        currency=currency,
        execution_reward=quote.executionRewardAtomic,
        verification_budget=quote.verificationBudgetAtomic,
        miner="miner",
        validator_prices={"validator": 10},
        valid_validator_peers={"validator"},
        decision_hash="decision",
    )
    validate_settlement_within_quote(settlement, quote)
    overpaid = settlement.model_copy(
        update={
            "totalChargedAtomic": 111,
            "payouts": [
                *settlement.payouts,
                settlement.payouts[0].model_copy(update={"amountAtomic": 1}),
            ],
        }
    )
    with pytest.raises(ValueError, match="component|complete accepted quote|ceiling"):
        validate_settlement_within_quote(overpaid, quote)
    with pytest.raises(PricingError, match="no compatible executor"):
        fixed_quote(
            "task", currency, [_offer("e", "miner", 10, currency)],
            [_offer("v", "validator", 1, currency)], 1, 1, 100,
            required_resources=ResourceRequirements(cpuCores=8),
        )
    with pytest.raises(PricingError, match="overflow"):
        fixed_quote(
            "task", currency, [_offer("e", "miner", MAX_ATOMIC, currency)],
            [_offer("v", "validator", 1, currency)], 1, 1, MAX_ATOMIC,
        )


def test_p6_quote_acceptance_rejects_changed_signed_offer(tmp_path: Path):
    import shutil

    from ai_mining_network.invoice import FixtureExtractor

    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "skills" / "invoice-extractor", tmp_path / "skills" / "invoice-extractor")
    service = DemoService(tmp_path, extractor_factory=FixtureExtractor)
    job = service.create_quote(invoice_count=3, sample_count=3, validator_count=1)
    task_id = job["task"]["taskId"]
    record = service._jobs[task_id]
    raw = record["offerMessages"][0]
    envelope = SignedMessage.model_validate(raw)
    changed_offer = envelope.payload.model_copy(update={"priceAtomic": envelope.payload.priceAtomic + 1})
    record["offerMessages"][0] = envelope.model_copy(update={"payload": changed_offer}).model_dump(mode="json")
    with pytest.raises(DemoError, match="selected offer signature is invalid"):
        service.accept_quote(task_id)
    assert service.status(task_id)["state"] == "QUOTED"
