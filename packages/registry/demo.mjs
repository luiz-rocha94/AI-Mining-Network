import { randomBytes, randomUUID } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { Wallet } from "ethers";
import { createPeerTransport } from "@ai-mining-network/networking";
import { HeliaArtifactStore } from "@ai-mining-network/storage";
import { contentHash, typedData } from "@ai-mining-network/protocol";
import { OrbitDbPublicMetadataStore } from "./src/index.mjs";

const demoRoot = await mkdtemp(path.join(tmpdir(), "amn-orbitdb-demo-"));
const wallet = Wallet.createRandom();
const principalId = "did:amn:orbitdb-demo-author";
const resolver = async (principal, envelope) => principal === principalId && envelope.signer.toLowerCase() === wallet.address.toLowerCase()
  ? { status: "ACTIVE", signer: wallet.address, keyEpoch: 0 }
  : { status: "REVOKED", signer: wallet.address, keyEpoch: 0 };
let publisherStore;
let readerStore;
let publisherNetwork;
let readerNetwork;
let publisher;
let reader;

try {
  ({ store: publisherStore, network: publisherNetwork } = await openPeer("publisher"));
  publisher = await OrbitDbPublicMetadataStore.create({
    artifactStore: publisherStore,
    directory: path.join(demoRoot, "publisher", "orbitdb"),
    identityId: "amn-orbitdb-demo-publisher",
    identityResolver: resolver,
  });

  const createdAt = new Date(Math.floor(Date.now() / 1000) * 1000).toISOString();
  const envelope = {
    protocolVersion: "2",
    id: randomUUID(),
    createdAt,
    expiresAt: new Date(Date.parse(createdAt) + 60 * 60 * 1000).toISOString(),
    principalId,
    signer: wallet.address,
    keyEpoch: 0,
    payload: {
      kind: "SkillManifest",
      name: "demo-text-summarizer",
      version: "1.0.0",
      contentHash: `sha256:${"a".repeat(64)}`,
      inputSchema: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
      outputSchema: { type: "object", properties: { summary: { type: "string" } }, required: ["summary"] },
      verificationStrategy: "per-document",
    },
    payloadHash: "",
    nonce: `0x${randomBytes(32).toString("hex")}`,
    signature: "0x",
  };
  envelope.payloadHash = contentHash(envelope.payload);
  const data = typedData(envelope, 31337);
  envelope.signature = await wallet.signTypedData(data.domain, data.types, data.value);
  const published = await publisher.append(envelope);

  ({ store: readerStore, network: readerNetwork } = await openPeer("reader", publisherStore.multiaddrs[0]));
  reader = await OrbitDbPublicMetadataStore.create({
    artifactStore: readerStore,
    directory: path.join(demoRoot, "reader", "orbitdb"),
    identityId: "amn-orbitdb-demo-reader",
    databaseAddress: publisher.address,
    identityResolver: resolver,
  });
  const deadline = Date.now() + 15_000;
  let discovered = [];
  while (Date.now() < deadline) {
    discovered = await reader.discoverSkills();
    if (discovered.some((record) => record.id === envelope.id)) break;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  if (!discovered.some((record) => record.id === envelope.id)) throw new Error("reader peer did not replicate the public skill record");
  console.log(JSON.stringify({
    databaseAddress: publisher.address,
    publishedStatus: published.status,
    publisherPeerId: publisher.peerId,
    readerPeerId: reader.peerId,
    discoveredSkills: discovered.map(({ payload, principalId: author }) => ({ name: payload.name, version: payload.version, contentHash: payload.contentHash, author })),
    privacy: "only the public skill manifest was published; task inputs and outputs use separate private storage",
  }, null, 2));
} finally {
  await reader?.close().catch(() => {});
  await publisher?.close().catch(() => {});
  await readerStore?.close().catch(() => {});
  await publisherStore?.close().catch(() => {});
  await readerNetwork?.close().catch(() => {});
  await publisherNetwork?.close().catch(() => {});
  await rm(demoRoot, { recursive: true, force: true });
}

async function openPeer(name, connectAddress) {
  const network = await createPeerTransport({
    storageDirectory: path.join(demoRoot, name, "networking"),
    identityResolver: resolver,
    peerPrincipalResolver: (peerId) => `peer:${peerId}`,
    authorizePrivateSender: async () => true,
    authorizePrivateRecipient: async () => true,
    allowOrbitDbTopics: true,
    discovery: false,
    listenAddresses: ["/ip4/127.0.0.1/tcp/0"],
  });
  try {
    const store = await new HeliaArtifactStore({
      storageDir: path.join(demoRoot, name, "helia"),
      libp2pNode: network.node,
    }).open();
    if (connectAddress) await store.connect(connectAddress);
    return { store, network };
  } catch (error) {
    await network.close().catch(() => {});
    throw error;
  }
}
