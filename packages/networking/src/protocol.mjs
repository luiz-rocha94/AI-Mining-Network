import { readFile } from "node:fs/promises";
import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import {
  canonicalJsonBytes,
  canonicalJsonString,
  validateEnvelope,
} from "@ai-mining-network/protocol";

const schemaUrl = new URL("../../protocol/v2/messages.schema.json", import.meta.url);
const protocolSchema = JSON.parse(await readFile(schemaUrl, "utf8"));
const ajv = new Ajv2020({ allErrors: false, strict: false, validateFormats: true });
addFormats(ajv);
const validateSchema = ajv.compile({
  ...protocolSchema,
  ...protocolSchema.$defs.SignedMessage,
});

export const PUBLIC_TOPIC = "amn/protocol/v2/public";
export const RPC_PROTOCOL = "/amn/protocol/2.0.0/peer-rpc";
export const MAX_PUBLIC_MESSAGE_BYTES = 256 * 1024;
export const MAX_DIRECT_FRAME_BYTES = 512 * 1024;
export const PUBLIC_MESSAGE_KINDS = new Set([
  "SkillManifest",
  "SkillBenchmark",
  "MinerCapabilities",
  "ValidatorCapabilities",
  "MinerOffer",
  "FixedPriceQuote",
  "ExecutionContract",
  "ExecutionCommitment",
  "VerificationAssignment",
  "VerificationReceipt",
  "VerificationDecision",
  "PaymentSettlement",
  "SkillRating",
  "IdentityTransition",
]);

export class PeerTransportError extends Error {
  constructor(code, message, options = {}) {
    super(message, options);
    this.name = "PeerTransportError";
    this.code = code;
  }
}

function schemaError() {
  const detail = validateSchema.errors?.[0];
  return new PeerTransportError(
    "INVALID_MESSAGE_SCHEMA",
    detail ? `${detail.instancePath || "/"} ${detail.message}` : "message does not match protocol v2 schema",
  );
}

export function serializeEnvelope(envelope, maxBytes = MAX_PUBLIC_MESSAGE_BYTES) {
  let bytes;
  try {
    bytes = canonicalJsonBytes(envelope);
  } catch (error) {
    throw new PeerTransportError("NON_CANONICAL_MESSAGE", error.message, { cause: error });
  }
  if (bytes.byteLength > maxBytes) {
    throw new PeerTransportError("MESSAGE_TOO_LARGE", `message exceeds ${maxBytes} byte limit`);
  }
  if (!validateSchema(envelope)) throw schemaError();
  return bytes;
}

export function parseCanonicalEnvelope(data, maxBytes = MAX_PUBLIC_MESSAGE_BYTES) {
  const bytes = Buffer.from(data);
  if (bytes.byteLength > maxBytes) {
    throw new PeerTransportError("MESSAGE_TOO_LARGE", `message exceeds ${maxBytes} byte limit`);
  }
  let envelope;
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    envelope = JSON.parse(text);
  } catch (error) {
    throw new PeerTransportError("INVALID_JSON", "message is not valid UTF-8 JSON", { cause: error });
  }
  if (!validateSchema(envelope)) throw schemaError();
  let canonical;
  try {
    canonical = canonicalJsonBytes(envelope);
  } catch (error) {
    throw new PeerTransportError("NON_CANONICAL_MESSAGE", error.message, { cause: error });
  }
  if (!Buffer.from(canonical).equals(bytes)) {
    throw new PeerTransportError("NON_CANONICAL_MESSAGE", "wire JSON must use protocol v2 canonical serialization");
  }
  return envelope;
}

export async function validateSignedEnvelope(envelope, options = {}) {
  if (!validateSchema(envelope)) throw schemaError();
  try {
    await validateEnvelope(envelope, {
      chainId: options.chainId ?? 31337,
      now: options.now ?? Date.now(),
      maxLifetimeMs: options.maxLifetimeMs,
      maxClockSkewMs: options.maxClockSkewMs,
      identityResolver: options.identityResolver,
      replayStore: options.replayStore,
    });
  } catch (error) {
    throw new PeerTransportError(error.code ?? "INVALID_ENVELOPE", error.message, { cause: error });
  }
  return envelope;
}

export function assertPublicEnvelope(envelope) {
  if (!PUBLIC_MESSAGE_KINDS.has(envelope?.payload?.kind)) {
    throw new PeerTransportError(
      "PRIVATE_MESSAGE_REQUIRES_UNICAST",
      `${envelope?.payload?.kind ?? "unknown"} is private by default and cannot be broadcast`,
    );
  }
  return envelope;
}

export function assertPrivateEnvelope(envelope) {
  if (PUBLIC_MESSAGE_KINDS.has(envelope?.payload?.kind)) {
    throw new PeerTransportError(
      "PUBLIC_MESSAGE_KIND",
      `${envelope.payload.kind} is a public protocol message; publish it to the public topic`,
    );
  }
  return envelope;
}

export function canonicalEnvelopeString(envelope) {
  return canonicalJsonString(envelope);
}
