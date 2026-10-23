import { randomBytes, randomUUID } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { memory } from "@libp2p/memory";
import { Wallet } from "ethers";
import { contentHash, typedData } from "@ai-mining-network/protocol";
import { createLibp2pPeerTransport } from "./src/libp2p-transport.mjs";

const directory = await mkdtemp(path.join(tmpdir(), "amn-network-demo-"));
const peers = [];
const wallets = new Map([
  ["did:amn:demo-alice", Wallet.createRandom()],
  ["did:amn:demo-bob", Wallet.createRandom()],
  ["did:amn:demo-carol", Wallet.createRandom()],
]);
const peerPrincipals = new Map();
const identityResolver = async (principalId) => {
  const wallet = wallets.get(principalId);
  return wallet == null ? undefined : { status: "ACTIVE", signer: wallet.address, keyEpoch: 0 };
};
const sharedOptions = {
  identityResolver,
  peerPrincipalResolver: async (peerId) => peerPrincipals.get(peerId),
  authorizePrivateSender: async ({ principalId }) => wallets.has(principalId),
  authorizePrivateRecipient: async ({ peerId }) => peerPrincipals.has(peerId),
};

async function waitUntil(predicate, timeoutMs = 8_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error("demo peers did not converge before timeout");
}

async function createPeer(index, principalId, address, bootstrapPeers = []) {
  const peer = await createLibp2pPeerTransport({
    ...sharedOptions,
    storageDirectory: path.join(directory, `peer-${index}`),
    listenAddresses: [`/memory/${address}`],
    bootstrapPeers,
    testOnly: true,
    testOnlyModules: { transports: [memory()], peerDiscovery: [] },
  });
  peers.push(peer);
  peerPrincipals.set(peer.localPeerId, principalId);
  return peer;
}

try {
  const alice = await createPeer(0, "did:amn:demo-alice", `amn-demo-${randomUUID()}`);
  const bob = await createPeer(1, "did:amn:demo-bob", `amn-demo-${randomUUID()}`);
  const carol = await createPeer(2, "did:amn:demo-carol", `amn-demo-${randomUUID()}`, alice.identity.multiaddrs);
  await alice.connect(bob.identity.multiaddrs[0]);
  await waitUntil(async () => (await alice.getPeers()).length >= 1 && (await carol.getPeers()).length >= 1);

  const payload = {
    kind: "MinerCapabilities",
    peerId: alice.localPeerId,
    paymentAddress: `0x${"12".repeat(20)}`,
    cpuCores: 8,
    ramBytes: 16_000_000_000,
    gpuModels: ["demo-device"],
    vramBytes: 0,
    modelDigests: ["demo-model"],
    skillHashes: [`sha256:${"ab".repeat(32)}`],
    supportedRuntimes: ["demo-runtime"],
    available: true,
  };
  const message = {
    protocolVersion: "2",
    id: randomUUID(),
    createdAt: new Date().toISOString(),
    expiresAt: new Date(Date.now() + 60 * 60_000).toISOString(),
    principalId: "did:amn:demo-alice",
    keyEpoch: 0,
    signer: wallets.get("did:amn:demo-alice").address,
    nonce: `0x${randomBytes(32).toString("hex")}`,
    payload,
    payloadHash: contentHash(payload),
  };
  const signature = typedData(message, 31337);
  message.signature = await wallets.get("did:amn:demo-alice")
    .signTypedData(signature.domain, signature.types, signature.value);
  const delivery = await alice.publish(message);
  await waitUntil(async () => Promise.all([bob, carol].map(async (peer) =>
    (await peer.getPublicEvents()).some((event) => event.id === message.id))).then((results) => results.every(Boolean)));

  console.log(JSON.stringify({
    mode: "local test only — in-process libp2p memory transport",
    encryptedTransport: "Noise",
    publicTopic: "amn/protocol/v2/public",
    signedMessageId: message.id,
    delivery: delivery.delivery,
    peers: peers.map((peer) => peer.localPeerId),
    converged: await Promise.all(peers.map(async (peer) => ({
      peerId: peer.localPeerId,
      publicEventIds: (await peer.getPublicEvents()).map((event) => event.id),
    }))),
    note: "This demo exercises the libp2p implementation locally; memory transport is not a production network backend.",
  }, null, 2));
} finally {
  await Promise.all(peers.map((peer) => peer.close().catch(() => {})));
  await rm(directory, { recursive: true, force: true });
}
