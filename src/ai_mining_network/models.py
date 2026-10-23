"""Version 2 wire models. Numeric money and measurements use integer units."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    ramBytes: int = Field(default=1_073_741_824, ge=1)
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


class SkillBenchmark(WireModel):
    kind: Literal["SkillBenchmark"] = "SkillBenchmark"
    benchmarkVersion: str = "invoice-synthetic-v1"
    skillHash: str
    datasetHash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runtime: str
    modelDigest: str
    hardwareClass: str
    sampleCount: int = Field(ge=1)
    inputTokens: int = Field(ge=0)
    outputTokens: int = Field(ge=0)
    p50DurationMs: int = Field(ge=0)
    p90DurationMs: int = Field(ge=0)
    cpuAverageMilliPct: int | None = Field(default=None, ge=0)
    peakRamBytes: int | None = Field(default=None, ge=0)
    peakVramBytes: int | None = Field(default=None, ge=0)
    successRateBps: int = Field(ge=0, le=10_000)
    outputValidationPasses: int = Field(ge=0)
    selfReportedMetrics: bool = True
    receiptId: str


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


class MinerOffer(WireModel):
    kind: Literal["MinerOffer"] = "MinerOffer"
    offerId: str
    peerId: str
    skillHash: str
    priceAtomic: int = Field(ge=0)
    currency: Currency
    estimatedDurationMs: int = Field(ge=0)
    expiresAt: datetime
    requirements: ResourceRequirements = Field(default_factory=ResourceRequirements)


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
    maxPriceAtomic: int = Field(ge=0)
    currency: Currency
    deadline: datetime
    verificationSampleCount: int = Field(ge=1)
    validatorCount: int = Field(ge=1)


class FixedPriceQuote(WireModel):
    kind: Literal["FixedPriceQuote"] = "FixedPriceQuote"
    quoteId: str
    taskId: str
    executionRewardAtomic: int = Field(ge=0)
    verificationBudgetAtomic: int = Field(ge=0)
    protocolFeeAtomic: int = Field(default=0, ge=0)
    totalAtomic: int = Field(ge=0)
    currency: Currency
    expiresAt: datetime
    executorOfferId: str
    validatorOfferIds: list[str] = Field(min_length=1)
    sampleCount: int = Field(ge=1)
    validatorCount: int = Field(ge=1)
    failureRules: dict
    verificationPolicy: dict


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
    amountAtomic: int = Field(ge=0)
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
    totalChargedAtomic: int = Field(ge=0)
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

