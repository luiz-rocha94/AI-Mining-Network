import { ComputeBackendError } from "./errors.mjs";
import { artifactHash, delay, extractContract, publicJob, requestFingerprint, validateIdempotentRetry } from "./common.mjs";
import { MemoryJobStore } from "./job-store.mjs";

/** Deterministic, offline ComputeBackend used by local development and fault tests. */
export class FakeComputeBackend {
  backendId = "local-fake";
  #store;
  #offers;
  #execute;
  #faults;
  #running = new Map();
  #artifacts = new Map();

  constructor({ offers = [], execute = async ({ privatePayload }) => privatePayload, faults = {}, store = new MemoryJobStore() } = {}) {
    this.#offers = structuredClone(offers);
    this.#execute = execute;
    this.#faults = faults;
    this.#store = store;
  }

  async capabilities() {
    return {
      backendId: this.backendId,
      available: true,
      productionReady: false,
      supportsCancellation: true,
      supportsArtifacts: true,
      trustNotes: ["Development fixture only; offers, execution and costs are simulated."],
    };
  }

  async offers(requirements = {}) {
    return structuredClone(this.#offers.filter((offer) => {
      if (offer.available === false) return false;
      if (requirements.runtimeName && offer.runtimeName !== requirements.runtimeName) return false;
      if (requirements.minCpuCores && offer.cpuCores < requirements.minCpuCores) return false;
      if (requirements.minMemoryGib && offer.memoryGib < requirements.minMemoryGib) return false;
      return true;
    }));
  }

  async submit(request) {
    const { contract, binding, contractId, idempotencyKey } = extractContract(request);
    if (binding.backend !== "local-fixture") {
      throw new ComputeBackendError("INVALID_CONTRACT", "fake backend only accepts local-fixture contract bindings");
    }
    const digest = requestFingerprint(request);
    const existing = await this.#store.findByIdempotencyKey(idempotencyKey);
    if (existing) return validateIdempotentRetry(existing, digest);
    const sameContract = await this.#store.get(contractId);
    if (sameContract) throw new ComputeBackendError("IDEMPOTENCY_CONFLICT", "execution contract ID was already used with another idempotency key");

    const now = new Date().toISOString();
    const record = {
      backendId: this.backendId,
      contractId,
      taskId: contract.taskId,
      quoteId: contract.quoteId,
      idempotencyKey,
      requestDigest: digest,
      state: "QUEUED",
      createdAt: now,
      updatedAt: now,
      selectedOfferId: binding.selectedOfferId,
      selectedProviderId: binding.selectedProviderId,
      offerSnapshotHash: binding.offerSnapshotHash,
      events: [{ state: "QUEUED", at: now }],
    };
    record.submitResponse = publicJob(record);
    if (!(await this.#store.create(record))) {
      const raced = await this.#store.findByIdempotencyKey(idempotencyKey);
      if (!raced) throw new ComputeBackendError("UNAVAILABLE", "job store rejected dispatch but record could not be read", { retryable: true });
      return validateIdempotentRetry(raced, digest);
    }
    const controller = new AbortController();
    this.#running.set(contractId, controller);
    void this.#run(record, request, controller.signal);
    return publicJob(record);
  }

  async #transition(record, state, details = {}) {
    const at = new Date().toISOString();
    const updated = { ...record, ...details, state, updatedAt: at, events: [...record.events, { state, at, ...details }] };
    await this.#store.update(updated);
    Object.assign(record, updated);
  }

  async #run(record, request, signal) {
    try {
      const fault = this.#faults[record.contractId] ?? this.#faults;
      await this.#transition(record, "ASSIGNED");
      if (fault.delayMs) await delay(fault.delayMs, signal);
      if (fault.timeout) throw new ComputeBackendError("TIMEOUT", "fake provider exceeded the configured execution timeout", { retryable: true });
      if (fault.providerFailure) throw new ComputeBackendError("PROVIDER_FAILED", "fake provider disappeared during execution", { retryable: true });
      if (fault.executionFailure) throw new ComputeBackendError("PROVIDER_FAILED", "fake worker returned a non-zero exit status", { retryable: false });
      if (signal.aborted) throw signal.reason ?? new Error("cancelled");
      await this.#transition(record, "RUNNING");
      const result = await this.#execute({ ...request, signal, contractId: record.contractId });
      if (signal.aborted) throw signal.reason ?? new Error("cancelled");
      const output = fault.corruptOutput ? { corrupted: true, output: result } : result;
      const outputHash = artifactHash(output);
      this.#artifacts.set(record.contractId, { output, outputHash });
      await this.#transition(record, "SUCCEEDED", { outputHash });
    } catch (error) {
      if (signal.aborted) {
        await this.#transition(record, "CANCELLED", { error: "execution cancelled" });
      } else if (error instanceof ComputeBackendError) {
        await this.#transition(record, error.code, { error: error.message, retryable: error.retryable });
      } else {
        await this.#transition(record, "PROVIDER_FAILED", { error: error instanceof Error ? error.message : String(error), retryable: true });
      }
    } finally {
      this.#running.delete(record.contractId);
    }
  }

  async status(contractId) {
    const record = await this.#store.get(contractId);
    if (!record) throw new ComputeBackendError("NOT_FOUND", `compute contract ${contractId} was not found`);
    return publicJob(record);
  }

  async cancel(contractId, reason = "cancelled by customer") {
    const record = await this.#store.get(contractId);
    if (!record) throw new ComputeBackendError("NOT_FOUND", `compute contract ${contractId} was not found`);
    if (["SUCCEEDED", "FAILED", "PROVIDER_FAILED", "TIMEOUT", "CANCELLED"].includes(record.state)) return publicJob(record);
    this.#running.get(contractId)?.abort(new ComputeBackendError("CANCELLED", reason));
    if (!this.#running.has(contractId)) {
      const cancelled = { ...record, state: "CANCELLED", updatedAt: new Date().toISOString(), events: [...record.events, { state: "CANCELLED", at: new Date().toISOString(), reason }] };
      await this.#store.update(cancelled);
      return publicJob(cancelled);
    }
    return this.status(contractId);
  }

  async fetchArtifacts(contractId) {
    const record = await this.#store.get(contractId);
    if (!record) throw new ComputeBackendError("NOT_FOUND", `compute contract ${contractId} was not found`);
    const artifacts = this.#artifacts.get(contractId);
    if (!artifacts || record.state !== "SUCCEEDED") {
      throw new ComputeBackendError("ARTIFACT_MISSING", "successful output artifact is unavailable");
    }
    return structuredClone(artifacts);
  }
}
