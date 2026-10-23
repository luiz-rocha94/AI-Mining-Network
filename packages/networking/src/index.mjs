import { LocalPeerNetwork } from "./local.mjs";
import { createLibp2pPeerTransport, PeerTransportError } from "./libp2p-transport.mjs";

export * from "./protocol.mjs";
export * from "./stores.mjs";
export * from "./framing.mjs";
export { LocalPeerNetwork, LocalPeerTransport } from "./local.mjs";
export { Libp2pPeerTransport, createLibp2pPeerTransport, validateNetworkMessage } from "./libp2p-transport.mjs";

export async function createPeerTransport(options = {}) {
  const backend = options.backend ?? "libp2p";
  if (backend === "local") {
    if (options.deployment === "production" || options.productionRequired === true) {
      throw new PeerTransportError("PRODUCTION_TRANSPORT_REQUIRED", "local peer transport is forbidden when production peer networking is required");
    }
    const network = options.network ?? new LocalPeerNetwork();
    const peer = network.createPeer(options);
    return peer.start();
  }
  if (backend === "libp2p") {
    return createLibp2pPeerTransport(options);
  }
  throw new PeerTransportError("UNSUPPORTED_BACKEND", `unsupported peer transport backend: ${backend}`);
}

export function assertProductionPeerTransport({ backend, productionRequired = false, deployment = "development" }) {
  if ((productionRequired || deployment === "production") && backend !== "libp2p") {
    throw new PeerTransportError("PRODUCTION_TRANSPORT_REQUIRED", "production deployment requires the encrypted libp2p transport");
  }
  return true;
}
