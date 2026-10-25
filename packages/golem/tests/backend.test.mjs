import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { randomBytes as cryptoRandomBytes } from "node:crypto";
import { Wallet } from "ethers";
import { MemoryReplayStore, contentHash, typedData } from "@ai-mining-network/protocol";
import {
  ComputeBackendError,
  FakeComputeBackend,
  FileJobStore,
  GolemYagnaBackend,
  allocationBudgetForCap,
  assessGolemHost,
  createComputeBackend,
  createGolemOfferSnapshot,
} from "../src/index.mjs";

const now = () => new Date();
const hash = (char) => `sha256:${char.repeat(64)}`;

async function signedGolemRequest({ providerId = "provider-1", offer = null, wallet = Wallet.createRandom() } = {}) {
  const privatePayload = { message: "private task input", records: [1, 2] };
  const inputManifest = [{ payloadHash: contentHash(privatePayload), mediaType: "application/json" }];
  const binding = {
    backend: "golem-yagna",
    selectedOfferId: offer?.offerId ?? "offer-1",
    selectedProviderId: providerId,
    offerSnapshotHash: offer?.offerSnapshotHash ?? hash("b"),
    acceptedQuoteTotalAtomic: 900_000,
    executionRewardAtomic: 800_000,
    currency: { chainId: 31337, tokenAddress: "0x0000000000000000000000000000000000000001", decimals: 6, symbol: "TESTUSD" },
    runtimeName: "vm",
    runtimeVersion: "0.6.0",
    imageDigest: `sha3-224:${"c".repeat(56)}`,
    inputHash: contentHash(inputManifest),
    resourceRequirements: { cpuCores: 2, ramBytes: 2_147_483_648, vramBytes: 0, gpuRequired: false, networkAccess: false, timeoutSeconds: 120 },
    deadline: new Date(now().getTime() + 120_000).toISOString(),
    nativeCostCapGlm: "0.01",
  };
  const createdAt = new Date(now().getTime() - 1000).toISOString();
  const envelope = {
    protocolVersion: "2",
    id: `golem-test-${cryptoRandomBytes(8).toString("hex")}`,
    createdAt,
    expiresAt: new Date(now().getTime() + 300_000).toISOString(),
    principalId: `eip155:31337:${wallet.address.toLowerCase()}`,
    keyEpoch: 0,
    signer: wallet.address,
    nonce: `0x${cryptoRandomBytes(32).toString("hex")}`,
    payload: {
      kind: "ExecutionContract",
      contractId: `contract-${cryptoRandomBytes(5).toString("hex")}`,
      taskId: "task-p7",
      quoteId: "quote-p7",
      executorPeerId: wallet.address,
      acceptedAt: createdAt,
      commitDeadline: binding.deadline,
      challengeRound: 1,
      idempotencyKey: `task-p7:${cryptoRandomBytes(6).toString("hex")}`,
      backendBinding: binding,
    },
    payloadHash: contentHash({
      kind: "ExecutionContract",
      contractId: "replaced",
    }),
    signature: `0x${"00".repeat(65)}`,
  };
  envelope.payloadHash = contentHash(envelope.payload);
  const data = typedData(envelope, 31337);
  envelope.signature = await wallet.signTypedData(data.domain, data.types, data.value);
  return {
    contractEnvelope: envelope,
    inputManifest,
    privatePayload,
    identityResolver: async () => ({ status: "ACTIVE", signer: wallet.address, keyEpoch: 0 }),
  };
}

function fakeOffer(overrides = {}) {
  const createdAt = new Date().toISOString();
  return {
    offerId: "offer-1",
    offerCreateAt: createdAt,
    provider: { id: "provider-1", name: "Golem test provider" },
    runtimeName: "vm",
    runtimeVersion: "0.6.0",
    cpuCores: 4,
    cpuThreads: 8,
    memoryGib: 8,
    storageGib: 80,
    runtimeCapabilities: [],
    cpuCapabilities: ["x86_64"],
    gpuBrand: undefined,
    pricing: { start: 0.0001, cpuSec: 0.0000001, envSec: 0.0000001 },
    paymentPlatformAddresses: { "erc20-hoodi-tglm": "0x0000000000000000000000000000000000000002" },
    getEstimatedCost: () => 0.001,
    ...overrides,
  };
}

async function waitForState(backend, contractId, expected, timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const state = await backend.status(contractId);
    if (expected.includes(state.state)) return state;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.fail(`job did not reach ${expected.join(" or ")}`);
}

test("fake backend completes, returns a private artifact and makes exact retries idempotent", async () => {
  const inputManifest = [hash("a")];
  const request = {
    contractEnvelope: { payload: {
      kind: "ExecutionContract", contractId: "contract-fake-1", taskId: "task-1", quoteId: "quote-1", idempotencyKey: "idem-fake-1",
      backendBinding: { backend: "local-fixture", selectedOfferId: "offer-1", selectedProviderId: "miner-1", offerSnapshotHash: hash("c"), acceptedQuoteTotalAtomic: 100, executionRewardAtomic: 80, inputHash: contentHash(inputManifest), deadline: new Date(Date.now() + 60_000).toISOString() },
    } },
    inputManifest,
    privatePayload: { prompt: "hello" },
  };
  const backend = new FakeComputeBackend({ execute: async () => ({ answer: "hello world" }) });
  const submitted = await backend.submit(request);
  const duplicate = await backend.submit(request);
  assert.equal(duplicate.contractId, submitted.contractId);
  assert.equal(duplicate.state, submitted.state);
  const result = await waitForState(backend, submitted.contractId, ["SUCCEEDED"]);
  assert.match(result.outputHash, /^sha256:[0-9a-f]{64}$/);
  assert.deepEqual((await backend.fetchArtifacts(submitted.contractId)).output, { answer: "hello world" });
  await assert.rejects(backend.submit({ ...request, privatePayload: { prompt: "changed" } }), { code: "IDEMPOTENCY_CONFLICT" });
  const reusedContract = { ...request, contractEnvelope: { ...request.contractEnvelope, payload: { ...request.contractEnvelope.payload, idempotencyKey: "a-different-key" } } };
  await assert.rejects(backend.submit(reusedContract), { code: "IDEMPOTENCY_CONFLICT" });
});

test("fake backend exposes timeout, provider failure and cancellation without inventing success", async () => {
  const requestFor = (id) => {
    const inputManifest = [];
    return {
      contractEnvelope: { payload: {
        kind: "ExecutionContract", contractId: id, taskId: id, quoteId: "q", idempotencyKey: id,
        backendBinding: { backend: "local-fixture", selectedOfferId: "o", selectedProviderId: "p", offerSnapshotHash: hash("d"), acceptedQuoteTotalAtomic: 1, executionRewardAtomic: 1, inputHash: contentHash(inputManifest), deadline: new Date(Date.now() + 60_000).toISOString() },
      } }, inputManifest, privatePayload: {},
    };
  };
  const timeout = new FakeComputeBackend({ faults: { timeout: true } });
  const t = await timeout.submit(requestFor("timeout-job"));
  assert.equal((await waitForState(timeout, t.contractId, ["TIMEOUT"])).state, "TIMEOUT");

  const failure = new FakeComputeBackend({ faults: { providerFailure: true } });
  const f = await failure.submit(requestFor("provider-failure-job"));
  assert.equal((await waitForState(failure, f.contractId, ["PROVIDER_FAILED"])).state, "PROVIDER_FAILED");

  const cancellation = new FakeComputeBackend({ faults: { delayMs: 300 } });
  const c = await cancellation.submit(requestFor("cancel-job"));
  await cancellation.cancel(c.contractId, "test cancellation");
  assert.equal((await waitForState(cancellation, c.contractId, ["CANCELLED"])).state, "CANCELLED");
});

test("Golem adapter scans a selected offer, enforces provider and spend bounds, and fetches result", async () => {
  const offer = fakeOffer();
  const rentHours = 120 / 3600;
  const selected = createGolemOfferSnapshot(offer, rentHours);
  const signed = await signedGolemRequest({ offer: selected });
  const calls = { allocation: null, order: null, upload: null, run: null, finalized: 0, released: 0 };
  const rental = {
    agreement: { id: "agreement-1", provider: { id: "provider-1" } },
    async getExeUnit() {
      return {
        async uploadJson(data, path) { calls.upload = { data, path }; return { result: "Ok" }; },
        async run(executable, args) { calls.run = { executable, args }; return { result: "Ok" }; },
        async downloadJson(path) { assert.equal(path, "/golem/amn-output.json"); return { getOutputAsJson: () => ({ answer: "done", count: 2 }) }; },
      };
    },
    async stopAndFinalize() { calls.finalized += 1; },
  };
  const proposal = {
    provider: { id: "provider-1" },
    properties: { "golem.runtime.name": "vm", "golem.runtime.version": "0.6.0" },
    getEstimatedCost: () => 0.001,
  };
  const network = {
    async connect() {},
    async disconnect() {},
    market: {
      buildScanSpecification: (options) => options,
      scan: () => ({ subscribe(observer) { observer.next(offer); observer.complete(); return { unsubscribe() {} }; } }),
    },
    payment: {
      async createAllocation(params) { calls.allocation = params; return { id: "allocation-1" }; },
      async getAllocation() { return { spentAmount: "0.002" }; },
      async releaseAllocation() { calls.released += 1; },
    },
    async oneOf({ order }) {
      calls.order = order;
      assert.equal(order.market.offerProposalFilter(proposal), true);
      return rental;
    },
  };
  const backend = new GolemYagnaBackend({
    config: { appKey: "test-key", network: "hoodi", token: "tglm", paymentPlatform: "erc20-hoodi-tglm" },
    networkFactory: () => network,
    identityResolver: signed.identityResolver,
    replayStore: new MemoryReplayStore(),
    host: { supported: true, platform: "linux", arch: "x64", nodeVersion: "v24.22.1", reasons: [] },
  });
  const submitted = await backend.submit(signed);
  assert.deepEqual(await backend.submit(signed), submitted);
  await assert.rejects(backend.submit({ ...signed, privatePayload: { message: "altered after acceptance" } }), { code: "IDEMPOTENCY_CONFLICT" });
  const done = await waitForState(backend, submitted.contractId, ["SUCCEEDED"]);
  assert.equal(done.costReconciliation, "RECONCILED");
  assert.equal(done.actualCostGlm, "0.002");
  assert.equal(calls.allocation.budget, 0.01);
  assert.equal(calls.order.demand.workload.imageHash, "c".repeat(56));
  assert.equal(calls.order.market.pricing.maxCpuPerHourPrice, offer.pricing.cpuSec * 3600);
  assert.equal(calls.upload.path, "/golem/amn-input.json");
  assert.equal(calls.run.executable, "/opt/amn/bin/amn-golem-worker");
  assert.equal(calls.finalized, 1);
  assert.equal(calls.released, 1);
  assert.deepEqual((await backend.fetchArtifacts(done.contractId)).output, { answer: "done", count: 2 });
  await backend.close();
});

test("Golem adapter rejects changed offer snapshots before provider dispatch", async () => {
  const offer = fakeOffer();
  const selected = createGolemOfferSnapshot(offer, 120 / 3600);
  const signed = await signedGolemRequest({ offer: selected });
  const changedOffer = fakeOffer({ pricing: { start: 0.0002, cpuSec: 0.0000001, envSec: 0.0000001 } });
  const network = {
    async connect() {},
    async disconnect() {},
    market: {
      buildScanSpecification: (options) => options,
      scan: () => ({ subscribe(observer) { observer.next(changedOffer); observer.complete(); return { unsubscribe() {} }; } }),
    },
  };
  const backend = new GolemYagnaBackend({
    config: { appKey: "test-key" }, networkFactory: () => network,
    identityResolver: signed.identityResolver, replayStore: new MemoryReplayStore(),
    host: { supported: true, platform: "linux", arch: "x64", nodeVersion: "v24.22.1", reasons: [] },
  });
  await assert.rejects(backend.submit(signed), { code: "OFFER_CHANGED" });
  await backend.close();
});

test("Golem host checks reject unsupported Windows requestors and production never falls back", async () => {
  const host = assessGolemHost({ platform: "win32", arch: "x64", nodeVersion: "24.22.1" });
  assert.equal(host.supported, false);
  assert.match(host.reasons.join(" "), /Linux/);
  assert.throws(() => createComputeBackend({ mode: "production", backend: "local-fake" }), { code: "UNAVAILABLE" });
  const backend = new GolemYagnaBackend({ host, config: {} });
  const caps = await backend.capabilities();
  assert.equal(caps.available, false);
  assert.equal(caps.productionReady, false);
  assert.ok(caps.reasons.length > 0);
  assert.ok(allocationBudgetForCap("0.1") <= 0.1);
  assert.equal(allocationBudgetForCap("0.01"), 0.01);
});

test("file job store survives restart and does not persist input or output values", async () => {
  const directory = await mkdtemp(join(tmpdir(), "amn-golem-store-"));
  try {
    const store = new FileJobStore(directory);
    const record = { contractId: "persisted-job", idempotencyKey: "persisted-idem", state: "ASSIGNED", requestDigest: hash("e") };
    await store.create(record);
    const restored = await new FileJobStore(directory).findByIdempotencyKey("persisted-idem");
    assert.equal(restored.contractId, record.contractId);
    const raw = await readFile(join(directory, (await import("node:crypto")).createHash("sha256").update(record.idempotencyKey).digest("hex") + ".json"), "utf8");
    assert.equal(raw.includes("privatePayload"), false);
    assert.equal(raw.includes("output"), false);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
