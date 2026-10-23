import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { randomBytes, randomUUID } from "node:crypto";
import { Wallet } from "ethers";
import { createPeerTransport } from "@ai-mining-network/networking";
import { HeliaArtifactStore } from "@ai-mining-network/storage";
import { canonicalJsonString, contentHash, typedData } from "@ai-mining-network/protocol";
import { OrbitDbPublicMetadataStore, RegistryError } from "../src/index.mjs";

const CHAIN_ID = 31337;
const TIMEOUT_MS = 25_000;

function identityResolver(wallets) {
  return async (principalId, envelope) => {
    const wallet = wallets.get(principalId);
    if (!wallet || wallet.address.toLowerCase() !== envelope.signer.toLowerCase()) {
      return { status: "REVOKED", signer: wallet?.address ?? "0x0000000000000000000000000000000000000000", keyEpoch: 0 };
    }
    return { status: "ACTIVE", signer: wallet.address, keyEpoch: 0 };
  };
}

async function signEnvelope(wallet, principalId, payload, id = randomUUID()) {
  const createdAt = new Date(Math.floor(Date.now() / 1000) * 1000).toISOString();
  const envelope = {
    protocolVersion: "2",
    id,
    createdAt,
    expiresAt: new Date(Date.parse(createdAt) + 60 * 60 * 1000).toISOString(),
    principalId,
    signer: wallet.address,
    keyEpoch: 0,
    payload,
    payloadHash: contentHash(payload),
    nonce: `0x${randomBytes(32).toString("hex")}`,
    signature: "0x",
  };
  const data = typedData(envelope, CHAIN_ID);
  envelope.signature = await wallet.signTypedData(data.domain, data.types, data.value);
  return envelope;
}

function manifest({ name = "invoice-extractor", version = "1.0.0", hash = "a".repeat(64) } = {}) {
  return {
    kind: "SkillManifest",
    name,
    version,
    contentHash: `sha256:${hash}`,
    inputSchema: { type: "object", properties: {} },
    outputSchema: { type: "object", properties: {} },
    verificationStrategy: "per-document",
  };
}

async function createPeer(root, name, wallets, databaseAddress, connectAddress) {
  const peerTransport = await createPeerTransport({
    storageDirectory: path.join(root, name, "networking"),
    identityResolver: async (principalId) => {
      const wallet = wallets.get(principalId);
      return wallet ? { status: "ACTIVE", signer: wallet.address, keyEpoch: 0 } : null;
    },
    peerPrincipalResolver: (peerId) => `peer:${peerId}`,
    authorizePrivateSender: async () => true,
    authorizePrivateRecipient: async () => true,
    allowOrbitDbTopics: true,
    discovery: false,
    listenAddresses: ["/ip4/127.0.0.1/tcp/0"],
  });
  let artifactStore;
  try {
    artifactStore = await new HeliaArtifactStore({
      storageDir: path.join(root, name, "helia"),
      libp2pNode: peerTransport.node,
    }).open();
    if (connectAddress) await artifactStore.connect(connectAddress);
    const registry = await OrbitDbPublicMetadataStore.create({
      artifactStore,
      directory: path.join(root, name, "orbitdb"),
      identityId: `amn-test-${name}`,
      databaseAddress,
      chainId: CHAIN_ID,
      identityResolver: identityResolver(wallets),
      authorizeRecord: ({ kind, envelope }) => kind !== "PublicAuditCheckpoint"
        || envelope.principalId === "did:amn:auditor",
    });
    return { artifactStore, peerTransport, registry };
  } catch (error) {
    await artifactStore?.close().catch(() => {});
    await peerTransport.close().catch(() => {});
    throw error;
  }
}

async function closePeer(peer) {
  if (!peer) return;
  await peer.registry?.close().catch(() => {});
  await peer.artifactStore?.close().catch(() => {});
  await peer.peerTransport?.close().catch(() => {});
}

async function eventually(action, predicate, timeoutMs = TIMEOUT_MS) {
  const deadline = Date.now() + timeoutMs;
  let value;
  let lastError;
  while (Date.now() < deadline) {
    try {
      value = await action();
      if (predicate(value)) return value;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  if (lastError) throw lastError;
  throw new Error(`condition not reached in ${timeoutMs}ms; last value: ${JSON.stringify(value)}`);
}

test("OrbitDB public records replicate, resolve deterministically, and survive partition/restart/rejoin", { timeout: 120_000 }, async () => {
  const root = await mkdtemp(path.join(tmpdir(), "amn-p5-orbitdb-"));
  const authorA = new Wallet(`0x${"11".repeat(32)}`);
  const authorB = new Wallet(`0x${"22".repeat(32)}`);
  const auditor = new Wallet(`0x${"33".repeat(32)}`);
  const wallets = new Map([
    ["did:amn:author-a", authorA],
    ["did:amn:author-b", authorB],
    ["did:amn:auditor", auditor],
  ]);
  let peerA;
  let peerB;
  try {
    peerA = await createPeer(root, "peer-a", wallets);
    const firstManifest = await signEnvelope(authorA, "did:amn:author-a", manifest());
    const firstResult = await peerA.registry.append(firstManifest);
    assert.equal(firstResult.status, "accepted");
    assert.match(peerA.registry.address, /^\/orbitdb\//);

    peerB = await createPeer(root, "peer-b", wallets, peerA.registry.address, peerA.artifactStore.multiaddrs[0]);
    await eventually(() => peerB.registry.discoverSkills(), (records) => records.some((record) => record.id === firstManifest.id));

    const benchmark = await signEnvelope(authorA, "did:amn:author-a", {
      kind: "SkillBenchmark",
      benchmarkVersion: "invoice-synthetic-v2",
      skillHash: firstManifest.payload.contentHash,
      datasetHash: `sha256:${"b".repeat(64)}`,
      runtime: "amn-invoice-v1",
      runtimeDigest: `sha256:${"d".repeat(64)}`,
      modelDigest: "fixture-model-sha256",
      hardwareClass: "cpu-test",
      warmColdPolicy: "warm-after-single-warmup",
      sampleCount: 20,
      durationSamplesMs: Array.from({ length: 20 }, (_, index) => 300 + index * 10),
      durationVarianceMs2: 3325,
      outputValidationResults: Array.from({ length: 20 }, () => true),
      inputTokens: 1000,
      outputTokens: 500,
      p50DurationMs: 390,
      p90DurationMs: 470,
      successRateBps: 10000,
      outputValidationPasses: 20,
      measurementProvenance: {
        measurementMode: "self-reported",
        collectorVersion: "registry-fixture-v2",
        measurementId: "benchmark-fixture-run-1",
        environmentHash: `sha256:${"e".repeat(64)}`,
        limitations: ["test fixture", "not proof of inference"],
      },
      selfReportedMetrics: true,
      receiptId: "benchmark-fixture-1",
    });
    const concurrentManifest = await signEnvelope(authorB, "did:amn:author-b", manifest({ name: "text-summarizer", version: "2.0.0", hash: "c".repeat(64) }));
    await Promise.all([
      peerA.registry.append(benchmark),
      peerB.registry.append(concurrentManifest),
    ]);
    const expectedIds = [firstManifest.id, benchmark.id, concurrentManifest.id].sort();
    await eventually(async () => [
      (await peerA.registry.query()).map((record) => record.id).sort(),
      (await peerB.registry.query()).map((record) => record.id).sort(),
    ], ([left, right]) => canonicalJsonString(left) === canonicalJsonString(expectedIds)
      && canonicalJsonString(right) === canonicalJsonString(expectedIds));

    const versionConflictA = await signEnvelope(authorA, "did:amn:author-a", manifest({ name: "contested-skill", version: "1.0.0", hash: "7".repeat(64) }));
    const versionConflictB = await signEnvelope(authorA, "did:amn:author-a", manifest({ name: "contested-skill", version: "1.0.0", hash: "8".repeat(64) }));
    await Promise.all([
      peerA.registry.append(versionConflictA),
      peerB.registry.append(versionConflictB),
    ]);
    const expectedConflictIds = [versionConflictA.id, versionConflictB.id].sort((a, b) => {
      const records = [versionConflictA, versionConflictB];
      return records.find((record) => record.id === a).payloadHash.localeCompare(records.find((record) => record.id === b).payloadHash);
    });
    const conflictsByPeer = await eventually(async () => [
      await peerA.registry.conflicts(),
      await peerB.registry.conflicts(),
    ], ([left, right]) => canonicalJsonString(left) === canonicalJsonString(right)
      && left.some((conflict) => conflict.key === "manifest:did:amn:author-a:contested-skill:1.0.0"
        && canonicalJsonString(conflict.records) === canonicalJsonString(expectedConflictIds)));
    assert.equal(conflictsByPeer[0].some((conflict) => conflict.key === "manifest:did:amn:author-a:contested-skill:1.0.0"), true);

    const duplicate = await peerA.registry.append(firstManifest);
    assert.equal(duplicate.status, "duplicate");
    const corrupt = structuredClone(firstManifest);
    corrupt.payload.version = "tampered";
    await assert.rejects(peerA.registry.append(corrupt), (error) => error.code === "MESSAGE_ID_CONFLICT");
    corrupt.id = randomUUID();
    await assert.rejects(peerA.registry.append(corrupt), /signature|payload hash/i);
    const privateTask = await signEnvelope(authorA, "did:amn:author-a", {
      kind: "CustomerTask",
      taskId: "private-task-1",
      prompt: "PRIVATE_PROMPT_SENTINEL",
    });
    await assert.rejects(peerA.registry.append(privateTask), (error) => error.code === "PRIVATE_RECORD_FORBIDDEN");
    for (const [kind, field, value] of [
      ["CustomerTask", "sourceDocument", "PRIVATE_SOURCE_SENTINEL"],
      ["CustomerTask", "groundTruth", "PRIVATE_GROUND_TRUTH_SENTINEL"],
      ["ExecutionCommitment", "plaintextResult", "PRIVATE_RESULT_SENTINEL"],
    ]) {
      await assert.rejects(
        peerA.registry.append(await signEnvelope(authorA, "did:amn:author-a", { kind, [field]: value })),
        (error) => error.code === "PRIVATE_RECORD_FORBIDDEN",
      );
    }

    const unauthorizedRevocation = await signEnvelope(authorB, "did:amn:author-b", {
      kind: "SkillRevocation", revocationId: "wrong-author", contentHash: firstManifest.payload.contentHash,
    });
    await assert.rejects(peerA.registry.append(unauthorizedRevocation), (error) => error.code === "UNAUTHORIZED_REVOCATION");
    const revocation = await signEnvelope(authorA, "did:amn:author-a", {
      kind: "SkillRevocation", revocationId: "withdraw-invoice-skill", contentHash: firstManifest.payload.contentHash,
    });
    await peerA.registry.append(revocation);
    await eventually(() => peerB.registry.discoverSkills(), (records) => !records.some((record) => record.id === firstManifest.id));
    assert.equal((await peerB.registry.discoverSkills({ includeRevoked: true })).some((record) => record.id === firstManifest.id), true);

    const unauthorizedCheckpoint = await signEnvelope(authorA, "did:amn:author-a", {
      kind: "PublicAuditCheckpoint",
      checkpointId: "checkpoint-1",
      recordsRoot: `sha256:${"d".repeat(64)}`,
      recordCount: 1,
      coveredRecordIds: [firstManifest.id],
    });
    await assert.rejects(peerA.registry.append(unauthorizedCheckpoint), (error) => error.code === "UNAUTHORIZED_RECORD");
    const checkpoint = await signEnvelope(auditor, "did:amn:auditor", {
      kind: "PublicAuditCheckpoint",
      checkpointId: "checkpoint-1",
      recordsRoot: `sha256:${"d".repeat(64)}`,
      recordCount: 1,
      coveredRecordIds: [firstManifest.id],
    });
    await peerA.registry.append(checkpoint);
    await eventually(() => peerB.registry.get(checkpoint.id), (record) => record?.id === checkpoint.id);

    // Close both nodes to create a real network partition, then make independent
    // writes on each persisted database before reconnecting them.
    const address = peerA.registry.address;
    await closePeer(peerB);
    peerB = undefined;
    await closePeer(peerA);
    peerA = undefined;

    peerA = await createPeer(root, "peer-a", wallets, address);
    peerB = await createPeer(root, "peer-b", wallets, address, peerA.artifactStore.multiaddrs[0]);
    const offlineA = await signEnvelope(authorA, "did:amn:author-a", manifest({ name: "partition-a", hash: "e".repeat(64) }));
    const offlineB = await signEnvelope(authorB, "did:amn:author-b", manifest({ name: "partition-b", hash: "f".repeat(64) }));
    await Promise.all([peerA.registry.append(offlineA), peerB.registry.append(offlineB)]);
    await closePeer(peerA);
    peerA = undefined;
    await closePeer(peerB);
    peerB = undefined;

    peerA = await createPeer(root, "peer-a", wallets, address);
    peerB = await createPeer(root, "peer-b", wallets, address, peerA.artifactStore.multiaddrs[0]);
    const finalIds = [
      firstManifest.id,
      benchmark.id,
      concurrentManifest.id,
      versionConflictA.id,
      versionConflictB.id,
      revocation.id,
      checkpoint.id,
      offlineA.id,
      offlineB.id,
    ].sort();
    await eventually(async () => [
      (await peerA.registry.query()).map((record) => record.id).sort(),
      (await peerB.registry.query()).map((record) => record.id).sort(),
    ], ([left, right]) => canonicalJsonString(left) === canonicalJsonString(finalIds)
      && canonicalJsonString(right) === canonicalJsonString(finalIds));

    const status = await peerA.registry.status();
    assert.equal(status.validRecordCount, finalIds.length);
    assert.equal(status.consistency, "eventual; records are immutable signed claims, not truth");
    assert.equal(status.privateTaskData, "not accepted by this adapter");
    assert.equal((await peerB.registry.discoverSkills()).some((record) => record.id === firstManifest.id), false);
    assert.equal((await peerB.registry.discoverSkills()).some((record) => record.id === offlineB.id), true);
    const publicJson = canonicalJsonString(await peerA.registry.query());
    for (const sentinel of ["PRIVATE_PROMPT_SENTINEL", "PRIVATE_SOURCE_SENTINEL", "PRIVATE_GROUND_TRUTH_SENTINEL", "PRIVATE_RESULT_SENTINEL"]) {
      assert.equal(publicJson.includes(sentinel), false, `${sentinel} must not enter the public database`);
    }
  } finally {
    await closePeer(peerB);
    await closePeer(peerA);
    await rm(root, { recursive: true, force: true });
  }
});

test("public metadata schema, signer policy, deterministic conflict views, and privacy boundary", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "amn-p5-policy-"));
  const authorA = new Wallet(`0x${"44".repeat(32)}`);
  const authorB = new Wallet(`0x${"55".repeat(32)}`);
  const wallets = new Map([["did:amn:author-a", authorA], ["did:amn:author-b", authorB]]);
  let peer;
  try {
    peer = await createPeer(root, "policy-peer", wallets);
    const first = await signEnvelope(authorA, "did:amn:author-a", manifest({ name: "same-skill", version: "1.0.0", hash: "1".repeat(64) }));
    const second = await signEnvelope(authorA, "did:amn:author-a", manifest({ name: "same-skill", version: "1.0.0", hash: "2".repeat(64) }));
    await peer.registry.append(first);
    await peer.registry.append(second);
    const conflicts = await peer.registry.conflicts();
    assert.equal(conflicts.length, 1);
    const expectedConflictOrder = [first, second].sort((a, b) => a.payloadHash.localeCompare(b.payloadHash)).map((record) => record.id);
    assert.deepEqual(conflicts[0].records, expectedConflictOrder);
    assert.deepEqual((await peer.registry.discoverSkills({ name: "same-skill" })).map((record) => record.id), [first.id, second.id]);

    const wrongSigner = await signEnvelope(authorB, "did:amn:author-a", manifest({ name: "forged-author" }));
    await assert.rejects(peer.registry.append(wrongSigner), (error) => error.code === "REVOKED_SIGNER");
    const malformed = await signEnvelope(authorA, "did:amn:author-a", {
      kind: "SkillManifest", name: "bad name", version: "1", contentHash: `sha256:${"3".repeat(64)}`,
      inputSchema: { type: "object" }, outputSchema: { type: "object" }, verificationStrategy: "unknown",
    });
    await assert.rejects(peer.registry.append(malformed), (error) => error.code === "INVALID_PUBLIC_SCHEMA");

    const persisted = await peer.registry.query();
    const publicBytes = canonicalJsonString(persisted);
    assert.equal(publicBytes.includes("PRIVATE_PROMPT_SENTINEL"), false);
    assert.equal(publicBytes.includes("private-task-1"), false);
  } finally {
    await closePeer(peer);
    await rm(root, { recursive: true, force: true });
  }
});
