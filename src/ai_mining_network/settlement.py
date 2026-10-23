"""Idempotent local settlement policy adapter."""

from __future__ import annotations

from .canonical import content_hash
from .models import Currency, Payout, PaymentSettlement
from .state_machine import EventLog


def create_settlement(
    task_id: str,
    quote_id: str,
    idempotency_key: str,
    outcome: str,
    currency: Currency,
    execution_reward: int,
    verification_budget: int,
    miner: str,
    validator_prices: dict[str, int],
    valid_validator_peers: set[str],
    decision_hash: str,
) -> PaymentSettlement:
    payouts: list[Payout] = []
    total = execution_reward + verification_budget
    if outcome == "VERIFIED":
        payouts.append(Payout(payee=miner, amountAtomic=execution_reward, purpose="execution"))
    else:
        payouts.append(Payout(payee="customer", amountAtomic=execution_reward, purpose="refund"))
    # A validator receives its accepted offer price only after a valid receipt.
    paid = 0
    for validator, amount in validator_prices.items():
        if validator not in valid_validator_peers:
            amount = 0
        if amount:
            payouts.append(Payout(payee=validator, amountAtomic=amount, purpose="verification"))
            paid += amount
    remainder = verification_budget - paid
    if remainder:
        payouts.append(Payout(payee="customer", amountAtomic=remainder, purpose="refund"))
    charged = sum(payout.amountAtomic for payout in payouts if payout.payee != "customer")
    settlement_outcome = "SETTLED" if outcome == "VERIFIED" else "REFUNDED"
    return PaymentSettlement(
        taskId=task_id,
        quoteId=quote_id,
        settlementId=content_hash({"taskId": task_id, "decisionHash": decision_hash}),
        idempotencyKey=idempotency_key,
        outcome=settlement_outcome,
        currency=currency,
        payouts=payouts,
        totalChargedAtomic=charged,
        decisionHash=decision_hash,
    )


def settle_once(event_log: EventLog, settlement: PaymentSettlement) -> tuple[dict, bool]:
    payload = settlement.model_dump(mode="json", exclude_none=True)
    payload_hash = content_hash(payload)
    stored, applied = event_log.store_settlement(
        settlement.idempotencyKey, settlement.taskId, payload_hash, payload
    )
    if applied:
        event_log.append(settlement.taskId, "payment.settled", stored)
    return stored, applied

