"""Export the active v2 JSON schema for all protocol payloads."""

from __future__ import annotations

import json
from pathlib import Path

from .models import (
    BenchmarkMeasurementProvenance,
    Currency,
    CustomerTask,
    ExecutionCommitment,
    ExecutionContract,
    ExecutionUnit,
    FixedPriceQuote,
    IdentityTransition,
    MinerCapabilities,
    MinerOffer,
    OfferCapabilities,
    PaymentSettlement,
    ResourceRequirements,
    SignedMessage,
    SkillBenchmark,
    SkillManifest,
    SkillRating,
    ValidatorCapabilities,
    VerificationAssignment,
    VerificationDecision,
    VerificationReceipt,
)

MODELS = [
    Currency,
    ResourceRequirements,
    SkillManifest,
    BenchmarkMeasurementProvenance,
    SkillBenchmark,
    MinerCapabilities,
    ValidatorCapabilities,
    OfferCapabilities,
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
    IdentityTransition,
    SignedMessage,
]


def build_schema() -> dict:
    entries = []
    for model in MODELS:
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        entries.append((model, schema))
    definitions = {model.__name__: schema for model, schema in entries}
    model_names = set(definitions)
    # Keep Pydantic's local definitions for compatibility. Shared models are
    # also top-level definitions so root-targeted $refs always resolve.
    for _, schema in entries:
        for name, definition in schema.get("$defs", {}).items():
            if name not in model_names:
                definitions.setdefault(name, definition)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://ai-mining-network.example/protocol/v2/messages.schema.json",
        "title": "AI Mining Network protocol v2",
        "description": "Signed envelopes wrap typed payloads; canonical JSON and EIP-712 rules are specified in docs/protocol.md.",
        "$defs": definitions,
    }


def main() -> None:
    target = Path("packages/protocol/v2/messages.schema.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(build_schema(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {target}")


if __name__ == "__main__":
    main()
