"""Fixed quote calculation. All money is integer atomic currency units."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .models import Currency, FixedPriceQuote, MinerOffer


class PricingError(ValueError):
    pass


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
) -> FixedPriceQuote:
    if sample_count < 1 or validator_count < 1 or max_price_atomic < 0 or quote_ttl_seconds < 1:
        raise PricingError("sample count, validator count, quote expiry, and price cap must be valid")
    now = datetime.now(timezone.utc)
    compatible_executors = [
        offer for offer in executor_offers
        if offer.expiresAt > now and offer.currency == currency
        and (required_skill_hash is None or offer.skillHash == required_skill_hash)
    ]
    if not compatible_executors:
        raise PricingError("no compatible executor offers are available")
    executor = min(compatible_executors, key=lambda offer: (offer.priceAtomic, offer.offerId))
    validators = []
    seen_peers = {executor.peerId.lower()}
    for offer in sorted(validator_offers, key=lambda item: (item.priceAtomic, item.offerId)):
        peer = offer.peerId.lower()
        if offer.expiresAt <= now or offer.currency != currency:
            continue
        if required_skill_hash is not None and offer.skillHash != required_skill_hash:
            continue
        if peer in seen_peers:
            continue
        seen_peers.add(peer)
        validators.append(offer)
        if len(validators) == validator_count:
            break
    if len(validators) < validator_count:
        raise PricingError(f"need {validator_count} distinct compatible validator offers; found {len(validators)}")
    execution = executor.priceAtomic
    verification = sum(offer.priceAtomic for offer in validators)
    protocol_fee = 0
    total = execution + verification + protocol_fee
    if total > max_price_atomic:
        raise PricingError(f"fixed quote {total} exceeds customer maximum {max_price_atomic}")
    return FixedPriceQuote(
        quoteId=str(uuid4()),
        taskId=task_id,
        executionRewardAtomic=execution,
        verificationBudgetAtomic=verification,
        protocolFeeAtomic=protocol_fee,
        totalAtomic=total,
        currency=currency,
        expiresAt=min([now + timedelta(seconds=quote_ttl_seconds), executor.expiresAt, *(offer.expiresAt for offer in validators)]),
        executorOfferId=executor.offerId,
        validatorOfferIds=[offer.offerId for offer in validators],
        sampleCount=sample_count,
        validatorCount=validator_count,
        failureRules={
            "executionTimeout": "refund-execution-reward",
            "verificationFailure": "refund-execution-reward-pay-valid-receipts",
            "unresolvedDispute": "refund-after-audit-deadline",
            "ratingAffectsPayment": False,
        },
        verificationPolicy={
            "sampleCount": sample_count,
            "validatorCount": validator_count,
            "challengeAfterCommit": True,
            "disputeOnMismatch": True,
            "unresolvedOutcome": "INCONCLUSIVE",
        },
    )

