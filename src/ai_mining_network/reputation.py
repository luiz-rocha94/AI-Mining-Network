"""Reputation aggregators with independent, payment-neutral dimensions."""

from __future__ import annotations

from collections import defaultdict

from .models import SignedMessage
from .signing import verify_message


def aggregate_skill_quality(messages: list[SignedMessage]) -> dict:
    """Deduplicate a signer's rating per task and validate every signature."""
    seen: set[tuple[str, str]] = set()
    accepted = []
    rejected = 0
    for message in messages:
        if message.payload.kind != "SkillRating" or not verify_message(message):
            rejected += 1
            continue
        rating = message.payload
        key = (message.signer.lower(), rating.taskId)
        if key in seen:
            rejected += 1
            continue
        seen.add(key)
        accepted.append(rating.stars)
    return {
        "ratingCount": len(accepted),
        "rejectedOrDuplicateCount": rejected,
        "averageStarsMilli": round(sum(accepted) * 1000 / len(accepted)) if accepted else None,
        "qualityScoreBps": round(sum(accepted) * 10_000 / (5 * len(accepted))) if accepted else None,
        "paymentEffect": "NONE",
    }


def aggregate_miner_reliability(executions: list[dict]) -> dict:
    """Compute reliability from objective task outcomes only, never ratings."""
    terminal = [item for item in executions if item.get("outcome") in {"VERIFIED", "FAILED", "TIMEOUT", "FRAUD"}]
    successes = sum(item["outcome"] == "VERIFIED" for item in terminal)
    failures = sum(item["outcome"] in {"FAILED", "FRAUD"} for item in terminal)
    timeouts = sum(item["outcome"] == "TIMEOUT" for item in terminal)
    return {
        "objectiveTaskCount": len(terminal),
        "verifiedCount": successes,
        "objectiveFailureCount": failures,
        "timeoutCount": timeouts,
        "reliabilityBps": round(10_000 * successes / len(terminal)) if terminal else None,
        "customerRatingInputs": 0,
    }


def aggregate_validator_reliability(receipt_agreement: dict[str, bool]) -> dict:
    """Compute agreement against later adjudication, excluding timeouts from penalty."""
    observations = list(receipt_agreement.values())
    agreements = sum(observations)
    return {
        "adjudicatedReceiptCount": len(observations),
        "agreementCount": agreements,
        "disagreementCount": len(observations) - agreements,
        "reliabilityBps": round(10_000 * agreements / len(observations)) if observations else None,
    }


def aggregate_benchmark_contributors(submissions: list[dict]) -> dict:
    """Contributor credibility requires valid signatures and later reproduction."""
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for item in submissions:
        raw = item.get("message")
        try:
            message = raw if isinstance(raw, SignedMessage) else SignedMessage.model_validate(raw)
            signature_valid = verify_message(message) and message.payload.kind == "SkillBenchmark"
            address = message.signer.lower()
        except Exception:
            address = str(item.get("signer", "")).lower()
            signature_valid = False
        if not address:
            continue
        totals[address][0] += 1
        if signature_valid and item.get("reproduced"):
            totals[address][1] += 1
    return {
        address: {
            "submissionCount": values[0],
            "reproducedCount": values[1],
            "credibilityBps": round(10_000 * values[1] / values[0]),
        }
        for address, values in sorted(totals.items())
    }
