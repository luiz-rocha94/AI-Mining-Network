import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { memory } from "@libp2p/memory";
import { typedData } from "@ai-mining-network/protocol";
import { PeerRateLimiter, assertProductionPeerTransport, createPeerTransport } from "../src/index.mjs";
import { encodeFrame, readFrame } from "../src/framing.mjs";
import {
  createIdentitySet,
  customerTask,
  minerCapabilities,
  signedMessage,
  verificationReceipt,
} from "./helpers.mjs";

async function waitUntil(predicate, timeoutMs = 8_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  assert.fail(`condition did not become true within ${timeoutMs}ms`);
}

function waitForPeerSync(peers, timeoutMs = 8_000) {
  const peerIds = new Set(peers.map((peer) => peer.localPeerId));
  return new Promise((resolve, reject) => {
    const removers = [];
    const timer = setTimeout(() => {
      removers.forEach((remove) => remove());
      reject(new Error("peer sync timed out"));
    }, timeoutMs);
    for (const peer of peers) {
      const handler = (event) => {
        if (!peerIds.has(event.peerId)) return;
        clearTimeout(timer);
        removers.forEach((remove) => remove());
        resolve(event);
      };
      peer.on("peer-sync", handler);
      removers.push(() => peer.off("peer-sync", handler));
    }
  });
}

async function createPeers(t, count = 3, identitySet = createIdentitySet()) {
  const directory = await mkdtemp(path.join(tmpdir(), "amn-libp2p-"));
  const peers = [];
  t.after(async () => {
    await Promise.all(peers.map((peer) => peer.close().catch(() => {})));
    await rm(directory, { recursive: true, force: true });
  });
  for (let index = 0; index < count; index += 1) {
    const principalId = ["did:amn:alice", "did:amn:bob", "did:amn:carol"][index];
    const peer = await createPeer({
      identitySet,
      principalId,
      storageDirectory: path.join(directory, `peer-${index}`),
      memoryAddress: `amn-${randomUUID()}`,
    });
    identitySet.peerPrincipals.set(peer.localPeerId, principalId);
    peers.push(peer);
  }
  return { directory, peers, identitySet };
}

async function createPeer({ identitySet, principalId, storageDirectory, memoryAddress, onRequest, extra = {} }) {
  return createPeerTransport({
    storageDirectory,
    listenAddresses: [`/memory/${memoryAddress}`],
    testOnly: true,
    testOnlyModules: { transports: [memory()], peerDiscovery: [] },
    identityResolver: identitySet.identityResolver,
    peerPrincipalResolver: identitySet.peerPrincipalResolver,
    authorizePrivateSender: identitySet.authorizePrivateSender,
    authorizePrivateRecipient: identitySet.authorizePrivateRecipient,
    onRequest,
    ...extra,
  });
}

test("production policy rejects local and test-only transport substitutions", async () => {
  assert.throws(() => assertProductionPeerTransport({ backend: "local", deployment: "production" }),
    (error) => error.code === "PRODUCTION_TRANSPORT_REQUIRED");
  const identitySet = createIdentitySet();
  await assert.rejects(
    createPeerTransport({
      storageDirectory: path.join(tmpdir(), `amn-invalid-${randomUUID()}`),
      identityResolver: identitySet.identityResolver,
      peerPrincipalResolver: identitySet.peerPrincipalResolver,
      authorizePrivateSender: identitySet.authorizePrivateSender,
      authorizePrivateRecipient: identitySet.authorizePrivateRecipient,
      testOnlyModules: { transports: [memory()] },
      deployment: "production",
    }),
    (error) => error.code === "TEST_MODULES_FORBIDDEN",
  );
  await assert.rejects(
    createPeerTransport({ backend: "local", productionRequired: true }),
    (error) => error.code === "PRODUCTION_TRANSPORT_REQUIRED",
  );
});

test("three libp2p peers exchange signed public records and converge on rejoin", async (t) => {
  const { peers, identitySet } = await createPeers(t, 3);
  const [alice, bob, carol] = peers;
  const syncs = [waitForPeerSync([alice, bob]),
    waitForPeerSync([bob, carol]),
    waitForPeerSync([alice, carol])];
  await alice.connect(bob.identity.multiaddrs[0]);
  await bob.connect(carol.identity.multiaddrs[0]);
  await alice.connect(carol.identity.multiaddrs[0]);
  await waitUntil(async () => (await alice.getPeers()).length === 2
    && (await bob.getPeers()).length === 2 && (await carol.getPeers()).length === 2);
  await Promise.all(syncs);

  const message = await signedMessage(
    identitySet.wallets.get("did:amn:alice"),
    "did:amn:alice",
    minerCapabilities(alice.localPeerId),
  );
  const publish = await alice.publish(message);
  assert.equal(publish.delivery, "GOSSIPED");
  await waitUntil(async () => Promise.all([bob, carol].map(async (peer) =>
    (await peer.getPublicEvents()).some((item) => item.id === message.id))).then((items) => items.every(Boolean)));

  const bobEvents = await bob.getPublicEvents();
  const carolEvents = await carol.getPublicEvents();
  assert.deepEqual(bobEvents.map((item) => item.id), carolEvents.map((item) => item.id));
  assert.deepEqual(bobEvents.map((item) => item.id), [message.id]);

  await carol.close();
  const offlineMessage = await signedMessage(
    identitySet.wallets.get("did:amn:bob"),
    "did:amn:bob",
    minerCapabilities(bob.localPeerId, "offline-publication"),
  );
  await bob.publish(offlineMessage);
  await waitUntil(async () => (await alice.getPublicEvents()).some((item) => item.id === offlineMessage.id));
  const replay = await createPeer({
    identitySet,
    principalId: "did:amn:carol",
    storageDirectory: path.join(path.dirname(peers[0].options.storageDirectory), "peer-2"),
    memoryAddress: "amn-carol-rejoin",
  });
  peers[2] = replay;
  identitySet.peerPrincipals.set(replay.localPeerId, "did:amn:carol");
  assert.equal(replay.localPeerId, carol.localPeerId, "persisted libp2p identity must survive restart");
  assert.deepEqual((await replay.getPublicEvents()).map((item) => item.id), [message.id]);
  await replay.connect(alice.identity.multiaddrs[0]);
  await waitUntil(async () => (await alice.getPeers()).includes(replay.localPeerId)
    && (await replay.getPublicEvents()).some((item) => item.id === offlineMessage.id));
  assert.deepEqual((await replay.getPeerScores()), []);

  const sameNonce = structuredClone(offlineMessage);
  sameNonce.id = randomUUID();
  const sameNonceData = typedData(sameNonce, 31337);
  sameNonce.signature = await identitySet.wallets.get("did:amn:bob")
    .signTypedData(sameNonceData.domain, sameNonceData.types, sameNonceData.value);
  await assert.rejects(replay.publish(sameNonce), (error) => error.code === "REPLAY");
});

test("static bootstrap peers provide optional peer discovery", async (t) => {
  const directory = await mkdtemp(path.join(tmpdir(), "amn-bootstrap-"));
  const identitySet = createIdentitySet();
  const peers = [];
  t.after(async () => {
    await Promise.all(peers.map((peer) => peer.close().catch(() => {})));
    await rm(directory, { recursive: true, force: true });
  });
  const seed = await createPeer({
    identitySet,
    principalId: "did:amn:alice",
    storageDirectory: path.join(directory, "seed"),
    memoryAddress: `amn-seed-${randomUUID()}`,
  });
  peers.push(seed);
  identitySet.peerPrincipals.set(seed.localPeerId, "did:amn:alice");
  const joining = await createPeer({
    identitySet,
    principalId: "did:amn:bob",
    storageDirectory: path.join(directory, "joining"),
    memoryAddress: `amn-joining-${randomUUID()}`,
    extra: { bootstrapPeers: seed.identity.multiaddrs },
  });
  peers.push(joining);
  identitySet.peerPrincipals.set(joining.localPeerId, "did:amn:bob");
  await waitUntil(async () => (await seed.getPeers()).includes(joining.localPeerId)
    && (await joining.getPeers()).includes(seed.localPeerId));
  assert.equal(await joining.syncPublicEvents(seed.localPeerId), true);
});

test("real TCP peers use Noise and converge on a signed public message", async (t) => {
  const directory = await mkdtemp(path.join(tmpdir(), "amn-tcp-"));
  const identitySet = createIdentitySet();
  const peers = [];
  t.after(async () => {
    await Promise.all(peers.map((peer) => peer.close().catch(() => {})));
    await rm(directory, { recursive: true, force: true });
  });
  const createTcpPeer = async (index, principalId) => {
    const peer = await createPeerTransport({
      storageDirectory: path.join(directory, `peer-${index}`),
      listenAddresses: ["/ip4/127.0.0.1/tcp/0"],
      discovery: false,
      identityResolver: identitySet.identityResolver,
      peerPrincipalResolver: identitySet.peerPrincipalResolver,
      authorizePrivateSender: identitySet.authorizePrivateSender,
      authorizePrivateRecipient: identitySet.authorizePrivateRecipient,
      requestTimeoutMs: 5_000,
    });
    peers.push(peer);
    identitySet.peerPrincipals.set(peer.localPeerId, principalId);
    return peer;
  };
  const alice = await createTcpPeer(0, "did:amn:alice");
  const bob = await createTcpPeer(1, "did:amn:bob");
  const sync = waitForPeerSync([alice, bob]);
  const address = alice.identity.multiaddrs.find((item) => item.startsWith("/ip4/127.0.0.1/tcp/"));
  assert.ok(address, "TCP peer should expose a loopback multiaddress");
  try {
    await bob.connect(address);
    await sync;
  } catch (error) {
    await sync.catch(() => {});
    throw error;
  }
  const message = await signedMessage(identitySet.wallets.get("did:amn:alice"), "did:amn:alice", minerCapabilities(alice.localPeerId));
  const delivery = await alice.publish(message);
  assert.equal(delivery.delivery, "GOSSIPED");
  await waitUntil(async () => (await bob.getPublicEvents()).some((item) => item.id === message.id));
});

test("private task requests are encrypted unicast, identity-bound, and not replicated", async (t) => {
  const { peers, identitySet } = await createPeers(t, 3);
  const [alice, bob, carol] = peers;
  const syncAB = waitForPeerSync([alice, bob]);
  const syncAC = waitForPeerSync([alice, carol]);
  await alice.connect(bob.identity.multiaddrs[0]);
  await alice.connect(carol.identity.multiaddrs[0]);
  await waitUntil(async () => (await alice.getPeers()).length === 2);
  await Promise.all([syncAB, syncAC]);
  let executions = 0;
  bob.onRequestHandler = async ({ envelope }) => {
    executions += 1;
    return signedMessage(
      identitySet.wallets.get("did:amn:bob"),
      "did:amn:bob",
      verificationReceipt({ peerId: bob.localPeerId, taskId: envelope.payload.taskId }),
    );
  };
  const request = await signedMessage(
    identitySet.wallets.get("did:amn:alice"),
    "did:amn:alice",
    customerTask(),
  );
  const response = await alice.request(bob.localPeerId, request);
  assert.equal(response.principalId, "did:amn:bob");
  assert.equal(response.payload.kind, "VerificationReceipt");
  assert.equal(executions, 1);
  assert.deepEqual(await carol.getPublicEvents(), []);

  const repeated = await alice.request(bob.localPeerId, request);
  assert.equal(repeated.id, response.id);
  assert.equal(executions, 1, "a repeated request returns cached response and does not execute twice");
  await assert.rejects(alice.request(carol.localPeerId, request),
    (error) => error.code === "MESSAGE_ID_CONFLICT");

  await bob.close();
  await waitUntil(async () => !(await alice.getPeers()).includes(bob.localPeerId));
  const unavailable = await signedMessage(
    identitySet.wallets.get("did:amn:alice"),
    "did:amn:alice",
    customerTask(),
  );
  await assert.rejects(alice.request(bob.localPeerId, unavailable),
    (error) => error.code === "PEER_UNAVAILABLE");
});

test("private messages reject a peer-to-principal impersonation", async (t) => {
  const { peers, identitySet } = await createPeers(t, 2);
  const [alice, bob] = peers;
  const sync = waitForPeerSync([alice, bob]);
  await alice.connect(bob.identity.multiaddrs[0]);
  await waitUntil(async () => (await alice.getPeers()).includes(bob.localPeerId));
  await sync;
  identitySet.peerPrincipals.set(alice.localPeerId, "did:amn:carol");
  const request = await signedMessage(
    identitySet.wallets.get("did:amn:alice"),
    "did:amn:alice",
    customerTask(),
  );
  await assert.rejects(alice.request(bob.localPeerId, request), (error) => error.code === "UNAUTHORIZED");
});

test("public event store rejects changed duplicate IDs and keeps deterministic pages", async (t) => {
  const { peers, identitySet } = await createPeers(t, 1);
  const [peer] = peers;
  const wallet = identitySet.wallets.get("did:amn:alice");
  const first = await signedMessage(wallet, "did:amn:alice", minerCapabilities(peer.localPeerId));
  await peer.publish(first);
  const changed = await signedMessage(wallet, "did:amn:alice", minerCapabilities(peer.localPeerId));
  changed.id = first.id;
  const { typedData } = await import("@ai-mining-network/protocol");
  const data = typedData(changed, 31337);
  changed.signature = await wallet.signTypedData(data.domain, data.types, data.value);
  await assert.rejects(peer.publish(changed), (error) => error.code === "MESSAGE_ID_CONFLICT");
  await assert.rejects(peer.publish(first), (error) => error.code === "DUPLICATE");
  const invalidSignature = structuredClone(await signedMessage(wallet, "did:amn:alice", minerCapabilities(peer.localPeerId)));
  invalidSignature.payload.cpuCores = 1;
  await assert.rejects(peer.publish(invalidSignature), (error) => error.code === "INVALID_SIGNATURE");
  await assert.rejects(peer.publish({ ...first, payload: customerTask() }),
    (error) => error.code === "PRIVATE_MESSAGE_REQUIRES_UNICAST");
  const stored = await peer.getPublicEvents();
  stored[0].payload.peerId = "mutated-copy";
  assert.equal((await peer.getPublicEvents())[0].payload.peerId, peer.localPeerId);
});

test("configuration rejects invalid sync bounds and missing persistent identity storage", async () => {
  const identities = createIdentitySet();
  const common = {
    storageDirectory: path.join(tmpdir(), `amn-config-${randomUUID()}`),
    identityResolver: identities.identityResolver,
    peerPrincipalResolver: identities.peerPrincipalResolver,
    authorizePrivateSender: identities.authorizePrivateSender,
    authorizePrivateRecipient: identities.authorizePrivateRecipient,
    testOnly: true,
    testOnlyModules: { transports: [memory()], peerDiscovery: [] },
  };
  await assert.rejects(createPeerTransport({ ...common, syncPageEvents: 0 }),
    (error) => error.code === "INVALID_CONFIGURATION");
  await assert.rejects(createPeerTransport({ ...common, maxMessageBytes: 16 }),
    (error) => error.code === "INVALID_CONFIGURATION");
  await assert.rejects(createPeerTransport({ ...common, storageDirectory: undefined }),
    (error) => error.code === "STORAGE_REQUIRED");
});

test("rate limits bound message and byte volume and cap tracked peer state", () => {
  const limiter = new PeerRateLimiter({ windowMs: 100, maxMessages: 2, maxBytes: 5, maxPeers: 1 });
  assert.equal(limiter.consume("peer-a", 3, 1000), true);
  assert.equal(limiter.consume("peer-a", 3, 1001), false);
  assert.equal(limiter.consume("peer-a", 1, 1002), true);
  assert.equal(limiter.consume("peer-b", 1, 1003), true);
  assert.equal(limiter.consume("peer-a", 1, 1004), true, "bounded LRU state safely forgets evicted peer windows");
  assert.throws(() => new PeerRateLimiter({ maxPeers: 0 }), /positive safe integers/);
});

test("length-framed RPC rejects truncated and oversized frames", async () => {
  const expected = { op: "SYNC_PAGE", after: "" };
  const frame = encodeFrame(expected, 512);
  const stream = { async *[Symbol.asyncIterator]() { yield frame.subarray(0, 3); yield frame.subarray(3); } };
  assert.deepEqual(await readFrame(stream, 512), expected);
  await assert.rejects(readFrame({ async *[Symbol.asyncIterator]() { yield new Uint8Array([0, 0]); } }, 512),
    (error) => error.code === "INVALID_FRAME");
  await assert.rejects(readFrame({ async *[Symbol.asyncIterator]() { yield new Uint8Array([0, 0, 2, 1]); } }, 512),
    (error) => error.code === "MESSAGE_TOO_LARGE");
});
