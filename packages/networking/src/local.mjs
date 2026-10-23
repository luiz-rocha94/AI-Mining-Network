import { randomUUID } from "node:crypto";
import { EventEmitter } from "node:events";
import { MAX_PUBLIC_MESSAGE_BYTES, assertPrivateEnvelope, assertPublicEnvelope, serializeEnvelope, PeerTransportError } from "./protocol.mjs";

export class LocalPeerNetwork {
  peers = new Map();

  createPeer(options = {}) {
    if (options.deployment === "production" || options.productionRequired === true) {
      throw new PeerTransportError("PRODUCTION_TRANSPORT_REQUIRED", "local peer transport is forbidden when production peer networking is required");
    }
    return new LocalPeerTransport(this, options);
  }

  register(peer) {
    if (this.peers.has(peer.localPeerId)) throw new PeerTransportError("DUPLICATE_PEER", "local test peer ID already exists");
    this.peers.set(peer.localPeerId, peer);
    for (const other of this.peers.values()) {
      if (other === peer) continue;
      peer.emit("peer-connected", { peerId: other.localPeerId });
      other.emit("peer-connected", { peerId: peer.localPeerId });
    }
  }

  unregister(peer) {
    this.peers.delete(peer.localPeerId);
    for (const other of this.peers.values()) other.emit("peer-disconnected", { peerId: peer.localPeerId });
  }

  broadcast(sender, envelope) {
    for (const peer of this.peers.values()) {
      if (peer !== sender) peer.emit("public-message", { envelope: structuredClone(envelope), peerId: sender.localPeerId, source: "local-test" });
    }
  }
}

export class LocalPeerTransport extends EventEmitter {
  constructor(network, { peerId = `local-${randomUUID()}`, deployment = "development", productionRequired = false, onRequest } = {}) {
    super();
    if (deployment === "production" || productionRequired) {
      throw new PeerTransportError("PRODUCTION_TRANSPORT_REQUIRED", "local peer transport is forbidden when production peer networking is required");
    }
    this.network = network;
    this.mode = "local-test";
    this.localPeerId = peerId;
    this.onRequestHandler = onRequest;
  }

  async start() {
    this.network.register(this);
    return this;
  }

  async publish(envelope) {
    assertPublicEnvelope(envelope);
    serializeEnvelope(envelope, MAX_PUBLIC_MESSAGE_BYTES);
    this.network.broadcast(this, envelope);
    return { messageId: envelope.id, delivery: "LOCAL_TEST", recipients: [...this.network.peers.keys()].filter((peerId) => peerId !== this.localPeerId) };
  }

  async request(peerId, envelope) {
    assertPrivateEnvelope(envelope);
    serializeEnvelope(envelope, MAX_PUBLIC_MESSAGE_BYTES);
    const peer = this.network.peers.get(peerId);
    if (peer == null) throw new PeerTransportError("PEER_UNAVAILABLE", "local test peer is not registered");
    if (typeof peer.onRequestHandler !== "function") throw new PeerTransportError("UNHANDLED", "local test peer has no request handler");
    return peer.onRequestHandler({ peerId: this.localPeerId, principalId: envelope.principalId, envelope: structuredClone(envelope) });
  }

  async getPublicEvents() {
    return [];
  }

  async getPeers() {
    return [...this.network.peers.keys()].filter((peerId) => peerId !== this.localPeerId).sort();
  }

  async close() {
    this.network.unregister(this);
    this.removeAllListeners();
  }
}
