import { generateKeyPair, privateKeyFromProtobuf, privateKeyToProtobuf } from "@libp2p/crypto/keys";
import { gossipsub } from "@libp2p/gossipsub";
import { identify } from "@libp2p/identify";
import { mdns } from "@libp2p/mdns";
import { noise } from "@libp2p/noise";
import { tcp } from "@libp2p/tcp";
import { yamux } from "@libp2p/yamux";
import { multiaddr } from "@multiformats/multiaddr";
import { createLibp2p } from "libp2p";
import { randomUUID } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { EventEmitter } from "node:events";
import { verifyEnvelope } from "@ai-mining-network/protocol";
import { FileReplayStore } from "@ai-mining-network/protocol/node-replay-store";
import { encodeFrame, readFrame, writeFrame } from "./framing.mjs";
import {
  MAX_DIRECT_FRAME_BYTES,
  MAX_PUBLIC_MESSAGE_BYTES,
  PUBLIC_MESSAGE_KINDS,
  PUBLIC_TOPIC,
  RPC_PROTOCOL,
  PeerTransportError,
  assertPrivateEnvelope,
  assertPublicEnvelope,
  canonicalEnvelopeString,
  parseCanonicalEnvelope,
  serializeEnvelope,
  validateSignedEnvelope,
} from "./protocol.mjs";
import { PeerRateLimiter, PeerScoreStore, PublicEventStore, publicEventKey } from "./stores.mjs";

const DEFAULT_SYNC_PAGE_EVENTS = 32;
const MAX_SYNC_PAGE_EVENTS = 128;
const MAX_RESPONSE_CACHE = 2_048;

async function loadOrCreatePrivateKey(storageDirectory, providedKey) {
  if (providedKey != null) return providedKey;
  const keyFile = path.join(storageDirectory, "libp2p-identity.protobuf");
  await mkdir(storageDirectory, { recursive: true, mode: 0o700 });
  try {
    return privateKeyFromProtobuf(await readFile(keyFile));
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const key = await generateKeyPair("Ed25519");
  try {
    await writeFile(keyFile, privateKeyToProtobuf(key), { flag: "wx", mode: 0o600 });
    return key;
  } catch (error) {
    if (error.code !== "EEXIST") throw error;
    return privateKeyFromProtobuf(await readFile(keyFile));
  }
}

function peerString(peer) {
  return typeof peer === "string" ? peer : peer?.toString?.() ?? String(peer);
}

function structuredError(error) {
  return {
    code: error?.code ?? "REQUEST_FAILED",
    message: String(error?.message ?? "request failed").slice(0, 256),
  };
}

export class Libp2pPeerTransport extends EventEmitter {
  static async create(options) {
    const storageDirectory = options.storageDirectory;
    if (typeof storageDirectory !== "string" || storageDirectory.length === 0) {
      throw new PeerTransportError("STORAGE_REQUIRED", "a persistent storageDirectory is required for stable identity and replay protection");
    }
    if (typeof options.identityResolver !== "function") {
      throw new PeerTransportError("IDENTITY_RESOLVER_REQUIRED", "network messages require an active EIP-712 identity resolver");
    }
    if (typeof options.peerPrincipalResolver !== "function"
      || typeof options.authorizePrivateSender !== "function"
      || typeof options.authorizePrivateRecipient !== "function") {
      throw new PeerTransportError("PRIVATE_AUTH_UNAVAILABLE", "peer-to-principal binding and both private message authorization policies are required");
    }
    if (options.testOnlyModules != null && !options.testOnly) {
      throw new PeerTransportError("TEST_MODULES_FORBIDDEN", "custom libp2p modules require testOnly: true");
    }
    if (options.testOnlyModules != null && options.deployment === "production") {
      throw new PeerTransportError("TEST_MODULES_FORBIDDEN", "production peers cannot use test-only libp2p modules");
    }
    const privateKey = await loadOrCreatePrivateKey(storageDirectory, options.privateKey);
    const maxMessageBytes = options.maxMessageBytes ?? MAX_PUBLIC_MESSAGE_BYTES;
    const maxFrameBytes = options.maxFrameBytes ?? MAX_DIRECT_FRAME_BYTES;
    const syncPageEvents = options.syncPageEvents ?? DEFAULT_SYNC_PAGE_EVENTS;
    if (!Number.isSafeInteger(syncPageEvents) || syncPageEvents < 1 || syncPageEvents > MAX_SYNC_PAGE_EVENTS) {
      throw new PeerTransportError("INVALID_CONFIGURATION", `syncPageEvents must be an integer from 1 to ${MAX_SYNC_PAGE_EVENTS}`);
    }
    if (!Number.isSafeInteger(maxMessageBytes) || maxMessageBytes < 1024 || maxMessageBytes > MAX_PUBLIC_MESSAGE_BYTES) {
      throw new PeerTransportError("INVALID_CONFIGURATION", `maxMessageBytes must be from 1024 to ${MAX_PUBLIC_MESSAGE_BYTES}`);
    }
    if (!Number.isSafeInteger(maxFrameBytes) || maxFrameBytes < maxMessageBytes || maxFrameBytes > 4 * MAX_DIRECT_FRAME_BYTES) {
      throw new PeerTransportError("INVALID_CONFIGURATION", `maxFrameBytes must be at least maxMessageBytes and at most ${4 * MAX_DIRECT_FRAME_BYTES}`);
    }
    const transports = options.testOnlyModules?.transports ?? [tcp()];
    const discoveryModules = options.testOnlyModules?.peerDiscovery ?? (options.discovery === false ? [] : [mdns()]);
    const listenAddresses = options.listenAddresses ?? ["/ip4/0.0.0.0/tcp/0"];
    const pubsubService = gossipsub({
      globalSignaturePolicy: "StrictSign",
      allowedTopics: [PUBLIC_TOPIC],
      allowPublishToZeroTopicPeers: true,
      maxInboundDataLength: maxMessageBytes,
      maxInboundStreams: 8,
      maxOutboundStreams: 8,
      maxOutboundBufferSize: maxMessageBytes * 4,
      maxTopicBytesPerPeer: 16 * 1024,
      messageProcessingConcurrency: 8,
      decodeRpcLimits: {
        maxMessages: 128,
        maxSubscriptions: 16,
        maxIhaveMessageIDs: 256,
        maxIwantMessageIDs: 256,
        maxIdontwantMessageIDs: 256,
        maxControlMessages: 256,
        maxPeerInfos: 64,
      },
      emitSelf: false,
      asyncValidation: true,
    });
    const node = await createLibp2p({
      start: false,
      privateKey,
      addresses: { listen: listenAddresses },
      transports,
      connectionEncrypters: [noise()],
      streamMuxers: [yamux()],
      peerDiscovery: discoveryModules,
      services: {
        identify: identify(),
        pubsub: pubsubService,
      },
      connectionManager: { maxConnections: options.maxConnections ?? 64 },
    });

    const transport = new Libp2pPeerTransport(node, options, {
      maxMessageBytes,
      maxFrameBytes,
    });
    try {
      await transport.start();
      return transport;
    } catch (error) {
      await node.stop().catch(() => {});
      throw error;
    }
  }

  constructor(node, options, limits) {
    super();
    this.node = node;
    this.mode = "libp2p";
    this.options = options;
    this.chainId = options.chainId ?? 31337;
    this.identityResolver = options.identityResolver;
    this.peerPrincipalResolver = options.peerPrincipalResolver;
    this.authorizePrivateSender = options.authorizePrivateSender;
    this.authorizePrivateRecipient = options.authorizePrivateRecipient;
    this.onRequestHandler = options.onRequest;
    this.maxMessageBytes = limits.maxMessageBytes;
    this.maxFrameBytes = limits.maxFrameBytes;
    this.requestTimeoutMs = options.requestTimeoutMs ?? 15_000;
    this.syncPageEvents = options.syncPageEvents ?? DEFAULT_SYNC_PAGE_EVENTS;
    this.scoreBanThreshold = options.scoreBanThreshold ?? -30;
    this.replayStore = new FileReplayStore(path.join(options.storageDirectory, "message-replays"));
    this.eventStore = new PublicEventStore(path.join(options.storageDirectory, "public-events.json"), {
      maxEvents: options.maxPublicEvents ?? 10_000,
    });
    this.peerScores = new PeerScoreStore(path.join(options.storageDirectory, "peer-scores.json"));
    this.rateLimiter = new PeerRateLimiter(options.rateLimits);
    this.responseCache = new Map();
    this.outboundResponseCache = new Map();
    this.syncJobs = new Map();
    this.dialingAddresses = new Set();
    this.localPeerId = node.peerId.toString();
    this.identity = {
      peerId: this.localPeerId,
      multiaddrs: node.getMultiaddrs().map((address) => address.toString()),
    };
  }

  async start() {
    await Promise.all([this.eventStore.load(), this.peerScores.load()]);
    for (const envelope of await this.eventStore.snapshot()) {
      assertPublicEnvelope(envelope);
      serializeEnvelope(envelope, this.maxMessageBytes);
      if (!verifyEnvelope(envelope, this.chainId)) {
        throw new PeerTransportError("CORRUPT_EVENT_STORE", "persisted public event failed its signature or payload hash check");
      }
    }

    const pubsub = this.node.services.pubsub;
    pubsub.topicValidators.set(PUBLIC_TOPIC, async (sourcePeer, message) => {
      const source = peerString(sourcePeer);
      try {
        if (!this.rateLimiter.consume(source, message.data.byteLength)) {
          await this.#penalize(source, "RATE_LIMIT", -5);
          return "reject";
        }
        const envelope = parseCanonicalEnvelope(message.data, this.maxMessageBytes);
        assertPublicEnvelope(envelope);
        await validateSignedEnvelope(envelope, {
          chainId: this.chainId,
          identityResolver: this.identityResolver,
        });
        return "accept";
      } catch (error) {
        if (error.code !== "REPLAY") await this.#penalize(source, error.code ?? "INVALID_MESSAGE", -5);
        return "reject";
      }
    });
    pubsub.addEventListener("message", (event) => {
      if (event.detail.topic === PUBLIC_TOPIC) {
        void this.#ingestPublic(event.detail.data, event.detail.from?.toString?.() ?? "unknown");
      }
    });
    this.node.addEventListener("peer:discovery", (event) => {
      const peerId = event.detail?.id?.toString?.();
      void this.#dialDiscovered(event.detail.multiaddrs ?? [], peerId);
    });
    this.node.addEventListener("peer:connect", (event) => {
      const peerId = peerString(event.detail);
      this.#emitSafe("peer-connected", { peerId });
      // A deterministic initiator prevents reciprocal peers from opening
      // duplicate anti-entropy streams at the same time.
      if (this.localPeerId.localeCompare(peerId) < 0) void this.#syncPeer(peerId);
    });
    this.node.addEventListener("peer:disconnect", (event) => {
      this.#emitSafe("peer-disconnected", { peerId: peerString(event.detail) });
    });
    await this.node.handle(RPC_PROTOCOL, async (stream, connection) => {
      await this.#handleRpcStream(connection.remotePeer.toString(), stream);
    }, { maxInboundStreams: 8, maxOutboundStreams: 8 });
    await this.node.start();
    pubsub.subscribe(PUBLIC_TOPIC);

    for (const address of this.options.bootstrapPeers ?? []) {
      try {
        await this.connect(address);
      } catch (error) {
        this.#emitSafe("peer-error", { address, code: error.code ?? "DIAL_FAILED" });
      }
    }
    this.identity.multiaddrs = this.node.getMultiaddrs().map((address) => address.toString());
    return this;
  }

  async connect(address, { signal } = {}) {
    let target;
    try {
      target = typeof address === "string" ? multiaddr(address) : address;
      const addressString = target.toString();
      const connected = this.node.getPeers().find((peer) => {
        if (peer.toString() === addressString) return true;
        return this.node.getConnections(peer).some((connection) => connection.remoteAddr?.toString() === addressString);
      });
      if (connected != null) return connected.toString();
      const connection = await this.node.dial(target, { signal });
      return connection.remotePeer.toString();
    } catch (error) {
      throw new PeerTransportError("PEER_UNAVAILABLE", "could not connect to peer address", { cause: error });
    }
  }

  async publish(envelope) {
    assertPublicEnvelope(envelope);
    const bytes = serializeEnvelope(envelope, this.maxMessageBytes);
    const existing = await this.eventStore.get(envelope.id);
    if (existing != null) {
      if (canonicalEnvelopeString(existing) !== canonicalEnvelopeString(envelope)) {
        throw new PeerTransportError("MESSAGE_ID_CONFLICT", "public message ID conflicts with a different signed event");
      }
      throw new PeerTransportError("DUPLICATE", "public message was already published by this peer");
    }
    await validateSignedEnvelope(envelope, {
      chainId: this.chainId,
      identityResolver: this.identityResolver,
      replayStore: this.replayStore,
    });
    try {
      if (!(await this.eventStore.append(envelope))) {
        throw new PeerTransportError("DUPLICATE", "public message was already published by this peer");
      }
    } catch (error) {
      if (error.code === "MESSAGE_ID_CONFLICT") {
        throw new PeerTransportError(error.code, error.message, { cause: error });
      }
      throw error;
    }
    this.#emitSafe("public-message", { envelope, peerId: this.localPeerId, source: "local" });
    let result;
    try {
      result = await this.node.services.pubsub.publish(PUBLIC_TOPIC, bytes);
    } catch (error) {
      this.#emitSafe("publish-error", { messageId: envelope.id, code: error.code ?? "PUBLISH_FAILED" });
      return { messageId: envelope.id, delivery: "QUEUED", recipients: [], errorCode: error.code ?? "PUBLISH_FAILED" };
    }
    return {
      messageId: envelope.id,
      delivery: result.recipients.length === 0 ? "QUEUED" : "GOSSIPED",
      recipients: result.recipients.map((peer) => peer.toString()),
    };
  }

  async request(peerId, envelope, { signal, timeoutMs = this.requestTimeoutMs } = {}) {
    assertPrivateEnvelope(envelope);
    serializeEnvelope(envelope, this.maxMessageBytes);
    await validateSignedEnvelope(envelope, {
      chainId: this.chainId,
      identityResolver: this.identityResolver,
    });
    const target = peerString(peerId);
    const localPrincipalId = await this.peerPrincipalResolver(this.localPeerId);
    const targetPrincipalId = await this.peerPrincipalResolver(target);
    if (localPrincipalId !== envelope.principalId || targetPrincipalId == null) {
      throw new PeerTransportError("UNAUTHORIZED", "direct task messages must bind the signed sender and recipient to their libp2p peers");
    }
    const requestHash = Buffer.from(serializeEnvelope(envelope, this.maxMessageBytes)).toString("base64");
    const cached = this.outboundResponseCache.get(envelope.id);
    if (cached != null) {
      if (cached.peerId !== target || cached.requestHash !== requestHash) {
        throw new PeerTransportError("MESSAGE_ID_CONFLICT", "message ID was reused for different request content");
      }
      return structuredClone(cached.response);
    }
    if (!(await this.authorizePrivateRecipient({ peerId: target, principalId: targetPrincipalId, envelope }))) {
      throw new PeerTransportError("UNAUTHORIZED", "recipient is not authorized for private task messages");
    }
    const response = await this.#rpc(target, { op: "MESSAGE", envelope }, { signal, timeoutMs });
    if (!response?.envelope) {
      throw new PeerTransportError(response?.error?.code ?? "INVALID_RESPONSE", response?.error?.message ?? "peer returned no signed response");
    }
    if (await this.peerPrincipalResolver(target) !== response.envelope.principalId) {
      throw new PeerTransportError("UNAUTHORIZED", "response signer is not bound to the connected libp2p peer");
    }
    await validateSignedEnvelope(response.envelope, {
      chainId: this.chainId,
      identityResolver: this.identityResolver,
      replayStore: this.replayStore,
    });
    this.outboundResponseCache.set(envelope.id, { peerId: target, requestHash, response: structuredClone(response.envelope) });
    while (this.outboundResponseCache.size > MAX_RESPONSE_CACHE) {
      this.outboundResponseCache.delete(this.outboundResponseCache.keys().next().value);
    }
    return response.envelope;
  }

  async onPublicMessages(handler) {
    this.on("public-message", handler);
    return () => this.off("public-message", handler);
  }

  async onPeerChanges(handler) {
    this.on("peer-connected", handler);
    this.on("peer-disconnected", handler);
    return () => {
      this.off("peer-connected", handler);
      this.off("peer-disconnected", handler);
    };
  }

  async getPublicEvents() {
    return this.eventStore.snapshot();
  }

  async syncPublicEvents(peerId) {
    const target = peerString(peerId);
    if (!this.node.getPeers().some((peer) => peer.toString() === target)) {
      throw new PeerTransportError("PEER_UNAVAILABLE", `peer ${target} is not connected`);
    }
    return this.#syncPeer(target);
  }

  async getPeers() {
    return this.node.getPeers().map((peer) => peer.toString()).sort();
  }

  async getPeerScore(peerId) {
    return this.peerScores.get(peerString(peerId));
  }

  async getPeerScores() {
    return this.peerScores.snapshot();
  }

  async close() {
    await this.node.stop();
    this.removeAllListeners();
  }

  async #ingestPublic(data, peerId) {
    try {
      const envelope = parseCanonicalEnvelope(data, this.maxMessageBytes);
      assertPublicEnvelope(envelope);
      const existing = await this.eventStore.get(envelope.id);
      if (existing != null) {
        await validateSignedEnvelope(envelope, {
          chainId: this.chainId,
          identityResolver: this.identityResolver,
        });
        if (canonicalEnvelopeString(existing) === canonicalEnvelopeString(envelope)) return false;
        throw new PeerTransportError("MESSAGE_ID_CONFLICT", "public message ID conflicts with a different signed event");
      }
      await validateSignedEnvelope(envelope, {
        chainId: this.chainId,
        identityResolver: this.identityResolver,
        replayStore: this.replayStore,
      });
      await this.eventStore.append(envelope);
      this.#emitSafe("public-message", { envelope, peerId, source: "gossipsub" });
      return true;
    } catch (error) {
      if (!["REPLAY", "EXPIRED"].includes(error.code)) await this.#penalize(peerId, error.code ?? "INVALID_MESSAGE", -5);
      return false;
    }
  }

  async #dialDiscovered(addresses, expectedPeerId) {
    if (expectedPeerId != null && this.node.getPeers().some((peer) => peer.toString() === expectedPeerId)) return;
    for (const address of addresses) {
      const addressText = address.toString();
      if (this.dialingAddresses.has(addressText)) continue;
      this.dialingAddresses.add(addressText);
      try {
        const embeddedPeerId = address.getPeerId?.();
        if (embeddedPeerId != null && this.node.getPeers().some((peer) => peer.toString() === embeddedPeerId)) continue;
        await this.node.dial(address);
      } catch (error) {
        if (error.code !== "ERR_CONNECTION" && error.name !== "InvalidMultiaddrError") {
          this.#emitSafe("peer-error", { address: addressText, code: error.code ?? "DIAL_FAILED" });
        }
      } finally {
        this.dialingAddresses.delete(addressText);
      }
    }
  }

  async #handleRpcStream(peerId, stream) {
    this.#emitSafe("rpc-inbound", { peerId, status: stream.status, protocol: stream.protocol });
    try {
      await writeFrame(stream, { ready: true }, this.maxFrameBytes);
      const frame = await readFrame(stream, this.maxFrameBytes);
      const bodyBytes = Buffer.byteLength(JSON.stringify(frame), "utf8");
      if (!this.rateLimiter.consume(peerId, bodyBytes)) {
        await this.#penalize(peerId, "RATE_LIMIT", -5);
        await writeFrame(stream, { error: { code: "RATE_LIMIT", message: "peer message rate exceeded" } }, this.maxFrameBytes);
        await stream.close();
        return;
      }
      let result;
      if (frame?.op === "SYNC_PAGE") {
        if (typeof frame.after !== "string" || frame.after.length > 512) {
          throw new PeerTransportError("INVALID_SYNC_CURSOR", "invalid public event cursor");
        }
        const page = await this.#publicPage(frame.after);
        result = { ok: true, ...page };
      } else if (frame?.op === "SYNC_PUT") {
        if (!Array.isArray(frame.events) || frame.events.length > this.syncPageEvents) {
          throw new PeerTransportError("INVALID_SYNC_PAGE", "invalid event sync page");
        }
        let accepted = 0;
        for (const envelope of frame.events) {
          const bytes = serializeEnvelope(envelope, this.maxMessageBytes);
          accepted += Number(await this.#ingestPublic(bytes, peerId));
        }
        result = { ok: true, accepted };
      } else if (frame?.op === "MESSAGE") {
        result = { ok: true, envelope: await this.#handlePrivateMessage(peerId, frame.envelope) };
      } else {
        throw new PeerTransportError("UNKNOWN_OPERATION", "unsupported peer RPC operation");
      }
      await writeFrame(stream, result, this.maxFrameBytes);
      await stream.close();
    } catch (error) {
      this.#emitSafe("rpc-error", { peerId, code: error.code ?? "RPC_FAILED", message: String(error.message ?? "").slice(0, 256) });
      const response = { error: structuredError(error) };
      try {
        await writeFrame(stream, response, this.maxFrameBytes);
        await stream.close();
      } catch {
        stream.abort?.(error);
      }
      if (!["DUPLICATE", "REPLAY", "EXPIRED"].includes(error.code)) {
        await this.#penalize(peerId, error.code ?? "RPC_FAILED", -5);
      }
    }
  }

  async #handlePrivateMessage(peerId, envelope) {
    assertPrivateEnvelope(envelope);
    const bytes = serializeEnvelope(envelope, this.maxMessageBytes);
    const requestBodyHash = Buffer.from(bytes).toString("base64");
    const expectedPrincipal = await this.peerPrincipalResolver(peerId);
    if (expectedPrincipal !== envelope.principalId) {
      throw new PeerTransportError("UNAUTHORIZED", "libp2p peer identity is not bound to the signed AMN principal");
    }
    await validateSignedEnvelope(envelope, {
      chainId: this.chainId,
      identityResolver: this.identityResolver,
    });
    if (!(await this.authorizePrivateSender({ peerId, principalId: envelope.principalId, envelope }))) {
      throw new PeerTransportError("UNAUTHORIZED", "sender is not authorized for this private task message");
    }
    const cached = this.responseCache.get(envelope.id);
    if (cached != null) {
      if (cached.peerId !== peerId || cached.requestBodyHash !== requestBodyHash) {
        throw new PeerTransportError("MESSAGE_ID_CONFLICT", "message ID was reused with a different signed body or sender");
      }
      return cached.response;
    }
    await validateSignedEnvelope(envelope, {
      chainId: this.chainId,
      identityResolver: this.identityResolver,
      replayStore: this.replayStore,
    });
    if (typeof this.onRequestHandler !== "function") {
      throw new PeerTransportError("UNHANDLED", "peer has no task request handler");
    }
    const response = await this.onRequestHandler({ peerId, principalId: envelope.principalId, envelope });
    if (response == null) throw new PeerTransportError("EMPTY_RESPONSE", "task request handler returned no signed response");
    serializeEnvelope(response, this.maxMessageBytes);
    if (response.principalId !== await this.peerPrincipalResolver(this.localPeerId)) {
      throw new PeerTransportError("UNAUTHORIZED", "direct task response must be signed by the principal bound to this libp2p peer");
    }
    await validateSignedEnvelope(response, {
      chainId: this.chainId,
      identityResolver: this.identityResolver,
    });
    this.responseCache.set(envelope.id, { peerId, requestBodyHash, response });
    while (this.responseCache.size > MAX_RESPONSE_CACHE) {
      this.responseCache.delete(this.responseCache.keys().next().value);
    }
    return response;
  }

  async #rpc(peerId, frame, { signal, timeoutMs } = {}) {
    const peer = this.node.getPeers().find((candidate) => candidate.toString() === peerId);
    if (peer == null) throw new PeerTransportError("PEER_UNAVAILABLE", `peer ${peerId} is not connected`);
    const timeout = AbortSignal.timeout(timeoutMs ?? this.requestTimeoutMs);
    const combinedSignal = signal == null ? timeout : AbortSignal.any([signal, timeout]);
    const stream = await this.node.dialProtocol(peer, RPC_PROTOCOL, { signal: combinedSignal });
    this.#emitSafe("rpc-open", { peerId, status: stream.status, protocol: stream.protocol, readStatus: stream.readStatus, writeStatus: stream.writeStatus });
    try {
      const ready = await readFrame(stream, this.maxFrameBytes);
      if (ready?.ready !== true) throw new PeerTransportError("INVALID_HANDSHAKE", "peer RPC readiness handshake failed");
      await writeFrame(stream, frame, this.maxFrameBytes);
      const response = await readFrame(stream, this.maxFrameBytes);
      await stream.close();
      if (response?.error) {
        throw new PeerTransportError(response.error.code ?? "REQUEST_FAILED", response.error.message ?? "peer request failed");
      }
      if (response?.ok !== true) throw new PeerTransportError("INVALID_RESPONSE", "peer response has no success marker");
      return response;
    } catch (error) {
      stream.abort?.(error);
      if (error.name === "TimeoutError" || error.name === "AbortError") {
        throw new PeerTransportError("REQUEST_TIMEOUT", "peer request timed out", { cause: error });
      }
      throw error;
    }
  }

  async #syncPeer(peerId) {
    if (peerId === this.localPeerId) return true;
    const existingJob = this.syncJobs.get(peerId);
    if (existingJob != null) return existingJob;
    const job = (async () => {
      try {
        const localEvents = await this.#validatedSnapshot();
        await this.#syncPush(peerId, localEvents);
        let cursor = "";
        for (;;) {
          const page = await this.#rpc(peerId, {
            op: "SYNC_PAGE",
            after: cursor,
            limit: this.syncPageEvents,
          });
          if (!Array.isArray(page.events) || typeof page.nextKey !== "string" || typeof page.done !== "boolean") {
            throw new PeerTransportError("INVALID_SYNC_PAGE", "peer returned an invalid sync page");
          }
          for (const envelope of page.events) {
            const bytes = serializeEnvelope(envelope, this.maxMessageBytes);
            await this.#ingestPublic(bytes, peerId);
          }
          if (page.done) break;
          if (page.nextKey <= cursor) throw new PeerTransportError("INVALID_SYNC_CURSOR", "sync cursor failed to advance");
          cursor = page.nextKey;
        }
        this.#emitSafe("peer-sync", { peerId, eventCount: (await this.eventStore.snapshot()).length });
        return true;
      } catch (error) {
        this.#emitSafe("peer-error", { peerId, code: error.code ?? "SYNC_FAILED" });
        return false;
      }
    })();
    this.syncJobs.set(peerId, job);
    try {
      return await job;
    } finally {
      if (this.syncJobs.get(peerId) === job) this.syncJobs.delete(peerId);
    }
  }

  async #validatedSnapshot() {
    const envelopes = await this.eventStore.snapshot();
    const active = [];
    for (const envelope of envelopes) {
      if (Date.parse(envelope.expiresAt) <= Date.now()) continue;
      assertPublicEnvelope(envelope);
      try {
        await validateSignedEnvelope(envelope, {
          chainId: this.chainId,
          identityResolver: this.identityResolver,
        });
        active.push(envelope);
      } catch (error) {
        if (error.code !== "REVOKED_SIGNER" && error.code !== "IDENTITY_MISMATCH") throw error;
      }
    }
    return active;
  }

  async #publicPage(afterKey) {
    if (typeof afterKey !== "string" || afterKey.length > 512) {
      throw new PeerTransportError("INVALID_SYNC_CURSOR", "invalid public event cursor");
    }
    const remaining = (await this.#validatedSnapshot()).filter((envelope) => publicEventKey(envelope) > afterKey);
    const events = [];
    let used = 512;
    for (const envelope of remaining.slice(0, this.syncPageEvents)) {
      const size = serializeEnvelope(envelope, this.maxMessageBytes).byteLength + 1;
      if (used + size > this.maxFrameBytes - 1024) {
        if (events.length === 0) throw new PeerTransportError("EVENT_TOO_LARGE", "public event cannot fit in a sync page");
        break;
      }
      events.push(envelope);
      used += size;
    }
    const nextKey = events.length === 0 ? afterKey : publicEventKey(events.at(-1));
    const lastKey = remaining.length === 0 ? afterKey : publicEventKey(remaining.at(-1));
    return { events, nextKey, done: nextKey === lastKey };
  }

  async #syncPush(peerId, envelopes) {
    let batch = [];
    for (const envelope of envelopes) {
      const candidate = [...batch, envelope];
      if (candidate.length > this.syncPageEvents) {
        await this.#rpc(peerId, { op: "SYNC_PUT", events: batch });
        batch = [envelope];
        continue;
      }
      try {
        encodeFrame({ op: "SYNC_PUT", events: candidate }, this.maxFrameBytes);
        batch = candidate;
      } catch (error) {
        if (batch.length === 0) throw error;
        await this.#rpc(peerId, { op: "SYNC_PUT", events: batch });
        batch = [envelope];
        encodeFrame({ op: "SYNC_PUT", events: batch }, this.maxFrameBytes);
      }
    }
    if (batch.length > 0) await this.#rpc(peerId, { op: "SYNC_PUT", events: batch });
  }

  async #penalize(peerId, reason, delta) {
    if (peerId === "unknown" || peerId === this.localPeerId) return;
    const score = await this.peerScores.record(peerId, delta, reason);
    this.#emitSafe("peer-score", { peerId, ...score, reason });
    if (score.score <= this.scoreBanThreshold) {
      const peer = this.node.getPeers().find((candidate) => candidate.toString() === peerId);
      if (peer != null) await this.node.hangUp(peer).catch(() => {});
    }
  }

  #emitSafe(name, value) {
    for (const listener of this.listeners(name)) {
      try {
        listener(value);
      } catch {
        // Application listeners cannot stop peer protocol handling.
      }
    }
  }
}

export async function createLibp2pPeerTransport(options = {}) {
  return Libp2pPeerTransport.create(options);
}

export function validateNetworkMessage(envelope, maxBytes = MAX_PUBLIC_MESSAGE_BYTES) {
  serializeEnvelope(envelope, maxBytes);
  return envelope;
}

export { PUBLIC_MESSAGE_KINDS, PUBLIC_TOPIC, RPC_PROTOCOL, PeerTransportError };
