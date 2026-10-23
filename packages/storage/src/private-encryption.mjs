import {
  createCipheriv,
  createDecipheriv,
  createPrivateKey,
  createPublicKey,
  diffieHellman,
  generateKeyPairSync,
  hkdfSync,
  randomBytes,
  randomUUID,
} from "node:crypto";

const ARTIFACT_MAGIC = Buffer.from("AMNPRIV1", "ascii");
const ARTIFACT_AAD_DOMAIN = "ai-mining-network/private-artifact/v1";
const ENVELOPE_AAD_DOMAIN = "ai-mining-network/private-grant/v1";

export function createRecipientKeyPair() {
  const pair = generateKeyPairSync("x25519");
  return {
    publicKey: pair.publicKey.export({ format: "der", type: "spki" }).toString("base64"),
    privateKey: pair.privateKey.export({ format: "der", type: "pkcs8" }).toString("base64"),
  };
}

export function encryptPrivateBytes(plaintext, taskId) {
  const dek = randomBytes(32);
  const nonce = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", dek, nonce);
  cipher.setAAD(artifactAad(taskId));
  const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
  const tag = cipher.getAuthTag();
  const storedBytes = Buffer.concat([ARTIFACT_MAGIC, nonce, tag, ciphertext]);
  return { dek, storedBytes };
}

export function createGrantEnvelope({
  dek,
  cid,
  taskId,
  recipientPrincipalId,
  recipientPublicKey,
  encryptedSha256,
  expiresAt,
}) {
  const grantId = randomUUID();
  const ephemeral = generateKeyPairSync("x25519");
  const recipientKey = createPublicKey({
    key: Buffer.from(recipientPublicKey, "base64"),
    format: "der",
    type: "spki",
  });
  const sharedSecret = diffieHellman({ privateKey: ephemeral.privateKey, publicKey: recipientKey });
  const context = envelopeContext({ grantId, cid, taskId, recipientPrincipalId, expiresAt });
  const salt = randomBytes(32);
  const wrappingKey = Buffer.from(hkdfSync("sha256", sharedSecret, salt, Buffer.from(context), 32));
  const nonce = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", wrappingKey, nonce);
  cipher.setAAD(Buffer.from(context));
  const wrappedDek = Buffer.concat([cipher.update(dek), cipher.final()]);
  const tag = cipher.getAuthTag();
  return {
    protocolVersion: "amn.private-grant.v1",
    grantId,
    cid,
    taskId,
    recipientPrincipalId,
    expiresAt,
    encryptedSha256,
    ephemeralPublicKey: ephemeral.publicKey.export({ format: "der", type: "spki" }).toString("base64"),
    salt: salt.toString("base64"),
    nonce: nonce.toString("base64"),
    tag: tag.toString("base64"),
    wrappedDek: wrappedDek.toString("base64"),
  };
}

export function decryptPrivateBytes(storedBytes, { envelope, recipientPrincipalId, recipientPrivateKey }) {
  if (envelope.protocolVersion !== "amn.private-grant.v1") throw new Error("Unsupported private grant version");
  if (envelope.recipientPrincipalId !== recipientPrincipalId) throw new Error("Private grant is assigned to another recipient");
  const minSize = ARTIFACT_MAGIC.length + 12 + 16;
  if (storedBytes.length < minSize || !storedBytes.subarray(0, ARTIFACT_MAGIC.length).equals(ARTIFACT_MAGIC)) {
    throw new Error("Private artifact envelope is malformed");
  }

  const ephemeralPublicKey = createPublicKey({
    key: Buffer.from(envelope.ephemeralPublicKey, "base64"),
    format: "der",
    type: "spki",
  });
  const privateKey = createPrivateKey({
    key: Buffer.from(recipientPrivateKey, "base64"),
    format: "der",
    type: "pkcs8",
  });
  const sharedSecret = diffieHellman({ privateKey, publicKey: ephemeralPublicKey });
  const context = envelopeContext(envelope);
  const wrappingKey = Buffer.from(
    hkdfSync("sha256", sharedSecret, Buffer.from(envelope.salt, "base64"), Buffer.from(context), 32),
  );
  const unwrap = createDecipheriv("aes-256-gcm", wrappingKey, Buffer.from(envelope.nonce, "base64"));
  unwrap.setAAD(Buffer.from(context));
  unwrap.setAuthTag(Buffer.from(envelope.tag, "base64"));
  const dek = Buffer.concat([
    unwrap.update(Buffer.from(envelope.wrappedDek, "base64")),
    unwrap.final(),
  ]);

  const nonceOffset = ARTIFACT_MAGIC.length;
  const nonce = storedBytes.subarray(nonceOffset, nonceOffset + 12);
  const tag = storedBytes.subarray(nonceOffset + 12, nonceOffset + 28);
  const ciphertext = storedBytes.subarray(nonceOffset + 28);
  const decrypt = createDecipheriv("aes-256-gcm", dek, nonce);
  decrypt.setAAD(artifactAad(envelope.taskId));
  decrypt.setAuthTag(tag);
  return Buffer.concat([decrypt.update(ciphertext), decrypt.final()]);
}

function artifactAad(taskId) {
  return Buffer.from(`${ARTIFACT_AAD_DOMAIN}:${taskId}`);
}

function envelopeContext({ grantId, cid, taskId, recipientPrincipalId, expiresAt }) {
  return `${ENVELOPE_AAD_DOMAIN}:${grantId}:${cid}:${taskId}:${recipientPrincipalId}:${expiresAt}`;
}
