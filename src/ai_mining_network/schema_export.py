"""Export versioned JSON schemas for all v1 protocol payloads."""

from __future__ import annotations

import json
from pathlib import Path

from .models import (
    CustomerTask,
    ExecutionCommitment,
    ExecutionContract,
    ExecutionUnit,
    FixedPriceQuote,
    MinerCapabilities,
    MinerOffer,
    PaymentSettlement,
    SkillBenchmark,
    SkillManifest,
    SkillRating,
    SignedMessage,
    VerificationAssignment,
    VerificationDecision,
    VerificationReceipt,
    ValidatorCapabilities,
)

MODELS = [
    SkillManifest,
    SkillBenchmark,
    MinerCapabilities,
    ValidatorCapabilities,
    MinerOffer,
    CustomerTask,
    FixedPriceQuote,
    ExecutionContract,
    ExecutionUnit,
    ExecutionCommitment,
    VerificationAssignment,
    VerificationReceipt,
    VerificationDecision,
    PaymentSettlement,
    SkillRating,
    SignedMessage,
]


def build_schema() -> dict:
    definitions = {}
    for model in MODELS:
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        definitions[model.__name__] = schema
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://ai-mining-network.example/protocol/v1/messages.schema.json",
        "title": "AI Mining Network protocol v1",
        "description": "Signed envelopes wrap typed payloads; canonical JSON and EIP-712 rules are specified in docs/protocol.md.",
        "$defs": definitions,
    }


def main() -> None:
    target = Path("packages/protocol/v1/messages.schema.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(build_schema(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {target}")


if __name__ == "__main__":
    main()
