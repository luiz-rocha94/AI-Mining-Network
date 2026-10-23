# AMN peer networking

This package provides the coordinator-free peer transport for signed AMN v2
protocol envelopes. The production adapter uses maintained JavaScript libp2p
components: TCP, Noise encryption, Yamux stream multiplexing, identify,
GossipSub, and mDNS discovery. `bootstrapPeers` accepts explicit peer
multiaddresses for networks where LAN mDNS is unavailable. An initial peer
address or an enabled discovery mechanism is still needed to join a remote
network; this package does not require a central task coordinator.

Peers that share this node with OrbitDB may opt into
`allowOrbitDbTopics: true`. OrbitDB topic IDs are derived after its manifest is
created, so the node cannot enumerate those topics up front. This disables the
GossipSub topic allowlist for that peer; strict pubsub signatures and the AMN
application handler's own topic filter remain active. Use the option only when
embedding the node in a public OrbitDB registry, and apply the registry's AMN
record validation separately.

## Run the local peer demo

From the repository root:

```sh
pnpm install
pnpm demo:networking
pnpm test:networking
```

The demo uses libp2p's in-process memory transport. It exercises signed message
validation and public-event convergence, but it does not demonstrate TCP reachability,
NAT traversal, or internet-scale discovery. The automated suite separately tests
Noise-encrypted TCP peers on loopback.

## Message policy

| Message | Delivery | Stored/replicated |
|---|---|---|
| Skill manifests, benchmark receipts, miner/validator capabilities and offers, fixed quotes, execution contracts/commitments, assignments, verification receipts/decisions, settlements, ratings, identity transitions | Signed GossipSub `amn/protocol/v2/public` | Durable local public-event log; anti-entropy copies signed, active, unexpired records on peer connection |
| `CustomerTask` and any unrecognized/private protocol payload | Signed direct RPC only | Not placed on GossipSub or in the public event log |

Public records are canonical JSON and EIP-712 validated against an active
identity resolver. GossipSub also uses libp2p strict message signatures. Direct
RPC streams require Noise-authenticated libp2p connections, bind the remote
PeerId to an AMN principal, check the signed sender, and call sender/recipient
authorization policies. The RPC performs a readiness handshake, uses
length-prefixed bounded JSON frames, and supports signed request/response.
Task content sent through a direct stream is encrypted in transit by Noise;
operators remain responsible for authorizing the task recipient and for any
artifact references included in the message.

The replay store persists message IDs and signer nonces locally using exclusive
file creation. Public event and peer-score snapshots persist under the required
`storageDirectory`. IDs are bounded, event ordering is timestamp/signer/ID
stable, and peer message/byte windows, frame sizes, public message sizes,
GossipSub decode limits, connection counts, and local score history are bounded.
The score is a local abuse-control heuristic; it is not shared reputation.

## Important limits

- The local memory adapter has no encryption, identity validation, persistence,
  or network discovery. `deployment: "production"` and
  `productionRequired: true` reject it.
- mDNS discovers peers on a local network only. Static bootstrap addresses are
  optional seeds, not a coordinator; no DHT, rendezvous, relay, NAT traversal,
  or durable reconnect service is included yet.
- GossipSub is best-effort delivery. The direct encrypted anti-entropy protocol
  reconciles public event snapshots when a connection is established. It is not
  a global consensus log and has no network-wide conflict-resolution authority.
- Local identity resolvers and peer-to-principal maps must be supplied by the
  application. This package cannot prove that identities are independent or
  prevent an attacker from creating many valid keys.
- Noise authenticates libp2p PeerIds and encrypts a connection. EIP-712 binds an
  AMN message to a signer key. Neither proves that a named person controls the
  key or that a miner ran the claimed AI computation.
- The Windows event/score snapshot writer uses a staged file and falls back to
  replace-capable copy if the filesystem denies rename. Replay claims use
  exclusive file creation. Protect storage directories with OS permissions;
  local disk loss and same-user tampering are outside the network guarantee.

See [`docs/architecture/peer-networking.md`](../../docs/architecture/peer-networking.md)
for the protocol flow, deployment notes, security assumptions, and adapter
compatibility details.
