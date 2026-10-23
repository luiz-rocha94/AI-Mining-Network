export type JsonValue = null | boolean | string | number | JsonValue[] | { [key: string]: JsonValue };
export type IdentityBinding = { status: "ACTIVE" | "REVOKED"; signer: string; keyEpoch: number };
export type SignedEnvelope = {
  protocolVersion: "2";
  id: string;
  createdAt: string;
  expiresAt: string;
  principalId: string;
  keyEpoch: number;
  signer: string;
  nonce: string;
  payload: Record<string, JsonValue> & { kind: string };
  payloadHash: `sha256:${string}`;
  signature: `0x${string}`;
};
export class ProtocolValidationError extends Error { code: string; }
export const PROTOCOL_VERSION: "2";
export const SUPPORTED_PROTOCOL_VERSIONS: Set<string>;
export const EIP712_DOMAIN: { name: string; version: string };
export const MESSAGE_TYPES: Record<string, Array<{ name: string; type: string }>>;
export function canonicalJsonString(value: JsonValue): string;
export function canonicalJsonBytes(value: JsonValue): Uint8Array;
export function sha256Hex(bytes: Uint8Array): `sha256:${string}`;
export function contentHash(value: JsonValue): `sha256:${string}`;
export function negotiateProtocolVersion(localVersions: Iterable<string>, remoteVersions: Iterable<string>): string;
export function typedData(envelope: SignedEnvelope, chainId?: number): { domain: Record<string, string | number>; types: typeof MESSAGE_TYPES; value: Record<string, unknown> };
export function typedMessageHash(envelope: SignedEnvelope, chainId?: number): string;
export function verifyEnvelope(envelope: SignedEnvelope, chainId?: number): boolean;
export function validateEnvelope(envelope: SignedEnvelope, options?: {
  chainId?: number;
  now?: number;
  maxLifetimeMs?: number;
  maxClockSkewMs?: number;
  identityResolver?: (principalId: string) => IdentityBinding | Promise<IdentityBinding | undefined> | undefined;
  replayStore?: { consume(envelope: SignedEnvelope, chainId: number, now: number): boolean | Promise<boolean> };
}): Promise<true>;
export class MemoryReplayStore {
  consume(envelope: SignedEnvelope, chainId: number, now?: number): Promise<boolean>;
}
