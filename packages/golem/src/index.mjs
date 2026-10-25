import { ComputeBackendError } from "./errors.mjs";
import { FakeComputeBackend } from "./fake-backend.mjs";
import { GolemYagnaBackend } from "./golem-yagna-backend.mjs";

export { BACKEND_ERROR_CODES, ComputeBackendError } from "./errors.mjs";
export { FakeComputeBackend } from "./fake-backend.mjs";
export { GolemYagnaBackend, allocationBudgetForCap, assessGolemHost, createGolemOfferSnapshot, validateGolemOfferBinding } from "./golem-yagna-backend.mjs";
export { FileJobStore, MemoryJobStore } from "./job-store.mjs";
export { artifactHash, extractContract, stableArtifactString } from "./common.mjs";
export { FileReplayStore } from "@ai-mining-network/protocol/node-replay-store";

/** Explicit backend selection. Live mode never falls back to the development fake. */
export function createComputeBackend({ backend, ...options } = {}) {
  const mode = options.mode ?? process.env.AMN_ENV ?? "development";
  const selected = backend ?? process.env.AMN_COMPUTE_BACKEND ?? (mode === "production" ? "golem-yagna" : "local-fake");
  if (mode === "production" && selected !== "golem-yagna") {
    throw new ComputeBackendError("UNAVAILABLE", "production mode cannot use the local fake compute backend");
  }
  if (selected === "local-fake") return new FakeComputeBackend(options.fake);
  if (selected === "golem-yagna") return new GolemYagnaBackend({ ...options.golem, config: { ...options.golem?.config, mode } });
  throw new ComputeBackendError("UNAVAILABLE", `unknown compute backend '${selected}'; choose local-fake explicitly or configure golem-yagna`);
}
