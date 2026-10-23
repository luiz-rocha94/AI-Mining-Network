# P3 peer networking implementation report

**Status:** implementation and focused automated validation passed on
2026-10-23. Maintainer validation remains pending. P4 has not started.

## Delivered

- Added `@ai-mining-network/networking`, a provider-neutral `PeerTransport`
  interface and a libp2p adapter using TCP, Noise, Yamux, identify, strict-signed
  GossipSub, and LAN mDNS. Operators can configure static bootstrap peers.
- Public v2 protocol envelopes are schema checked, EIP-712 verified against an
  active identity resolver, replay checked, size/rate limited, and persisted in
  a deterministic local event log. Bounded anti-entropy sync repairs missed
  public events when peers reconnect.
- `CustomerTask` and unknown/private kinds use authorized direct RPC only. RPC
  uses Noise-encrypted connections, bounded length-prefixed frames, peer-to-
  principal binding, request/response authorization, and signed envelopes.
- Added a test-only local transport, local three-peer convergence demo, TCP /
  Noise loopback tests, and documentation for production assumptions.
- Made protocol replay claims use exclusive file creation, preserving
  restart-safe claims on Windows where hard links are unavailable.

## Reproduction and evidence

From the repository root, after `pnpm install`:

```powershell
pnpm test:networking
pnpm demo:networking
```

Focused test result: **10 passed, 0 failed**. Coverage includes production
configuration rejection of test substitutes; signed public records and
reconnect convergence; static bootstrap; real TCP/Noise loopback; private
encrypted unicast and identity authorization; impersonation rejection; event
duplicate/conflict handling; invalid limits and missing storage; bounded rate
windows; and truncated/oversized RPC frames.

The local demo completed with three in-process memory peers and showed the same
signed public event ID in each peer's event snapshot. The demo's memory
transport is deliberately test-only; it does not prove internet reachability.

Full repository result: **passed** with `pnpm test:all` (4 web tests, 6
protocol tests, TypeScript protocol declaration check, 10 networking tests,
4 Hardhat contract tests, and 69 Python tests). The command requires the
project virtual environment on `PATH` so `python -m pytest` uses the installed
development dependencies.

## Integration boundary

**Real library integration:** the adapter instantiates maintained libp2p
modules for TCP, Noise, Yamux, identify, GossipSub, and mDNS; it is exercised by
loopback TCP tests. `@libp2p/memory` is development-only.

**Application boundary:** this package is not yet connected to the customer's
workflow API, miner job scheduler, or validator runtime. It does not transfer
large task artifacts. Those remain P4+ work; private messages currently carry
bounded protocol envelopes only.

**Not included:** DHT/rendezvous, relays, NAT traversal, a globally replicated
consensus log, cross-peer identity governance, or a network-wide Sybil
resistance mechanism. mDNS discovers peers on a LAN. A remote network needs
reachable peer addresses or an operator-provided discovery module. Bootstrap
addresses seed discovery and are not an always-on coordinator.

## Security limits

Noise authenticates a libp2p PeerId and encrypts bytes in transit. Strict
GossipSub signatures authenticate the included PeerId key. AMN EIP-712
verification binds a message to the configured signer/domain, and application
resolvers bind signer keys and PeerIds to principals. These mechanisms do not
prove personhood, independent identities, honest compute, or AI correctness.
Local peer scores are abuse-control heuristics, not shared reputation or
settlement evidence. Private-message confidentiality ends at the authorized
endpoints; task envelopes are not a large-artifact encryption/storage design.

Windows testing exposed filesystem rename restrictions. Event and score
snapshots stage writes and fall back to copy when rename is denied; a hard
process termination during that fallback can leave a partial snapshot. Replay
claims instead use exclusive file creation. Protect local state with operating
system permissions and backups.

## Dependencies and license inventory

The adapter's resolved Node dependency closure is covered by
[`dependency-licenses.md`](dependency-licenses.md). Direct libp2p packages,
Ajv, and the development-only memory transport are now included in the license
report generator. Review unknown/custom license metadata before redistribution.

## P3 acceptance checklist

- [x] Authenticated encrypted libp2p transport, public signed messaging,
  optional discovery, and direct RPC.
- [x] Public/private message policy and encrypted authorized private delivery.
- [x] Replay protection, deterministic event ordering, anti-entropy, and
  bounded resource/rate handling.
- [x] Focused TCP/Noise and multi-peer test coverage; local convergence demo.
- [x] Architecture, integration boundary, and security assumptions documented.
- [ ] Maintainer reviews this report and the demo, then accepts P3 before P4.
