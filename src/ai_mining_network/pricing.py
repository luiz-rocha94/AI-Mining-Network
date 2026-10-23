"""Fixed quote calculation. All money is integer atomic currency units."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .canonical import content_hash
from .models import (
    MAX_ATOMIC,
    Currency,
    FixedPriceQuote,
    MinerOffer,
    ResourceRequirements,
)


class PricingError(ValueError):
    pass


def currency_matches(left: Currency, right: Currency) -> bool:
    """Compare an asset by chain/contract/decimals while ignoring address casing."""
    return (
        left.chainId == right.chainId
        and left.tokenAddress.lower() == right.tokenAddress.lower()
        and left.decimals == right.decimals
        and left.symbol == right.symbol
    )


def local_fixture_offer_prices(
    p90_duration_ms: int,
    unit_count: int,
    sample_count: int,
    validator_count: int,
    *,
    atomic_per_ms: int = 4,
    executor_floor_atomic: int = 100_000,
    validator_floor_atomic: int = 20_000,
) -> tuple[int, int]:
    """Rate-card prices for the offline development fixture; never a live quote."""
    if p90_duration_ms < 0 or unit_count < 1 or sample_count < 1 or validator_count < 1:
        raise PricingError("fixture duration, units, samples, and validators are invalid")
    if sample_count > unit_count or validator_count > sample_count:
        raise PricingError("fixture validators and samples must fit the unit count")
    if atomic_per_ms < 0 or executor_floor_atomic < 0 or validator_floor_atomic < 0:
        raise PricingError("fixture rate card values cannot be negative")
    execution = max(executor_floor_atomic, p90_duration_ms * unit_count * atomic_per_ms)
    per_validator = max(
        validator_floor_atomic,
        p90_duration_ms * math.ceil(sample_count / validator_count) * atomic_per_ms,
    )
    if max(execution, per_validator) > MAX_ATOMIC:
        raise PricingError("local fixture rate card exceeds the protocol safe-integer ceiling")
    return execution, per_validator


def fixed_quote(
    task_id: str,
    currency: Currency,
    executor_offers: list[MinerOffer],
    validator_offers: list[MinerOffer],
    sample_count: int,
    validator_count: int,
    max_price_atomic: int,
    quote_ttl_seconds: int = 600,
    required_skill_hash: str | None = None,
    required_resources: ResourceRequirements | None = None,
    required_model_digest: str | None = None,
    required_runtime_digest: str | None = None,
    required_verification_strategy: str | None = None,
    protocol_fee_atomic: int = 0,
    storage_budget_atomic: int = 0,
    settlement_budget_atomic: int = 0,
    assumptions: dict | None = None,
) -> FixedPriceQuote:
    if (
        sample_count < 1
        or validator_count < 1
        or validator_count > sample_count
        or quote_ttl_seconds < 1
    ):
        raise PricingError("sample count, validator count, quote expiry, and price cap must be valid")
    for name, value in {
        "max_price_atomic": max_price_atomic,
        "protocol_fee_atomic": protocol_fee_atomic,
        "storage_budget_atomic": storage_budget_atomic,
        "settlement_budget_atomic": settlement_budget_atomic,
    }.items():
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_ATOMIC:
            raise PricingError(f"{name} must be a JSON-safe integer in [0, {MAX_ATOMIC}]")
    now = datetime.now(timezone.utc)

    def compatible(
        offer: MinerOffer,
        *,
        require_validator_strategy: bool = False,
    ) -> bool:
        if offer.expiresAt.tzinfo is None or offer.expiresAt <= now:
            return False
        if not currency_matches(offer.currency, currency):
            return False
        if required_skill_hash is not None and offer.skillHash != required_skill_hash:
            return False
        capabilities = offer.availableResources
        if capabilities is not None and not capabilities.available:
            return False
        if required_resources is not None:
            if capabilities is None:
                return False
            if capabilities.cpuCores < required_resources.cpuCores:
                return False
            if capabilities.ramBytes < required_resources.ramBytes:
                return False
            if capabilities.vramBytes < required_resources.vramBytes:
                return False
            if required_resources.gpuRequired and not capabilities.gpuModels:
                return False
            if required_resources.networkAccess and not capabilities.supportsNetworkAccess:
                return False
            if offer.estimatedDurationMs > required_resources.timeoutSeconds * 1000:
                return False
        if required_model_digest is not None:
            if capabilities is None or required_model_digest not in capabilities.modelDigests:
                return False
        if required_runtime_digest is not None:
            if capabilities is None or required_runtime_digest not in capabilities.runtimeDigests:
                return False
        if require_validator_strategy and required_verification_strategy is not None:
            if capabilities is None or required_verification_strategy not in capabilities.verificationStrategies:
                return False
        return True

    compatible_executors = [offer for offer in executor_offers if compatible(offer)]
    if not compatible_executors:
        raise PricingError("no compatible executor offers are available")
    executor = min(compatible_executors, key=lambda offer: (offer.priceAtomic, offer.offerId))
    validators = []
    seen_peers = {executor.peerId.lower()}
    for offer in sorted(validator_offers, key=lambda item: (item.priceAtomic, item.offerId)):
        peer = offer.peerId.lower()
        if not compatible(offer, require_validator_strategy=True):
            continue
        if peer in seen_peers:
            continue
        seen_peers.add(peer)
        validators.append(offer)
        if len(validators) == validator_count:
            break
    if len(validators) < validator_count:
        raise PricingError(f"need {validator_count} distinct compatible validator offers; found {len(validators)}")
    if assumptions is not None and not isinstance(assumptions, dict):
        raise PricingError("quote assumptions must be an object")
    try:
        content_hash(assumptions or {})
    except (TypeError, ValueError) as exc:
        raise PricingError(f"quote assumptions are not canonical safe JSON: {exc}") from exc
    execution = executor.priceAtomic
    verification = sum(offer.priceAtomic for offer in validators)
    components = [execution, verification, protocol_fee_atomic, storage_budget_atomic, settlement_budget_atomic]
    total = 0
    for amount in components:
        if amount < 0 or amount > MAX_ATOMIC or total > MAX_ATOMIC - amount:
            raise PricingError("quote component arithmetic overflows the protocol safe-integer ceiling")
        total += amount
    if total > max_price_atomic:
        raise PricingError(f"fixed quote {total} exceeds customer maximum {max_price_atomic}")
    selected_offers = [executor, *validators]
    snapshot_hash = content_hash(
        [offer.model_dump(mode="json") for offer in selected_offers]
    )
    price_basis = (
        "local-fixture"
        if any(offer.offerSource == "local-fixture" for offer in selected_offers)
        else "compatible-offers"
    )
    return FixedPriceQuote(
        quoteId=str(uuid4()),
        taskId=task_id,
        executionRewardAtomic=execution,
        verificationBudgetAtomic=verification,
        protocolFeeAtomic=protocol_fee_atomic,
        storageBudgetAtomic=storage_budget_atomic,
        settlementBudgetAtomic=settlement_budget_atomic,
        totalAtomic=total,
        currency=currency,
        expiresAt=min([now + timedelta(seconds=quote_ttl_seconds), executor.expiresAt, *(offer.expiresAt for offer in validators)]),
        executorOfferId=executor.offerId,
        validatorOfferIds=[offer.offerId for offer in validators],
        priceBasis=price_basis,
        offerSnapshotHash=snapshot_hash,
        assumptions={
            "selection": "lowest-priced unexpired compatible offer; validators use distinct peers",
            "requiredSkillHash": required_skill_hash,
            "requiredModelDigest": required_model_digest,
            "requiredRuntimeDigest": required_runtime_digest,
            "requiredVerificationStrategy": required_verification_strategy,
            "requiredResources": required_resources.model_dump(mode="json") if required_resources else None,
            "offerSources": [offer.offerSource for offer in selected_offers],
            "caller": assumptions or {},
        },
        sampleCount=sample_count,
        validatorCount=validator_count,
        failureRules={
            "executionTimeout": "refund-execution-reward",
            "verificationFailure": "refund-execution-reward-pay-valid-receipts",
            "unresolvedDispute": "refund-after-audit-deadline",
            "ratingAffectsPayment": False,
            "unspentBudget": "refund-to-customer",
        },
        verificationPolicy={
            "sampleCount": sample_count,
            "validatorCount": validator_count,
            "challengeAfterCommit": True,
            "disputeOnMismatch": True,
            "unresolvedOutcome": "INCONCLUSIVE",
        },
    )

