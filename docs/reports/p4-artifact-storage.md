# P4 content-addressed artifact storage report

**Status:** local adapter and acceptance suite passed on 2026-10-23. P5 has not
started. This completes the P4 local development gate only; it does not make
Helia or the application workflow production-ready.

## Delivered

- Added `@ai-mining-network/storage` around Helia 7.1.16, UnixFS 8.0.9,
  Bitswap 4.0.20, `blockstore-fs` 4.0.1, `datastore-level` 13.0.1, and the
  libp2p TCP/Noise/Yamux modules. Persistent block data and the Helia identity
  survive restart; peer cache and DHT routing state are rebuildable/in-memory.
- Added a direct connected-peer content router for local and explicitly
  connected peers. DHT service is configured only when bootstrap peer
  addresses are supplied. There is no HTTP gateway fallback.
- Public storage requires caller authorization and accepts only
  `skill-package`, `benchmark`, or `public-receipt` kinds. Artifacts are pinned
  locally and may have a retention deadline. Announcement is bounded; without
  configured routing the adapter reports `failed; pinned locally` rather than
  claiming network availability.
- Private storage encrypts before adding bytes to Helia: AES-256-GCM payloads
  and one X25519/HKDF-SHA256/AES-GCM-wrapped data key per authorized recipient.
  Grants bind task, CID, recipient, grant ID, and expiry. Local grant revocation
  survives restart.
- Retrieval requires caller-supplied expected CID and SHA-256, imposes a byte
  limit and deadline, and verifies returned data before exposing it. Retention
  cleanup unpins and garbage-collects locally owned expired content.
- Added a two-peer demo that fetches an explicitly public skill and an encrypted
  artifact that only the assigned demo recipient can decrypt. Updated the
  generated dependency license inventory.

## Validation evidence

Commands run from the repository root on the Windows development host:

```powershell
pnpm test:networking
pnpm demo:networking
pnpm test:storage
pnpm demo:storage
```

Results:

- P3 regression: `pnpm test:networking` — **10 passed, 0 failed**.
- P3 local demo: `pnpm demo:networking` — three in-process peers converged on
  the same signed event. This is test-memory transport, not public reachability.
- P4 storage tests: `pnpm test:storage` — **6 passed, 0 failed**. Coverage:
  explicit public authorization and allowed kinds, CID/SHA checks, private
  encryption/grant assignment/expiry/revocation across restart, two persistent
  peers with source and destination restarts, missing-provider failure without
  gateway fallback, retention garbage collection, and modified-block rejection.
- P4 local demo: `pnpm demo:storage` — public content fetched by the second
  TCP peer and assigned validator fetched/decrypted the private artifact. The
  report correctly said `failed; pinned locally` for global announcement
  because no bootstrap peers were configured.
- Full regression: `pnpm test:all` — **4 web, 6 protocol, 10 networking, 6
  storage, 4 Hardhat contract, and 69 Python tests passed**; the strict protocol
  TypeScript declaration check also passed.
- Privacy checks search the stored block files and grant response for markers
  representing private prompt, source document, ground truth, and plaintext
  output; no marker is present in plaintext.
- `scripts/license_report.py` regenerated the inventory: 58 Python distributions
  and 718 resolved Node package versions. Helia dependencies report
  `Apache-2.0 OR MIT`; review dependency terms before redistribution.

## Integration boundary and limits

This adapter is real Helia/libp2p code exercised on two loopback TCP peers. The
P4 package is not wired into the task API, local skill registry, miner runtime,
or validator runtime. No DHT bootstrap, public multi-operator retrieval,
pinning service, OrbitDB, or global content-availability mechanism was tested.
The two-peer demo explicitly connects known addresses and is local only.

A CID and digest prove byte identity relative to an expected value, not
authorship, inference execution, or semantic correctness. A local pin protects
data from that node's garbage collector; it does not guarantee another node
holds a copy. Private artifact CIDs and sizes may leak metadata. A peer that
learns a private CID can retrieve ciphertext. Grant revocation is local and
cannot erase known plaintext or keys, nor invalidate a grant at an offline peer.
Recipient identity binding and grant exchange still need to be integrated with
the signed AMN message protocol. Local filesystem permissions, disk backups,
key custody, and disk-capacity monitoring are operator responsibilities.

See [`artifact-storage.md`](../architecture/artifact-storage.md), the
[integration matrix](../architecture/integration-matrix.md), and the
[threat model](../security/threat-model.md).
