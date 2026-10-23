import { randomBytes, randomUUID } from "node:crypto";
import { Wallet } from "ethers";
import { contentHash, typedData } from "@ai-mining-network/protocol";

export async function signedMessage(wallet, principalId, payload, { chainId = 31337, lifetimeMs = 60 * 60_000 } = {}) {
  const createdAt = new Date().toISOString();
  const envelope = {
    protocolVersion: "2",
    id: randomUUID(),
    createdAt,
    expiresAt: new Date(Date.now() + lifetimeMs).toISOString(),
    principalId,
    keyEpoch: 0,
    signer: wallet.address,
    nonce: `0x${randomBytes(32).toString("hex")}`,
    payload,
    payloadHash: contentHash(Object.fromEntries(Object.entries(payload).filter(([, value]) => value !== null))),
  };
  const data = typedData(envelope, chainId);
  envelope.signature = await wallet.signTypedData(data.domain, data.types, data.value);
  return envelope;
}

export function createIdentitySet(principals = ["did:amn:alice", "did:amn:bob", "did:amn:carol"]) {
  const wallets = new Map(principals.map((principal) => [principal, Wallet.createRandom()]));
  const peerPrincipals = new Map();
  return {
    wallets,
    peerPrincipals,
    identityResolver: async (principalId) => {
      const wallet = wallets.get(principalId);
      return wallet == null ? undefined : { status: "ACTIVE", signer: wallet.address, keyEpoch: 0 };
    },
    peerPrincipalResolver: async (peerId) => peerPrincipals.get(peerId),
    authorizePrivateSender: async ({ principalId }) => wallets.has(principalId),
    authorizePrivateRecipient: async ({ peerId }) => peerPrincipals.has(peerId),
  };
}

export function minerCapabilities(peerId, tag = "test") {
  return {
    kind: "MinerCapabilities",
    peerId,
    paymentAddress: `0x${"11".repeat(20)}`,
    cpuCores: 4,
    ramBytes: 8_000_000_000,
    gpuModels: [],
    vramBytes: 0,
    modelDigests: [],
    skillHashes: [`sha256:${"22".repeat(32)}`],
    supportedRuntimes: [tag],
    available: true,
  };
}

export function customerTask(taskId = `task-${randomUUID()}`) {
  return {
    kind: "CustomerTask",
    taskId,
    skillHash: `sha256:${"22".repeat(32)}`,
    inputSetHash: `sha256:${"33".repeat(32)}`,
    inputCount: 1,
    partitioning: "per-document",
    requestedOutput: "extract test data",
    maxPriceAtomic: 1000,
    currency: { chainId: 31337, tokenAddress: `0x${"00".repeat(20)}`, decimals: 18, symbol: "TEST" },
    deadline: new Date(Date.now() + 30 * 60_000).toISOString(),
    verificationSampleCount: 1,
    validatorCount: 1,
  };
}

export function verificationReceipt({ peerId, taskId }) {
  return {
    kind: "VerificationReceipt",
    receiptId: randomUUID(),
    taskId,
    commitmentRoot: `0x${"44".repeat(32)}`,
    validatorPeerId: peerId,
    unitId: "unit-1",
    result: "PASS",
    observedOutputHash: null,
    evidenceHash: `sha256:${"55".repeat(32)}`,
    merkleProof: [],
    reasonCode: "TEST_ACK",
    modelDigest: "fixture-model",
    durationMs: 1,
  };
}
