import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { readFileSync } from "node:fs";
import { StandardMerkleTree } from "@openzeppelin/merkle-tree";
import { Wallet } from "ethers";
import {
  MemoryReplayStore,
  PROTOCOL_VERSION,
  ProtocolValidationError,
  canonicalJsonBytes,
  contentHash,
  negotiateProtocolVersion,
  typedData,
  typedMessageHash,
  validateEnvelope,
  verifyEnvelope,
} from "../v2/index.mjs";
import { FileReplayStore } from "../v2/node-replay-store.mjs";

const vectors = JSON.parse(readFileSync(new URL("../v2/golden-vectors.json", import.meta.url), "utf8"));
const fixture = vectors.signedEnvelope;
const envelope = fixture.message;
const createdAt = Date.parse(envelope.createdAt);
assert.equal(vectors.protocolVersion, PROTOCOL_VERSION);

test("Python and TypeScript canonical JSON and payload hash match golden vectors", () => {
  assert.equal(new TextDecoder().decode(canonicalJsonBytes(vectors.canonical.value)), vectors.canonical.utf8);
  assert.equal(contentHash(vectors.canonical.value), vectors.canonical.sha256);
  assert.throws(() => canonicalJsonBytes({ amount: 0.1 }), /safe integer/);
  assert.throws(() => canonicalJsonBytes({ "β": 1 }), /ASCII/);
});

test("Python and TypeScript produce byte-identical EIP-712 digest and signature", async () => {
  const data = typedData(envelope, fixture.chainId);
  assert.equal(typedMessageHash(envelope, fixture.chainId), fixture.typedDataHash);
  const wallet = new Wallet(fixture.testOnlyPrivateKey);
  const signature = await wallet.signTypedData(data.domain, data.types, data.value);
  assert.equal(signature, envelope.signature);
  assert.equal(verifyEnvelope(envelope, fixture.chainId), true);
  assert.equal(verifyEnvelope(envelope, 1), false);

  const altered = structuredClone(envelope);
  altered.payload.stars = 1;
  assert.equal(verifyEnvelope(altered, fixture.chainId), false);
  const wrongIdentity = structuredClone(envelope);
  wrongIdentity.principalId = "did:amn:attacker";
  assert.equal(verifyEnvelope(wrongIdentity, fixture.chainId), false);
});

test("protocol version negotiation selects a common supported major or rejects", () => {
  assert.equal(negotiateProtocolVersion(["2"], ["3", "2"]), "2");
  assert.throws(() => negotiateProtocolVersion(["2"], ["3"]), /no mutually supported/);
  assert.throws(() => negotiateProtocolVersion(["1.0"], ["1.0"]), /decimal major-version/);
});

test("OpenZeppelin Merkle leaves, root, and inclusion proof match Python vectors", () => {
  const tree = StandardMerkleTree.of(vectors.merkle.values, vectors.merkle.types);
  assert.equal(tree.root, vectors.merkle.root);
  assert.deepEqual(tree.getProof(1), vectors.merkle.proofForIndex1);
  assert.equal(tree.verify(1, vectors.merkle.proofForIndex1), true);
});

test("live validation rejects expired messages, revoked signers, and unknown versions", async () => {
  await assert.rejects(
    validateEnvelope(envelope, { chainId: fixture.chainId, now: Date.parse(envelope.expiresAt) + 1 }),
    (error) => error instanceof ProtocolValidationError && error.code === "EXPIRED",
  );
  await assert.rejects(
    validateEnvelope(envelope, {
      chainId: fixture.chainId,
      now: createdAt + 1000,
      identityResolver: () => ({ status: "REVOKED", signer: envelope.signer, keyEpoch: envelope.keyEpoch }),
    }),
    (error) => error instanceof ProtocolValidationError && error.code === "REVOKED_SIGNER",
  );
  const unknownVersion = { ...envelope, protocolVersion: "3" };
  const legacyVersion = { ...envelope, protocolVersion: "1" };
  for (const unsupported of [unknownVersion, legacyVersion]) {
    assert.equal(verifyEnvelope(unsupported, fixture.chainId), false);
    await assert.rejects(
      validateEnvelope(unsupported, { chainId: fixture.chainId, now: createdAt + 1000 }),
      (error) => error instanceof ProtocolValidationError && error.code === "UNSUPPORTED_VERSION",
    );
  }
});

test("replay checks reject duplicate message IDs and nonces across guard instances", async () => {
  const memory = new MemoryReplayStore();
  await validateEnvelope(envelope, { chainId: fixture.chainId, now: createdAt + 1000, replayStore: memory });
  await assert.rejects(
    validateEnvelope(envelope, { chainId: fixture.chainId, now: createdAt + 1000, replayStore: memory }),
    (error) => error instanceof ProtocolValidationError && error.code === "REPLAY",
  );

  const wallet = new Wallet(fixture.testOnlyPrivateKey);
  const freshIdSameNonce = { ...envelope, id: "00000000-0000-4000-8000-000000000002" };
  const secondData = typedData(freshIdSameNonce, fixture.chainId);
  freshIdSameNonce.signature = await wallet.signTypedData(secondData.domain, secondData.types, secondData.value);
  await assert.rejects(
    validateEnvelope(freshIdSameNonce, { chainId: fixture.chainId, now: createdAt + 1000, replayStore: memory }),
    (error) => error instanceof ProtocolValidationError && error.code === "REPLAY",
  );

  const temp = await mkdtemp(path.join(tmpdir(), "amn-protocol-replay-"));
  try {
    const firstProcess = new FileReplayStore(temp);
    await validateEnvelope(envelope, { chainId: fixture.chainId, now: createdAt + 1000, replayStore: firstProcess });
    const restartedProcess = new FileReplayStore(temp);
    await assert.rejects(
      validateEnvelope(envelope, { chainId: fixture.chainId, now: createdAt + 1000, replayStore: restartedProcess }),
      (error) => error instanceof ProtocolValidationError && error.code === "REPLAY",
    );
  } finally {
    await rm(temp, { recursive: true, force: true });
  }

  const raceDirectory = await mkdtemp(path.join(tmpdir(), "amn-protocol-replay-race-"));
  try {
    const outcomes = await Promise.allSettled([
      validateEnvelope(envelope, {
        chainId: fixture.chainId,
        now: createdAt + 1000,
        replayStore: new FileReplayStore(raceDirectory),
      }),
      validateEnvelope(envelope, {
        chainId: fixture.chainId,
        now: createdAt + 1000,
        replayStore: new FileReplayStore(raceDirectory),
      }),
    ]);
    assert.equal(outcomes.filter((result) => result.status === "fulfilled").length, 1);
    assert.equal(outcomes.filter((result) => result.status === "rejected").length, 1);
  } finally {
    await rm(raceDirectory, { recursive: true, force: true });
  }
});
