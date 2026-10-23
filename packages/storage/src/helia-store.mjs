import { createHash } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { bootstrap } from "@libp2p/bootstrap";
import { gossipsub } from "@libp2p/gossipsub";
import { identify } from "@libp2p/identify";
import { kadDHT } from "@libp2p/kad-dht";
import { noise } from "@libp2p/noise";
import { ping } from "@libp2p/ping";
import { tcp } from "@libp2p/tcp";
import { yamux } from "@libp2p/yamux";
import { multiaddr } from "@multiformats/multiaddr";
import { FsBlockstore } from "blockstore-fs";
import { MemoryDatastore } from "datastore-core";
import { LevelDatastore } from "datastore-level";
import { createHeliaLight } from "helia";
import * as dagCbor from "@ipld/dag-cbor";
import { unixfs } from "@helia/unixfs";
import { withBitswap } from "@helia/bitswap";
import { withLibp2pLight } from "@helia/libp2p";
import { CID } from "multiformats/cid";
import { createGrantEnvelope, decryptPrivateBytes, encryptPrivateBytes } from "./private-encryption.mjs";

const OWNER = "ai-mining-network/artifact-store/v1";
const PUBLIC_KINDS = new Set(["skill-package", "benchmark", "public-receipt"]);
const DEFAULT_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024;
const DEFAULT_TIMEOUT_MS = 15_000;
const DEFAULT_ROUTING_TIMEOUT_MS = 5_000;

/**
 * Persistent Helia/IPFS artifact adapter. Customer files are private by default:
 * only the explicit public publishing method can create a public artifact.
 */
export class HeliaArtifactStore {
  #options;
  #helia;
  #unixfs;
  #revoked = new Set();
  #grants = new Map();

  constructor({
    storageDir,
    listenAddresses = [],
    bootstrapPeers = [],
    maxArtifactBytes = DEFAULT_MAX_ARTIFACT_BYTES,
    routingTimeoutMs = DEFAULT_ROUTING_TIMEOUT_MS,
    publicAuthorization = async () => false,
    privateGrantPolicy = async () => false,
    libp2pNode,
  }) {
    if (!storageDir) throw new TypeError("storageDir is required");
    if (!Number.isSafeInteger(maxArtifactBytes) || maxArtifactBytes <= 0) {
      throw new TypeError("maxArtifactBytes must be a positive safe integer");
    }
    if (!Number.isSafeInteger(routingTimeoutMs) || routingTimeoutMs <= 0) {
      throw new TypeError("routingTimeoutMs must be a positive safe integer");
    }
    this.#options = {
      storageDir: path.resolve(storageDir),
      listenAddresses,
      bootstrapPeers,
      maxArtifactBytes,
      routingTimeoutMs,
      publicAuthorization,
      privateGrantPolicy,
      libp2pNode,
    };
  }

  async open() {
    if (this.#helia) return this;
    const { storageDir, listenAddresses, bootstrapPeers } = this.#options;
    await mkdir(storageDir, { recursive: true });
    const blocksDir = path.join(storageDir, "blocks");
    const datastoreDir = path.join(storageDir, "datastore");
    await Promise.all([mkdir(blocksDir, { recursive: true }), mkdir(datastoreDir, { recursive: true })]);
    const blockstore = new StartableFsBlockstore(blocksDir);
    const datastore = new StartableLevelDatastore(datastoreDir);
    let helia = createHeliaLight({
      blockstore,
      datastore,
      // OrbitDB manifests/log entries use DAG-CBOR blocks.
      codecs: [dagCbor],
    });
    if (this.#options.libp2pNode) {
      helia = attachExternalLibp2p(helia, this.#options.libp2pNode);
    } else {
      const peerDiscovery = bootstrapPeers.length > 0
        ? [bootstrap({ list: [...bootstrapPeers] })]
        : [];
      helia = withLibp2pLight(helia, {
        addresses: { listen: [...listenAddresses] },
        transports: [tcp()],
        connectionEncrypters: [noise()],
        streamMuxers: [yamux()],
        peerDiscovery,
        services: {
          identify: identify(),
          ping: ping(),
          // OrbitDB synchronization uses Helia's libp2p node and requires pubsub.
          // A public OrbitDB database applies its own signed-record validation.
          pubsub: gossipsub({ allowPublishToZeroTopicPeers: true }),
          ...(bootstrapPeers.length > 0
            ? { dht: kadDHT({ clientMode: false, protocol: "/ipfs/kad/1.0.0" }) }
            : {}),
        },
        // Peer cache and DHT routing are reconstructible. Keeping their writes
        // separate avoids closing a shared persistent datastore during libp2p shutdown.
        datastore: new MemoryDatastore(),
      });
    }
    this.#helia = withBitswap(helia);
    try {
      // Helia checks its datastore version before invoking its start lifecycle.
      // Open persistent stores first; their start bridges are idempotent when Helia starts them again.
      await Promise.all([blockstore.start(), datastore.start()]);
      await this.#helia.start();
      await this.#loadRevocations(storageDir);
      this.#helia.routing.addRouter(createConnectedPeerRouter(this.#helia));
      this.#unixfs = unixfs(this.#helia);
    } catch (error) {
      this.#helia = undefined;
      await helia.stop().catch(() => {});
      await Promise.allSettled([blockstore.close(), datastore.close()]);
      throw error;
    }
    return this;
  }

  get peerId() {
    this.#assertOpen();
    return this.#helia.libp2p.peerId.toString();
  }

  /**
   * Return the live Helia instance for protocol adapters such as OrbitDB.
   * Callers may use its documented Helia APIs but must not stop it; this store
   * retains lifecycle ownership. A fresh instance is returned after each open.
   */
  getHeliaInstance() {
    this.#assertOpen();
    return this.#helia;
  }

  get multiaddrs() {
    this.#assertOpen();
    return this.#helia.libp2p.getMultiaddrs().map((address) => address.toString());
  }

  async connect(address) {
    this.#assertOpen();
    const connection = await this.#helia.libp2p.dial(multiaddr(address));
    await this.#announcePublicPins();
    return connection.remotePeer.toString();
  }

  async announce(cid) {
    this.#assertOpen();
    const parsed = CID.parse(cid);
    const pin = await this.#helia.pins.get(parsed);
    if (pin.metadata?.amn_owner !== OWNER) throw new Error("Only artifacts pinned by this store can be announced");
    try {
      await this.#withRoutingTimeout((signal) => this.#helia.routing.provide(parsed, { signal }));
      return { cid: parsed.toString(), status: "announced-to-configured-routing" };
    } catch {
      return { cid: parsed.toString(), status: "failed; pinned locally" };
    }
  }

  /** Store an explicitly authorized public skill, benchmark, or public receipt. */
  async putPublic(input, { kind, authorization, retentionMs } = {}) {
    this.#assertOpen();
    if (!PUBLIC_KINDS.has(kind)) throw new Error("Public artifact kind is not allowed");
    if (await this.#options.publicAuthorization({ kind, authorization }) !== true) {
      throw new Error("Public artifact publication was not explicitly authorized");
    }
    const bytes = this.#toBytes(input);
    const cid = await this.#unixfs.addBytes(bytes);
    const sha256 = digest(bytes);
    const expiresAt = getExpiry(retentionMs);
    await this.#pin(cid, { kind, visibility: "public", expiresAt });
    let providerAnnouncement = "not-requested";
    if (this.#helia.libp2p.getConnections().length > 0) {
      try {
        await this.#withRoutingTimeout((signal) => this.#helia.routing.provide(cid, { signal }));
        providerAnnouncement = "announced-to-configured-routing";
      } catch {
        providerAnnouncement = "failed; pinned locally";
      }
    }
    return {
      cid: cid.toString(),
      sha256,
      size: bytes.length,
      visibility: "public",
      pinStatus: "pinned-local",
      expiresAt,
      providerAnnouncement,
    };
  }

  /**
   * Encrypt before adding bytes to Helia. Grants are returned only to the caller
   * and are never written into public pin metadata or sent to peers automatically.
   */
  async putPrivate(input, { taskId, recipients, expiresAt, retentionMs } = {}) {
    this.#assertOpen();
    if (typeof taskId !== "string" || taskId.length === 0 || taskId.length > 200) {
      throw new TypeError("taskId is required and must be at most 200 characters");
    }
    if (!Array.isArray(recipients) || recipients.length === 0 || recipients.length > 20) {
      throw new TypeError("Provide between 1 and 20 explicitly assigned recipients");
    }
    const plaintext = this.#toBytes(input);
    const expiry = normalizeExpiry(expiresAt, retentionMs);
    const recipientIds = new Set();
    for (const recipient of recipients) {
      if (!recipient?.principalId || typeof recipient.publicKey !== "string") {
        throw new TypeError("Each recipient requires principalId and an X25519 publicKey");
      }
      if (recipientIds.has(recipient.principalId)) throw new Error("Duplicate private artifact recipient");
      recipientIds.add(recipient.principalId);
      const authorized = await this.#options.privateGrantPolicy({
        action: "issue",
        taskId,
        principalId: recipient.principalId,
        expiresAt: expiry,
      });
      if (authorized !== true) throw new Error(`Private artifact recipient is not authorized: ${recipient.principalId}`);
    }

    const { dek, storedBytes } = encryptPrivateBytes(plaintext, taskId);
    const cid = await this.#unixfs.addBytes(storedBytes);
    const encryptedSha256 = digest(storedBytes);
    const localExpiry = retentionMs === undefined ? Date.parse(expiry) : getExpiry(retentionMs);
    await this.#pin(cid, { kind: "private-encrypted", visibility: "encrypted-private", expiresAt: localExpiry });
    const grants = recipients.map((recipient) => createGrantEnvelope({
      dek,
      cid: cid.toString(),
      taskId,
      recipientPrincipalId: recipient.principalId,
      recipientPublicKey: recipient.publicKey,
      encryptedSha256,
      expiresAt: expiry,
    }));
    for (const grant of grants) this.#grants.set(grant.grantId, grant);
    dek.fill(0);
    return {
      cid: cid.toString(),
      encryptedSha256,
      size: storedBytes.length,
      visibility: "encrypted-private",
      pinStatus: "pinned-local",
      expiresAt: expiry,
      grants,
    };
  }

  /** Retrieve only after the caller validates signed expected CID and SHA-256 values. */
  async get({ cid, expectedCid, expectedSha256, timeoutMs = DEFAULT_TIMEOUT_MS }) {
    this.#assertOpen();
    const parsedCid = parseExpectedCid(cid, expectedCid);
    const bytes = await this.#readBounded(parsedCid, timeoutMs);
    const actualSha256 = digest(bytes);
    if (!constantTimeHexEqual(actualSha256, expectedSha256)) {
      throw new Error("Artifact SHA-256 does not match the signed expected hash");
    }
    const recomputedCid = await this.#unixfs.addBytes(bytes);
    if (!recomputedCid.equals(parsedCid)) throw new Error("Artifact bytes do not match the expected CID");
    return bytes;
  }

  async getPrivate({ envelope, recipientPrincipalId, recipientPrivateKey, timeoutMs = DEFAULT_TIMEOUT_MS }) {
    this.#assertOpen();
    if (!envelope || envelope.protocolVersion !== "amn.private-grant.v1") throw new Error("Invalid private grant");
    if (this.#revoked.has(envelope.grantId)) throw new Error("Private grant has been revoked");
    if (Date.parse(envelope.expiresAt) <= Date.now()) throw new Error("Private grant has expired");
    if (envelope.recipientPrincipalId !== recipientPrincipalId) throw new Error("Private grant is assigned to another recipient");
    const known = this.#grants.get(envelope.grantId);
    if (known && JSON.stringify(known) !== JSON.stringify(envelope)) throw new Error("Private grant does not match issued grant");
    if (await this.#options.privateGrantPolicy({
      action: "read",
      grantId: envelope.grantId,
      taskId: envelope.taskId,
      principalId: recipientPrincipalId,
      cid: envelope.cid,
      expiresAt: envelope.expiresAt,
    }) !== true) throw new Error("Private artifact access is not authorized");
    const storedBytes = await this.get({
      cid: envelope.cid,
      expectedCid: envelope.cid,
      expectedSha256: envelope.encryptedSha256,
      timeoutMs,
    });
    return decryptPrivateBytes(storedBytes, { envelope, recipientPrincipalId, recipientPrivateKey });
  }

  async revokeGrant(grantId) {
    this.#assertOpen();
    const envelope = this.#grants.get(grantId);
    if (!envelope) throw new Error("Unknown private grant");
    if (await this.#options.privateGrantPolicy({
      action: "revoke",
      grantId,
      taskId: envelope.taskId,
      principalId: envelope.recipientPrincipalId,
      cid: envelope.cid,
    }) !== true) throw new Error("Private grant revocation is not authorized");
    this.#revoked.add(grantId);
    await this.#persistRevocations();
  }

  async pin({ cid, expectedCid, expectedSha256, retentionMs } = {}) {
    const bytes = await this.get({ cid, expectedCid, expectedSha256 });
    const parsedCid = CID.parse(expectedCid);
    await this.#pin(parsedCid, { kind: "replica", visibility: "replicated", expiresAt: getExpiry(retentionMs) });
    return { cid: parsedCid.toString(), sha256: digest(bytes), size: bytes.length, pinStatus: "pinned-local" };
  }

  async getStatus(cid) {
    this.#assertOpen();
    const parsed = CID.parse(cid);
    const local = await this.#helia.blockstore.has(parsed);
    const pinned = await this.#helia.pins.isPinned(parsed);
    return {
      cid: parsed.toString(),
      availability: pinned ? "local-pinned" : local ? "local-unpinned" : "not-local",
      local,
      pinned,
      scope: "local-node-only; no global availability claim",
    };
  }

  async releaseExpired(now = Date.now()) {
    this.#assertOpen();
    const expired = [];
    for await (const pin of this.#helia.pins.ls()) {
      if (pin.metadata?.amn_owner !== OWNER) continue;
      const expiresAt = pin.metadata?.amn_expires_at;
      if (typeof expiresAt !== "number" || expiresAt > now) continue;
      for await (const _ of this.#helia.pins.rm(pin.cid)) { /* drain unpin iterator */ }
      expired.push(pin.cid.toString());
    }
    if (expired.length > 0) await this.#helia.gc();
    for (const [grantId, envelope] of this.#grants) {
      if (Date.parse(envelope.expiresAt) <= now) {
        this.#revoked.add(grantId);
        this.#grants.delete(grantId);
      }
    }
    return expired;
  }

  async close() {
    if (!this.#helia) return;
    const helia = this.#helia;
    this.#helia = undefined;
    this.#unixfs = undefined;
    try {
      await helia.stop();
    } catch (error) {
      await Promise.allSettled([helia.blockstore.unwrap().close?.(), helia.datastore.close?.()]);
      throw error;
    }
  }

  async #loadRevocations(storageDir) {
    const file = path.join(storageDir, "private-grant-revocations.json");
    try {
      const parsed = JSON.parse(await readFile(file, "utf8"));
      if (!Array.isArray(parsed.revokedGrantIds) || parsed.revokedGrantIds.some((id) => typeof id !== "string")) {
        throw new Error("Invalid persisted grant revocation state");
      }
      this.#revoked = new Set(parsed.revokedGrantIds);
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      this.#revoked = new Set();
    }
    this.#revocationFile = file;
  }

  async #persistRevocations() {
    const temporary = `${this.#revocationFile}.${process.pid}.tmp`;
    await writeFile(temporary, `${JSON.stringify({ version: 1, revokedGrantIds: [...this.#revoked].sort() })}\n`, { flag: "w" });
    await rename(temporary, this.#revocationFile);
  }

  async #withRoutingTimeout(action) {
    const signal = AbortSignal.timeout(this.#options.routingTimeoutMs);
    let rejectOnAbort;
    const timedOut = new Promise((_, reject) => {
      rejectOnAbort = () => reject(signal.reason);
      signal.addEventListener("abort", rejectOnAbort, { once: true });
    });
    try {
      return await Promise.race([action(signal), timedOut]);
    } finally {
      signal.removeEventListener("abort", rejectOnAbort);
    }
  }

  #assertOpen() {
    if (!this.#helia || !this.#unixfs) throw new Error("HeliaArtifactStore is not open");
  }

  #toBytes(input) {
    const bytes = typeof input === "string" ? Buffer.from(input) : Buffer.from(input);
    if (bytes.length > this.#options.maxArtifactBytes) throw new Error("Artifact exceeds configured size limit");
    return bytes;
  }

  async #readBounded(cid, timeoutMs) {
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0) throw new TypeError("timeoutMs must be a positive safe integer");
    const signal = AbortSignal.timeout(timeoutMs);
    const chunks = [];
    let size = 0;
    for await (const chunk of this.#unixfs.cat(cid, { signal })) {
      size += chunk.length;
      if (size > this.#options.maxArtifactBytes) throw new Error("Retrieved artifact exceeds configured size limit");
      chunks.push(Buffer.from(chunk));
    }
    if (size === 0) throw new Error("Artifact is empty or unavailable");
    return Buffer.concat(chunks, size);
  }

  async #pin(cid, { kind, visibility, expiresAt }) {
    const metadata = {
      amn_owner: OWNER,
      amn_kind: kind,
      amn_visibility: visibility,
    };
    if (expiresAt !== null) metadata.amn_expires_at = expiresAt;
    for await (const _ of this.#helia.pins.add(cid, { depth: Infinity, metadata })) { /* drain pin iterator */ }
  }

  async #announcePublicPins() {
    for await (const pin of this.#helia.pins.ls()) {
      if (pin.metadata?.amn_owner !== OWNER || pin.metadata?.amn_visibility !== "public") continue;
      try {
        await this.#withRoutingTimeout((signal) => this.#helia.routing.provide(pin.cid, { signal }));
      } catch {
        // A pin remains locally available when no DHT peers/bootstrap nodes are reachable.
      }
    }
  }

  #revocationFile;
}

function createConnectedPeerRouter(helia) {
  return {
    name: "amn-connected-peers",
    async *findProviders(cid, options = {}) {
      if (options.signal?.aborted) return;
      for (const connection of helia.libp2p.getConnections()) {
        if (options.signal?.aborted) return;
        // A connected peer is a candidate source, not a proof that it has the
        // requested block. Bitswap verifies received blocks against their CIDs.
        yield {
          id: connection.remotePeer.toCID(),
          multiaddrs: [connection.remoteAddr],
          router: "amn-connected-peers",
          fallback: false,
        };
      }
    },
  };
}

function attachExternalLibp2p(helia, node) {
  if (typeof node?.start !== "function" || typeof node?.stop !== "function"
    || typeof node?.getConnections !== "function" || !node.services?.pubsub) {
    throw new TypeError("libp2pNode must be a started-capable libp2p instance with a pubsub service");
  }
  Object.defineProperty(helia, "libp2p", {
    configurable: true,
    enumerable: true,
    get: () => node,
  });
  // The peer transport owns this node's lifecycle. Helia only borrows it for
  // Bitswap, direct-peer routing and OrbitDB's pubsub synchronization.
  helia.addMixin({
    name: "amn-external-libp2p",
    start: async () => { await node.start(); },
    stop: async () => {},
  });
  return helia;
}

class StartableFsBlockstore extends FsBlockstore {
  #started = false;
  isStarted() { return this.#started; }
  async start() { if (!this.#started) { await this.open(); this.#started = true; } }
  async stop() { if (this.#started) { await this.close(); this.#started = false; } }
}

class StartableLevelDatastore extends LevelDatastore {
  #started = false;
  isStarted() { return this.#started; }
  async start() { if (!this.#started) { await this.open(); this.#started = true; } }
  async stop() { if (this.#started) { await this.close(); this.#started = false; } }
}

function parseExpectedCid(cid, expectedCid) {
  if (typeof expectedCid !== "string" || !expectedCid) throw new TypeError("expectedCid is required");
  if (expectedCid !== cid) throw new Error("CID does not match the signed expected CID");
  return CID.parse(expectedCid);
}

function digest(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function constantTimeHexEqual(actual, expected) {
  if (typeof expected !== "string" || !/^[a-f0-9]{64}$/i.test(expected)) return false;
  const left = Buffer.from(actual, "hex");
  const right = Buffer.from(expected, "hex");
  return left.length === right.length && left.equals(right);
}

function normalizeExpiry(expiresAt, retentionMs) {
  if (expiresAt !== undefined) {
    const time = Date.parse(expiresAt);
    if (!Number.isFinite(time) || time <= Date.now()) throw new TypeError("expiresAt must be a future ISO timestamp");
    return new Date(time).toISOString();
  }
  if (retentionMs !== undefined) {
    if (!Number.isSafeInteger(retentionMs) || retentionMs <= 0) throw new TypeError("retentionMs must be a positive safe integer");
    return new Date(Date.now() + retentionMs).toISOString();
  }
  return new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString();
}

function getExpiry(retentionMs) {
  if (retentionMs === undefined || retentionMs === null) return null;
  if (!Number.isSafeInteger(retentionMs) || retentionMs <= 0) throw new TypeError("retentionMs must be a positive safe integer");
  return Date.now() + retentionMs;
}
