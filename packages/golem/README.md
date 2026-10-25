# AMN Golem Yagna compute adapter

This package implements the `ComputeBackend` seam for a Golem Yagna requestor
using `@golem-sdk/golem-js` 3.11.0. It does not run a Yagna daemon, build a
Golem VM image, create a provider account, or silently replace a failed Golem
request with local execution.

## What is implemented

- Scans Yagna offers for the signed runtime, version, CPU and memory needs.
- Produces a canonical content hash for every offer snapshot, including the
  advertised provider, runtime, resources, payment platform and GLM rates.
- Rejects stale, changed, missing, incompatible or over-cap selected offers.
- Creates a Yagna allocation at the customer-authorized native GLM cap.
- Dispatches a fixed AMN worker executable inside the customer-pinned Golem VM
  image. Customer task data is uploaded directly to that VM and is not written
  to public IPFS or OrbitDB by this adapter.
- Verifies the signed AMN execution contract, identity binding, replay state,
  input-manifest hash, runtime/image, resource requirements and deadline before
  dispatch.
- Maps agreement/execution lifecycle, timeout, cancellation, provider errors,
  result retrieval and Yagna-reported cost into structured backend states.
- Provides `FileJobStore` and reuses the protocol package's `FileReplayStore`
  for node-local durable job and replay records.
- Provides `FakeComputeBackend` for deterministic offline success, failure,
  timeout and cancellation paths.

## Supported host and setup

The official Yagna requestor setup supports Linux. The SDK package metadata
allows Node.js 18+, while AMN recommends Node.js 20 or newer. The adapter checks
for Linux x86-64 and fails closed on other platforms. Providers have separate
requirements including Linux x86-64 and nested virtualization; a requestor host
check does not attest provider hardware. See the
[official requestor setup](https://docs.golem.network/docs/creators/tools/yagna/yagna-installation-for-requestors),
[provider requirements](https://docs.golem.network/docs/golem/overview/provider),
and [Golem-JS reference](https://docs.golem.network/docs/golem-js/reference/README).

On a supported Linux requestor, install and start Yagna using the official
instructions, create an app key, and configure an existing Golem payment
account. Then set:

```sh
export YAGNA_API_URL=http://127.0.0.1:7465
export YAGNA_APP_KEY='your-local-yagna-app-key'
export YAGNA_PAYMENT_NETWORK=hoodi
```

The test network default uses `tglm`; mainnet GLM requires explicit network,
token and payment-platform configuration. The adapter does not ask for or
persist a wallet private key: Yagna owns the payment account. Golem allocations
cap provider spend in Golem's native token. They do not fund or settle AMN's
separate customer quote.

## VM image contract

The signed binding must include a SHA3-224 GVMI digest, exact runtime name and
version, resource requirements, task deadline, input manifest hash, selected
offer/provider snapshot and a positive `nativeCostCapGlm` decimal string. The
image must contain the trusted executable configured as
`workerExecutable` (default `/opt/amn/bin/amn-golem-worker`) and accept the
configured fixed arguments. The input manifest contains one `payloadHash`
computed over the canonical JSON private payload; encode fractional values as
decimal strings because protocol JSON accepts safe integers only. AMN does not
build or publish a VM image in P7.
Do not place customer-controlled shell commands in the worker arguments.

AMN represents this image as `sha3-224:<56 lowercase hex characters>` in the
signed binding, then removes the prefix when passing the raw digest to the
Golem-JS VM API. Golem documents SHA3-224 image IDs as 56 hexadecimal
characters. Signed CPU, RAM, and storage values are minimum offer requirements,
not hard execution limits. The current adapter rejects nonzero VRAM and
outbound network access until the selected runtime and image contract can
enforce those limits explicitly.

GPU tasks must explicitly request `vm-nvidia` with the documented experimental
GPU capability. This is not a claim that any consumer GPU is eligible. The
official [Golem AI/GPU tutorial](https://docs.golem.network/docs/creators/javascript/tutorials/AI-on-golem-network)
describes that runtime/capability path.

## Use

```js
import {
  FileJobStore,
  FileReplayStore,
  GolemYagnaBackend,
} from "./src/index.mjs";

const backend = new GolemYagnaBackend({
  config: {
    mode: "production",
    chainId: 31337,
    network: process.env.YAGNA_PAYMENT_NETWORK,
    token: "tglm",
    paymentPlatform: "erc20-hoodi-tglm",
  },
  identityResolver: resolveActiveAmnIdentity,
  replayStore: new FileReplayStore("./state/replay"),
  jobStore: new FileJobStore("./state/golem-jobs"),
});

const ready = await backend.capabilities();
if (!ready.productionReady) throw new Error(ready.reasons.join(" "));
// Quote and sign a v2 ExecutionContract with backendBinding.backend="golem-yagna"
// before calling submit({contractEnvelope, inputManifest, privatePayload}).
```

`identityResolver` must resolve the signer to an active AMN identity with its
current key epoch. Production calls also require durable replay and job stores.
The adapter doesn't fall back to the fake backend if Yagna is missing.

## Limitations and trust

- The live requestor uses the documented Golem-JS API, but no live Yagna
  requestor/provider test has been run from the current Windows development
  host. `pnpm test:golem` tests the adapter with a Yagna-shaped mock and the
  offline fake only.
- A scanned offer is rechecked by provider ID and complete snapshot before
  dispatch. Golem-JS `oneOf` negotiates an agreement through its own market
  proposal lifecycle; it doesn't expose the scan's original offer serial on the
  resulting agreement. AMN additionally restricts the agreement to that
  provider, runtime and rates no greater than the accepted offer/cap. Exact
  proposal-serial continuity remains a live integration limitation.
- Yagna's estimated cost is an estimate. The allocation is the provider-side
  native-token ceiling; AMN waits for Yagna-reported allocation spend and does
  not settle the customer payment from this adapter alone.
- The remote provider can read task plaintext inside the VM. TLS/secure
  transport, artifact hashes and signatures do not provide confidential
  computing or prove that inference took place.
- Node-local durable records do not replicate across requestors. A process
  restart returns `recoveryRequired` for an in-flight agreement; an operator
  must reconcile its Yagna agreement and allocation before AMN settlement.
- `@golem-sdk/golem-js` is LGPL-3.0; AMN remains MIT. Preserve the dependency's
  license and notices when distributing this package and review LGPL
  obligations for the chosen distribution model.

Run the offline walkthrough with `pnpm demo:golem` and the package contract
suite with `pnpm test:golem`.
