"""Version 2 wire models. Numeric money and measurements use integer units."""

from __future__ import annotations

import math
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Protocol v2 represents atomics as JSON integers consumed by JS as well as
# Python. The safe-integer ceiling avoids silent rounding in cross-language
# quote approval; a future protocol can move larger amounts to decimal strings.
MAX_ATOMIC = 2**53 - 1
SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Currency(WireModel):
    chainId: int = Field(gt=0)
    tokenAddress: str
    decimals: int = Field(ge=0, le=36)
    symbol: str


class ResourceRequirements(WireModel):
    cpuCores: int = Field(default=1, ge=1)
    cpuThreads: int | None = Field(default=None, ge=1)
    ramBytes: int = Field(default=1_073_741_824, ge=1)
    storageBytes: int | None = Field(default=None, ge=1)
    vramBytes: int = Field(default=0, ge=0)
    gpuRequired: bool = False
    networkAccess: bool = False
    timeoutSeconds: int = Field(default=600, ge=1, le=86_400)


class SkillManifest(WireModel):
    kind: Literal["SkillManifest"] = "SkillManifest"
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    version: str = Field(min_length=1, max_length=64)
    contentHash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    skillFile: str = "SKILL.md"
    inputSchema: dict
    outputSchema: dict
    requiredModels: list[str] = Field(default_factory=list)
    allowedTools: list[str] = Field(default_factory=list)
    verificationStrategy: Literal["per-record", "per-document", "per-image", "deterministic-batch"]
    requirements: ResourceRequirements = Field(default_factory=ResourceRequirements)
    publicBenchmarkIds: list[str] = Field(default_factory=list)


class BenchmarkMeasurementProvenance(WireModel):
    """Self-reported environment and collector details; not hardware attestation."""

    measurementMode: Literal[
        "self-reported", "independent-observer", "trusted-hardware-attestation"
    ] = "self-reported"
    collectorVersion: str = Field(min_length=1, max_length=64)
    measurementId: str = Field(min_length=1, max_length=128)
    environmentHash: str = Field(pattern=SHA256_PATTERN)
    limitations: list[str] = Field(default_factory=list, max_length=32)


class SkillBenchmark(WireModel):
    kind: Literal["SkillBenchmark"] = "SkillBenchmark"
    benchmarkVersion: str = "invoice-synthetic-v2"
    skillHash: str = Field(pattern=SHA256_PATTERN)
    datasetHash: str = Field(pattern=SHA256_PATTERN)
    runtime: str
    runtimeDigest: str = Field(pattern=SHA256_PATTERN)
    modelDigest: str
    hardwareClass: str
    warmColdPolicy: Literal[
        "warm-after-single-warmup", "cold-per-sample", "natural-cache-state"
    ]
    sampleCount: int = Field(ge=20)
    durationSamplesMs: list[int] = Field(min_length=20, max_length=100_000)
    durationVarianceMs2: int = Field(ge=0)
    outputValidationResults: list[bool] = Field(min_length=20, max_length=100_000)
    inputTokens: int = Field(ge=0)
    outputTokens: int = Field(ge=0)
    p50DurationMs: int = Field(ge=0)
    p90DurationMs: int = Field(ge=0)
    cpuAverageMilliPct: int | None = Field(default=None, ge=0)
    cpuSamplesMilliPct: list[int] | None = Field(default=None, min_length=20, max_length=100_000)
    peakRamBytes: int | None = Field(default=None, ge=0)
    ramSamplesBytes: list[int] | None = Field(default=None, min_length=20, max_length=100_000)
    peakVramBytes: int | None = Field(default=None, ge=0)
    successRateBps: int = Field(ge=0, le=10_000)
    outputValidationPasses: int = Field(ge=0)
    measurementProvenance: BenchmarkMeasurementProvenance
    selfReportedMetrics: bool = True
    receiptId: str

    @model_validator(mode="after")
    def validate_observations(self):
        if self.sampleCount != len(self.durationSamplesMs):
            raise ValueError("sampleCount must match durationSamplesMs")
        if self.sampleCount != len(self.outputValidationResults):
            raise ValueError("sampleCount must match outputValidationResults")
        if any(duration < 0 for duration in self.durationSamplesMs):
            raise ValueError("duration samples cannot be negative")
        ordered = sorted(self.durationSamplesMs)
        p50 = ordered[max(0, min(len(ordered) - 1, math.ceil(0.50 * len(ordered)) - 1))]
        p90 = ordered[max(0, min(len(ordered) - 1, math.ceil(0.90 * len(ordered)) - 1))]
        if self.p50DurationMs != p50 or self.p90DurationMs != p90:
            raise ValueError("P50/P90 must be recomputed from the signed duration samples")
        expected_variance = round(statistics.pvariance(self.durationSamplesMs))
        if self.durationVarianceMs2 != expected_variance:
            raise ValueError("durationVarianceMs2 does not match signed duration samples")
        passes = sum(self.outputValidationResults)
        if self.outputValidationPasses != passes:
            raise ValueError("outputValidationPasses does not match signed validation results")
        if self.successRateBps != round(10_000 * passes / self.sampleCount):
            raise ValueError("successRateBps does not match signed validation results")
        if self.selfReportedMetrics != (self.measurementProvenance.measurementMode == "self-reported"):
            raise ValueError("selfReportedMetrics must agree with measurementProvenance")
        if self.cpuSamplesMilliPct is not None:
            if len(self.cpuSamplesMilliPct) != self.sampleCount:
                raise ValueError("cpuSamplesMilliPct must match sampleCount")
            if self.cpuAverageMilliPct != round(statistics.mean(self.cpuSamplesMilliPct)):
                raise ValueError("cpuAverageMilliPct does not match signed CPU samples")
        elif self.cpuAverageMilliPct is not None:
            raise ValueError("cpuAverageMilliPct requires its per-sample observations")
        if self.ramSamplesBytes is not None:
            if len(self.ramSamplesBytes) != self.sampleCount:
                raise ValueError("ramSamplesBytes must match sampleCount")
            if self.peakRamBytes != max(self.ramSamplesBytes):
                raise ValueError("peakRamBytes does not match signed RSS observations")
        return self


class MinerCapabilities(WireModel):
    kind: Literal["MinerCapabilities"] = "MinerCapabilities"
    peerId: str
    paymentAddress: str
    cpuCores: int = Field(ge=1)
    ramBytes: int = Field(ge=1)
    gpuModels: list[str] = Field(default_factory=list)
    vramBytes: int = Field(default=0, ge=0)
    modelDigests: list[str] = Field(default_factory=list)
    skillHashes: list[str] = Field(default_factory=list)
    supportedRuntimes: list[str] = Field(default_factory=list)
    available: bool = True


class ValidatorCapabilities(WireModel):
    kind: Literal["ValidatorCapabilities"] = "ValidatorCapabilities"
    peerId: str
    paymentAddress: str
    verificationStrategies: list[str] = Field(min_length=1)
    modelDigests: list[str] = Field(default_factory=list)
    maxUnitsPerTask: int = Field(default=100, ge=1)
    available: bool = True


class OfferCapabilities(WireModel):
    """Capacity advertised by an offer; signed claims are not hardware attestation."""

    available: bool = True
    cpuCores: int = Field(default=0, ge=0)
    ramBytes: int = Field(default=0, ge=0)
    gpuModels: list[str] = Field(default_factory=list)
    vramBytes: int = Field(default=0, ge=0)
    modelDigests: list[str] = Field(default_factory=list)
    runtimeDigests: list[str] = Field(default_factory=list)
    verificationStrategies: list[str] = Field(default_factory=list)
    supportsNetworkAccess: bool = False


class MinerOffer(WireModel):
    kind: Literal["MinerOffer"] = "MinerOffer"
    offerId: str
    peerId: str
    skillHash: str
    priceAtomic: int = Field(strict=True, ge=0, le=MAX_ATOMIC)
    currency: Currency
    estimatedDurationMs: int = Field(ge=0)
    expiresAt: datetime
    requirements: ResourceRequirements = Field(default_factory=ResourceRequirements)
    offerSource: Literal["local-fixture", "provider-offer"] = "local-fixture"
    availableResources: OfferCapabilities | None = None


class CustomerTask(WireModel):
    kind: Literal["CustomerTask"] = "CustomerTask"
    taskId: str
    skillHash: str
    workflowHash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    inputSetHash: str
    promptHash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    outputSchemaHash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    modelName: str | None = None
    modelDigest: str | None = None
    inputCount: int = Field(ge=1)
    partitioning: Literal["per-record", "per-document", "per-image", "deterministic-batch"]
    requestedOutput: str
    maxPriceAtomic: int = Field(strict=True, ge=0, le=MAX_ATOMIC)
    currency: Currency
    deadline: datetime
    verificationSampleCount: int = Field(ge=1)
    validatorCount: int = Field(ge=1)


class FixedPriceQuote(WireModel):
    kind: Literal["FixedPriceQuote"] = "FixedPriceQuote"
    quoteId: str
    taskId: str
    executionRewardAtomic: int = Field(strict=True, ge=0, le=MAX_ATOMIC)
    verificationBudgetAtomic: int = Field(strict=True, ge=0, le=MAX_ATOMIC)
    protocolFeeAtomic: int = Field(default=0, strict=True, ge=0, le=MAX_ATOMIC)
    storageBudgetAtomic: int = Field(default=0, strict=True, ge=0, le=MAX_ATOMIC)
    settlementBudgetAtomic: int = Field(default=0, strict=True, ge=0, le=MAX_ATOMIC)
    totalAtomic: int = Field(strict=True, ge=0, le=MAX_ATOMIC)
    currency: Currency
    expiresAt: datetime
    executorOfferId: str
    validatorOfferIds: list[str] = Field(min_length=1)
    priceBasis: Literal["compatible-offers", "local-fixture"] = "local-fixture"
    offerSnapshotHash: str = Field(pattern=SHA256_PATTERN)
    assumptions: dict = Field(default_factory=dict)
    sampleCount: int = Field(ge=1)
    validatorCount: int = Field(ge=1)
    failureRules: dict
    verificationPolicy: dict

    @model_validator(mode="after")
    def validate_total(self):
        components = (
            self.executionRewardAtomic
            + self.verificationBudgetAtomic
            + self.protocolFeeAtomic
            + self.storageBudgetAtomic
            + self.settlementBudgetAtomic
        )
        if components > MAX_ATOMIC:
            raise ValueError("quote components overflow uint256 atomic arithmetic")
        if self.totalAtomic != components:
            raise ValueError("totalAtomic must equal the sum of every quoted cost component")
        if len(set(self.validatorOfferIds)) != len(self.validatorOfferIds):
            raise ValueError("validator offer IDs must be distinct")
        if self.validatorCount != len(self.validatorOfferIds):
            raise ValueError("validatorCount must match validatorOfferIds")
        return self


class ComputeBackendBinding(WireModel):
    """Terms accepted by the customer before a compute provider is contacted."""

    backend: Literal["local-fixture", "golem-yagna"]
    selectedOfferId: str = Field(min_length=1, max_length=256)
    selectedProviderId: str = Field(min_length=1, max_length=256)
    offerSnapshotHash: str = Field(pattern=SHA256_PATTERN)
    acceptedQuoteTotalAtomic: int = Field(strict=True, ge=0, le=MAX_ATOMIC)
    executionRewardAtomic: int = Field(strict=True, ge=0, le=MAX_ATOMIC)
    currency: Currency
    runtimeName: str = Field(min_length=1, max_length=128)
    runtimeVersion: str = Field(min_length=1, max_length=128)
    imageDigest: str | None = Field(default=None, pattern=r"^sha3-224:[0-9a-f]{56}$")
    inputHash: str = Field(pattern=SHA256_PATTERN)
    resourceRequirements: ResourceRequirements
    deadline: datetime
    # Golem bills in its configured native token. This string is an exact decimal
    # GLM cap; it is deliberately separate from AMN's accepted ERC-20 quote.
    nativeCostCapGlm: str | None = Field(
        default=None, pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,18})?$"
    )

    @model_validator(mode="after")
    def validate_backend_terms(self):
        if self.deadline.tzinfo is None or self.deadline.utcoffset() != timedelta(0):
            raise ValueError("compute backend deadline must be timezone-aware UTC")
        if self.backend == "golem-yagna":
            if self.imageDigest is None:
                raise ValueError("Golem dispatch requires a pinned SHA3-224 VM image digest")
            if (
                self.nativeCostCapGlm is None
                or not math.isfinite(float(self.nativeCostCapGlm))
                or float(self.nativeCostCapGlm) <= 0
            ):
                raise ValueError("Golem dispatch requires a positive native GLM spend cap")
        elif self.imageDigest is not None or self.nativeCostCapGlm is not None:
            raise ValueError("local fixture bindings cannot claim Golem image or payment terms")
        return self


class ExecutionContract(WireModel):
    kind: Literal["ExecutionContract"] = "ExecutionContract"
    contractId: str
    taskId: str
    quoteId: str
    executorPeerId: str
    acceptedAt: datetime
    commitDeadline: datetime
    challengeRound: int = Field(ge=1)
    idempotencyKey: str
    # Optional only for historical v2 receipts; all new AMN executions populate
    # the binding so backend selection and quote terms are signed before dispatch.
    backendBinding: ComputeBackendBinding | None = None


class ExecutionUnit(WireModel):
    unitId: str
    inputHash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    outputHash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    inputRef: str
    outputRef: str


class ExecutionCommitment(WireModel):
    kind: Literal["ExecutionCommitment"] = "ExecutionCommitment"
    taskId: str
    contractId: str
    merkleRoot: str = Field(pattern=r"^0x[0-9a-f]{64}$")
    outputArtifactHash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    unitCount: int = Field(ge=1)
    leafEncoding: list[str]
    committedAt: datetime
    challengeRound: int = Field(ge=1)


class VerificationAssignment(WireModel):
    kind: Literal["VerificationAssignment"] = "VerificationAssignment"
    taskId: str
    commitmentRoot: str
    randomnessRound: int = Field(ge=1)
    randomnessValue: str
    randomnessVerified: bool
    validatorPeerId: str
    unitIds: list[str] = Field(min_length=1)
    assignmentIndex: int = Field(ge=0)


class VerificationReceipt(WireModel):
    kind: Literal["VerificationReceipt"] = "VerificationReceipt"
    receiptId: str
    taskId: str
    commitmentRoot: str
    validatorPeerId: str
    unitId: str
    result: Literal["PASS", "FAIL", "INCONCLUSIVE", "TIMEOUT", "DISPUTE"]
    observedOutputHash: str | None = None
    evidenceHash: str
    merkleProof: list[str]
    reasonCode: str
    modelDigest: str
    durationMs: int = Field(ge=0)


class VerificationDecision(WireModel):
    kind: Literal["VerificationDecision"] = "VerificationDecision"
    taskId: str
    outcome: Literal["VERIFIED", "FAILED", "INCONCLUSIVE", "DISPUTED"]
    reasonCode: str
    explanation: str
    acceptedReceiptIds: list[str]
    rejectedReceiptIds: list[str]
    auditRequired: bool


class Payout(WireModel):
    payee: str
    amountAtomic: int = Field(strict=True, ge=0, le=MAX_ATOMIC)
    purpose: Literal["execution", "verification", "protocol-fee", "refund"]


class PaymentSettlement(WireModel):
    kind: Literal["PaymentSettlement"] = "PaymentSettlement"
    taskId: str
    quoteId: str
    settlementId: str
    idempotencyKey: str
    outcome: Literal["SETTLED", "REFUNDED", "DISPUTED"]
    currency: Currency
    payouts: list[Payout]
    totalChargedAtomic: int = Field(strict=True, ge=0, le=MAX_ATOMIC)
    decisionHash: str


class SkillRating(WireModel):
    kind: Literal["SkillRating"] = "SkillRating"
    ratingId: str
    taskId: str
    skillHash: str
    stars: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=1000)


class IdentityTransition(WireModel):
    """Signed identity-directory update; policy and authorization live in identity_store."""

    kind: Literal["IdentityTransition"] = "IdentityTransition"
    identityId: str = Field(min_length=1, max_length=128, pattern=r"^[-a-zA-Z0-9:._]+$")
    sequence: int = Field(ge=1)
    action: Literal["ROTATE", "REVOKE", "RECOVER"]
    previousSigner: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    newSigner: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{40}$")


Payload = Annotated[
    SkillManifest
    | SkillBenchmark
    | MinerCapabilities
    | ValidatorCapabilities
    | MinerOffer
    | CustomerTask
    | FixedPriceQuote
    | ExecutionContract
    | ExecutionCommitment
    | VerificationAssignment
    | VerificationReceipt
    | VerificationDecision
    | PaymentSettlement
    | SkillRating
    | IdentityTransition,
    Field(discriminator="kind"),
]


class SignedMessage(WireModel):
    protocolVersion: Literal["2"] = "2"
    id: str = Field(default_factory=lambda: str(uuid4()))
    createdAt: datetime = Field(default_factory=utc_now, json_schema_extra={"pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"})
    expiresAt: datetime = Field(json_schema_extra={"pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"})
    principalId: str = Field(min_length=1, max_length=128, pattern=r"^[-a-zA-Z0-9:._]+$")
    keyEpoch: int = Field(ge=0, le=9_007_199_254_740_991)
    signer: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    nonce: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")
    payload: Payload
    payloadHash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    signature: str = Field(pattern=r"^0x[0-9a-fA-F]{130}$")

    @field_validator("createdAt", "expiresAt", mode="before")
    @classmethod
    def wire_timestamps_are_canonical_utc(cls, value):
        if isinstance(value, str) and not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z",
            value,
        ):
            raise ValueError("wire timestamps must be UTC RFC3339 ending in Z")
        return value

    @field_validator("createdAt", "expiresAt")
    @classmethod
    def protocol_timestamps_are_utc_milliseconds(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("protocol timestamps must be timezone-aware UTC")
        if value.microsecond % 1000:
            raise ValueError("protocol timestamps must have millisecond precision")
        return value

    @field_validator("nonce")
    @classmethod
    def nonce_is_bytes32(cls, value: str) -> str:
        if len(value) != 66 or not value.startswith("0x"):
            raise ValueError("nonce must be a 32-byte 0x-prefixed hex value")
        try:
            bytes.fromhex(value[2:])
        except ValueError as exc:
            raise ValueError("nonce must be a 32-byte 0x-prefixed hex value") from exc
        return value.lower()

    @model_validator(mode="after")
    def expiry_follows_creation(self) -> SignedMessage:
        if self.expiresAt <= self.createdAt:
            raise ValueError("expiresAt must be later than createdAt")
        return self

