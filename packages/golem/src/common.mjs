import { createHash } from "node:crypto";
import { contentHash } from "@ai-mining-network/protocol";
import { ComputeBackendError } from "./errors.mjs";

export function stableArtifactString(value) {
  if (value === null || typeof value === "string" || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TypeError("artifact JSON cannot contain a non-finite number");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(stableArtifactString).join(",")}]`;
  if (value && Object.getPrototypeOf(value) === Object.prototype) {
    const entries = Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableArtifactString(value[key])}`);
    return `{${entries.join(",")}}`;
  }
  throw new TypeError("artifact must be plain JSON data");
}

export function artifactHash(value) {
  return `sha256:${createHash("sha256").update(stableArtifactString(value), "utf8").digest("hex")}`;
}

export function requestFingerprint(request) {
  return artifactHash({
    contractEnvelope: request.contractEnvelope,
    inputManifest: request.inputManifest,
    privatePayload: request.privatePayload,
  });
}

export function extractContract(request) {
  const envelope = request?.contractEnvelope;
  const contract = envelope?.payload;
  const binding = contract?.backendBinding;
  if (!envelope || contract?.kind !== "ExecutionContract" || !binding) {
    throw new ComputeBackendError("INVALID_CONTRACT", "signed execution contract and backend binding are required");
  }
  if (!contract.contractId || !contract.taskId || !contract.quoteId || !contract.idempotencyKey) {
    throw new ComputeBackendError("INVALID_CONTRACT", "execution contract is missing an identifier or idempotency key");
  }
  if (!Array.isArray(request.inputManifest)) {
    throw new ComputeBackendError("INVALID_CONTRACT", "inputManifest must be a JSON array");
  }
  let manifestHash;
  try {
    manifestHash = contentHash(request.inputManifest);
  } catch (error) {
    throw new ComputeBackendError("INVALID_CONTRACT", `input manifest is not canonical protocol JSON: ${error.message}`);
  }
  if (manifestHash !== binding.inputHash) {
    throw new ComputeBackendError("INVALID_CONTRACT", "input manifest hash does not match the signed backend binding");
  }
  if (binding.backend !== "local-fixture" && binding.backend !== "golem-yagna") {
    throw new ComputeBackendError("INVALID_CONTRACT", "unsupported compute backend in signed binding");
  }
  if (!binding.selectedOfferId || !binding.selectedProviderId || !binding.offerSnapshotHash) {
    throw new ComputeBackendError("INVALID_CONTRACT", "backend binding is missing its selected offer snapshot");
  }
  if (!Number.isSafeInteger(binding.acceptedQuoteTotalAtomic) || binding.acceptedQuoteTotalAtomic < 0) {
    throw new ComputeBackendError("INVALID_CONTRACT", "accepted quote total must be a non-negative safe integer");
  }
  if (!Number.isSafeInteger(binding.executionRewardAtomic) || binding.executionRewardAtomic < 0) {
    throw new ComputeBackendError("INVALID_CONTRACT", "execution reward must be a non-negative safe integer");
  }
  const deadline = Date.parse(binding.deadline);
  if (!Number.isFinite(deadline)) throw new ComputeBackendError("INVALID_CONTRACT", "backend deadline is invalid");
  return { envelope, contract, binding, deadline, contractId: contract.contractId, idempotencyKey: contract.idempotencyKey };
}

export function publicJob(record) {
  const { output: _privateOutput, input: _privateInput, submitResponse: _submitResponse, ...safe } = record;
  return structuredClone(safe);
}

export function delay(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(signal.reason ?? new Error("aborted"));
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(timer);
      reject(signal.reason ?? new Error("aborted"));
    }, { once: true });
  });
}

export function validateIdempotentRetry(existing, digest) {
  if (existing.requestDigest !== digest) {
    throw new ComputeBackendError("IDEMPOTENCY_CONFLICT", "idempotency key was reused with different contract or task content");
  }
  return structuredClone(existing.submitResponse ?? publicJob(existing));
}
