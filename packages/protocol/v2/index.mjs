import { createHash } from "node:crypto";
import { TypedDataEncoder, verifyTypedData } from "ethers";

export const PROTOCOL_VERSION = "2";
export const SUPPORTED_PROTOCOL_VERSIONS = new Set([PROTOCOL_VERSION]);
export const EIP712_DOMAIN = { name: "AI Mining Network", version: "2" };
export const MESSAGE_TYPES = {
  ProtocolMessage: [
    { name: "protocolVersion", type: "uint16" },
    { name: "messageId", type: "bytes32" },
    { name: "createdAtMs", type: "uint64" },
    { name: "expiresAtMs", type: "uint64" },
    { name: "principalId", type: "string" },
    { name: "signer", type: "address" },
    { name: "keyEpoch", type: "uint64" },
    { name: "messageType", type: "string" },
    { name: "payloadHash", type: "bytes32" },
    { name: "nonce", type: "bytes32" },
  ],
};

export class ProtocolValidationError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "ProtocolValidationError";
    this.code = code;
  }
}

export function negotiateProtocolVersion(localVersions, remoteVersions) {
  const local = new Set(localVersions);
  const remote = new Set(remoteVersions);
  if ([...local, ...remote].some((value) => typeof value !== "string" || !/^\d+$/.test(value))) {
    throw new TypeError("protocol versions must be decimal major-version strings");
  }
  const common = [...local].filter((version) => remote.has(version) && SUPPORTED_PROTOCOL_VERSIONS.has(version));
  if (common.length === 0) throw new ProtocolValidationError("NO_COMMON_VERSION", "no mutually supported protocol version");
  return common.sort((left, right) => Number(right) - Number(left))[0];
}

function assertCanonicalValue(value, path = "$ROOT") {
  if (value === null || typeof value === "string" || typeof value === "boolean") return;
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) {
      throw new TypeError(`${path}: only JavaScript safe integer numbers are allowed`);
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertCanonicalValue(item, `${path}[${index}]`));
    return;
  }
  if (typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype) {
    for (const [key, item] of Object.entries(value)) {
      if (!/^[\x00-\x7F]*$/.test(key)) throw new TypeError(`${path}: object keys must be ASCII`);
      assertCanonicalValue(item, `${path}.${key}`);
    }
    return;
  }
  throw new TypeError(`${path}: unsupported canonical JSON value`);
}

function sortCanonical(value) {
  if (Array.isArray(value)) return value.map(sortCanonical);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, sortCanonical(value[key])]),
    );
  }
  return value;
}

export function canonicalJsonString(value) {
  assertCanonicalValue(value);
  return JSON.stringify(sortCanonical(value));
}

export function canonicalJsonBytes(value) {
  return new TextEncoder().encode(canonicalJsonString(value));
}

export function sha256Hex(bytes) {
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}

export function contentHash(value) {
  return sha256Hex(canonicalJsonBytes(value));
}

function payloadWithoutNullFields(payload) {
  return Object.fromEntries(Object.entries(payload).filter(([, value]) => value !== null));
}

function digestBytes(value, prefix = "sha256:") {
  const raw = value.startsWith(prefix) ? value.slice(prefix.length) : value.replace(/^0x/, "");
  if (!/^[0-9a-fA-F]{64}$/.test(raw)) throw new TypeError("expected a 32-byte hash");
  return `0x${raw.toLowerCase()}`;
}

function parseTimestamp(value, field) {
  if (typeof value !== "string" || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.(\d+))?Z$/.test(value)) {
    throw new ProtocolValidationError("INVALID_TIMESTAMP", `${field} must be UTC RFC3339 with millisecond precision`);
  }
  const fractionalDigits = value.match(/\.(\d+)Z$/)?.[1] ?? "";
  if (fractionalDigits.length > 3 && /[1-9]/.test(fractionalDigits.slice(3))) {
    throw new ProtocolValidationError("INVALID_TIMESTAMP", `${field} exceeds millisecond precision`);
  }
  const normalized = value.replace(/(\.\d{3})\d+Z$/, "$1Z");
  const parsed = Date.parse(normalized);
  if (!Number.isFinite(parsed)) throw new ProtocolValidationError("INVALID_TIMESTAMP", `${field} is invalid`);
  return parsed;
}

export function typedData(envelope, chainId = 31337) {
  const payload = envelope.payload;
  const createdAtMs = parseTimestamp(envelope.createdAt, "createdAt");
  const expiresAtMs = parseTimestamp(envelope.expiresAt, "expiresAt");
  const messageId = `0x${createHash("sha256").update(envelope.id, "utf8").digest("hex")}`;
  return {
    domain: { ...EIP712_DOMAIN, chainId },
    types: MESSAGE_TYPES,
    value: {
      protocolVersion: Number(envelope.protocolVersion),
      messageId,
      createdAtMs,
      expiresAtMs,
      principalId: envelope.principalId,
      signer: envelope.signer,
      keyEpoch: envelope.keyEpoch,
      messageType: payload.kind,
      payloadHash: digestBytes(envelope.payloadHash),
      nonce: digestBytes(envelope.nonce, "0x"),
    },
  };
}

export function typedMessageHash(envelope, chainId = 31337) {
  const data = typedData(envelope, chainId);
  return TypedDataEncoder.hash(data.domain, data.types, data.value);
}

export function verifyEnvelope(envelope, chainId = 31337) {
  try {
    if (envelope.protocolVersion !== PROTOCOL_VERSION) return false;
    if (!/^0x[0-9a-fA-F]{40}$/.test(envelope.signer)) return false;
    if (!/^0x[0-9a-fA-F]{64}$/.test(envelope.nonce)) return false;
    if (!/^0x[0-9a-fA-F]{130}$/.test(envelope.signature)) return false;
    if (!Number.isSafeInteger(envelope.keyEpoch) || envelope.keyEpoch < 0) return false;
    if (contentHash(payloadWithoutNullFields(envelope.payload)) !== envelope.payloadHash) return false;
    const data = typedData(envelope, chainId);
    const recovered = verifyTypedData(data.domain, data.types, data.value, envelope.signature);
    return recovered.toLowerCase() === envelope.signer.toLowerCase();
  } catch {
    return false;
  }
}

export async function validateEnvelope(envelope, options = {}) {
  const {
    chainId = 31337,
    now = Date.now(),
    maxLifetimeMs = 7 * 24 * 60 * 60 * 1000,
    maxClockSkewMs = 2 * 60 * 1000,
    identityResolver,
    replayStore,
  } = options;
  if (envelope.protocolVersion !== PROTOCOL_VERSION) {
    throw new ProtocolValidationError("UNSUPPORTED_VERSION", "unsupported protocol version");
  }
  if (!verifyEnvelope(envelope, chainId)) {
    throw new ProtocolValidationError("INVALID_SIGNATURE", "message signature or payload hash is invalid");
  }
  const createdAt = parseTimestamp(envelope.createdAt, "createdAt");
  const expiresAt = parseTimestamp(envelope.expiresAt, "expiresAt");
  if (expiresAt <= createdAt || expiresAt - createdAt > maxLifetimeMs) {
    throw new ProtocolValidationError("INVALID_EXPIRY", "message expiry is invalid or exceeds maximum lifetime");
  }
  if (createdAt > now + maxClockSkewMs) {
    throw new ProtocolValidationError("FUTURE_MESSAGE", "message creation time is in the future");
  }
  if (expiresAt <= now) throw new ProtocolValidationError("EXPIRED", "message has expired");

  if (identityResolver) {
    const identity = await identityResolver(envelope.principalId);
    if (!identity || identity.status !== "ACTIVE") {
      throw new ProtocolValidationError("REVOKED_SIGNER", "signing identity is unknown or revoked");
    }
    if (identity.signer?.toLowerCase() !== envelope.signer.toLowerCase()
      || identity.keyEpoch !== envelope.keyEpoch) {
      throw new ProtocolValidationError("IDENTITY_MISMATCH", "signer does not match the active identity key binding");
    }
  }
  if (replayStore && !(await replayStore.consume(envelope, chainId, now))) {
    throw new ProtocolValidationError("REPLAY", "message identifier or signer nonce has already been accepted");
  }
  return true;
}

export class MemoryReplayStore {
  #seen = new Set();

  async consume(envelope, chainId) {
    const signer = envelope.signer.toLowerCase();
    const keys = [
      `${chainId}:nonce:${signer}:${envelope.nonce.toLowerCase()}`,
      `${chainId}:id:${envelope.id}`,
    ];
    if (keys.some((key) => this.#seen.has(key))) return false;
    keys.forEach((key) => this.#seen.add(key));
    return true;
  }
}
