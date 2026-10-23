# Artifact storage (P4)

The storage package provides an explicit Helia/IPFS adapter for public skill,
benchmark, and receipt artifacts plus encrypted private task artifacts. The
customer/miner workflow and local Python skill registry still use their local
stores. The P5 OrbitDB public metadata adapter uses this Helia node for
replicated public registry events.

## Local implementation

- Helia uses a filesystem blockstore and Level datastore. A persisted libp2p
  identity survives process restart. The peer store and DHT routing datastore
  are in-memory and rebuildable.
- TCP connections use libp2p Noise and Yamux. Bitswap transfers content blocks;
  Helia recomputes CID identity, and AMN checks the caller's signed expected CID
  and SHA-256 before returning bytes.
- The store can borrow a P3 libp2p node via `libp2pNode` so Helia/Bitswap and
  OrbitDB share the same peer connections. The P3 node owns its lifecycle and
  must be closed after the Helia store. OrbitDB peers opt into dynamically
  derived pubsub topics; the restrictive P3 topic allowlist stays enabled by
  default for other peers.
- A connected-peer content router makes already connected peers available for
  local retrieval. A DHT service is enabled only when bootstrap addresses are
  configured. There is no HTTP gateway or centralized retrieval fallback.
- Public writes require explicit authorization and are limited to
  `skill-package`, `benchmark`, and `public-receipt`. Pin status is local to the
  node. Optional announcements have a bounded timeout and return
  `failed; pinned locally` when configured routing is unavailable.
- Retention expiry unpins AMN-owned artifacts and runs local Helia garbage
  collection. Other peers may retain their own copies.

## Private artifact exchange

Private bytes are encrypted before being passed to Helia. The payload uses
AES-256-GCM with a random data-encryption key. Each assigned recipient receives
an X25519/HKDF-SHA256/AES-GCM envelope that wraps that key and binds the grant
to the task, CID, recipient principal, grant ID, and expiry. A caller-supplied
authorization policy must approve grant issuance, reads, and revocations.
Revoked grant IDs are persisted atomically in the revoking store's local storage
directory so a local restart does not silently clear them.

The grant must be delivered to the assigned party out of band by the caller.
Helia content addressing is not an access-control layer: anyone who learns the
CID may retrieve ciphertext from a reachable pinning peer, but cannot decrypt it
without an authorized recipient's private key and grant. Revocation is enforced
only by the local AMN store that recorded it and only when future reads pass
through that store. It cannot revoke a recipient's already received key/grant,
erase plaintext already read, or invalidate an offline/unaware peer. Grant
state is not yet replicated or backed by a network identity/authorization
service.

## Reproduce

```powershell
pnpm test:storage
pnpm demo:storage
```

The demo starts two local TCP peers, publishes and fetches an explicitly public
skill artifact, then transfers an encrypted private artifact to its assigned
validator, which decrypts it. It uses no DHT bootstrap, gateway, cloud pinning
service, or real funds. The P5 OrbitDB registry uses a separate multi-peer
demo. Neither the P3/P4/P5 adapters nor the customer task workflow use this P4
storage demo automatically.

## Guarantees and limits

CID and SHA-256 checks establish byte integrity relative to the expected values;
they do not prove who authored the data or that a skill produced a correct
answer. Local pinning establishes availability on one node only. DHT provider
records and peers are not persistence guarantees. Local garbage collection does
not delete remote replicas. Transport encryption protects a live libp2p
connection, while private-artifact confidentiality relies on payload encryption
and safe recipient-key custody. File size is capped and reads have a deadline;
operator disk capacity, backups, and key recovery remain operator concerns.

The adapter is tested on the repository's Windows development host with local
loopback peers. Public bootstrap/DHT interoperability, multi-operator discovery,
workflow integration, distributed grant revocation, and production key
management remain unvalidated.

## Upstream references

- [Helia project and modular APIs](https://github.com/ipfs/helia/blob/main/README.md)
- [Helia 6-to-7 upgrade guide](https://github.com/ipfs/helia/wiki/Upgrade-guide-from-Helia-6.0.0-to-Helia-7.0.0/d6b9a3ea85a6993c362922e716f4d5dea7804ca1)
- [IPFS persistence and pinning](https://docs.ipfs.tech/concepts/persistence/)
- [IPFS privacy and encryption](https://docs.ipfs.tech/concepts/privacy-and-encryption/)
