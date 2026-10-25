import { contentHash } from "@ai-mining-network/protocol";
import { FakeComputeBackend } from "../src/index.mjs";

const inputManifest = ["sha256:invoice-a", "sha256:invoice-b"];
const backend = new FakeComputeBackend({
  offers: [{ offerId: "demo-offer", runtimeName: "local-fixture", cpuCores: 4, memoryGib: 8, available: true }],
  execute: async ({ privatePayload }) => ({ answer: `Processed: ${privatePayload.prompt}` }),
});
const binding = {
  backend: "local-fixture",
  selectedOfferId: "demo-offer",
  selectedProviderId: "demo-miner",
  offerSnapshotHash: `sha256:${"a".repeat(64)}`,
  acceptedQuoteTotalAtomic: 1_000,
  executionRewardAtomic: 800,
  inputHash: contentHash(inputManifest),
  deadline: new Date(Date.now() + 60_000).toISOString(),
};
const request = {
  contractEnvelope: { payload: {
    kind: "ExecutionContract",
    contractId: "demo-contract-p7",
    taskId: "demo-task-p7",
    quoteId: "demo-quote-p7",
    idempotencyKey: "demo-task-p7:settle:v1",
    backendBinding: binding,
  } },
  inputManifest,
  privatePayload: { prompt: "Summarize the two demo invoices" },
};

const accepted = await backend.submit(request);
let status;
do {
  status = await backend.status(accepted.contractId);
  if (status.state !== "SUCCEEDED") await new Promise((resolve) => setTimeout(resolve, 5));
} while (!["SUCCEEDED", "FAILED", "PROVIDER_FAILED", "TIMEOUT", "CANCELLED"].includes(status.state));
const artifacts = await backend.fetchArtifacts(status.contractId);
console.log(JSON.stringify({ backend: "local-fake", state: status.state, outputHash: artifacts.outputHash, output: artifacts.output }, null, 2));
