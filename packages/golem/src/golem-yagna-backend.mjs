import { GolemNetwork } from "@golem-sdk/golem-js";
import { MemoryReplayStore, contentHash, validateEnvelope } from "@ai-mining-network/protocol";
import { ComputeBackendError, classifyGolemError } from "./errors.mjs";
import { artifactHash, extractContract, publicJob, requestFingerprint, validateIdempotentRetry } from "./common.mjs";
import { MemoryJobStore } from "./job-store.mjs";

const GIB = 1024 ** 3;
const IMAGE_DIGEST = /^sha3-224:[0-9a-f]{56}$/;
const SAFE_DECIMAL = /^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,18})?$/;

export function assessGolemHost({ platform = process.platform, arch = process.arch, nodeVersion = process.versions.node } = {}) {
  const reasons = [];
  if (platform !== "linux") reasons.push("Golem Yagna requestor is only supported on Linux by the current official setup guide.");
  if (arch !== "x64") reasons.push("This adapter currently requires an x86-64 requestor host.");
  const majorNode = Number.parseInt(nodeVersion.split(".")[0], 10);
  if (!Number.isInteger(majorNode) || majorNode < 20) reasons.push("The adapter requires Node.js 20 or newer (supported LTS runtime).");
  return { supported: reasons.length === 0, platform, arch, nodeVersion, reasons };
}

function assertDecimal(value, label) {
  if (typeof value !== "string" || !SAFE_DECIMAL.test(value) || Number(value) <= 0 || !Number.isFinite(Number(value))) {
    throw new ComputeBackendError("INVALID_CONTRACT", `${label} must be a positive decimal string with at most 18 fractional digits`);
  }
  return value;
}

function decimalUnits(value, decimals = 18) {
  const text = String(value);
  const match = /^(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:e([+-]?[0-9]+))?$/i.exec(text);
  if (!match) throw new TypeError(`invalid decimal amount ${text}`);
  const fraction = match[2] ?? "";
  const exponent = Number(match[3] ?? 0);
  const scale = fraction.length - exponent;
  const digits = BigInt(`${match[1]}${fraction}` || "0");
  if (scale > decimals && /[1-9]/.test(`${match[1]}${fraction}`.slice(-(scale - decimals)))) {
    throw new TypeError("decimal amount exceeds supported precision");
  }
  if (scale <= decimals) return digits * 10n ** BigInt(decimals - scale);
  return digits / 10n ** BigInt(scale - decimals);
}

function nextRepresentableDown(value) {
  if (value <= 0) return 0;
  const bytes = Buffer.allocUnsafe(8);
  bytes.writeDoubleBE(value);
  bytes.writeBigUInt64BE(bytes.readBigUInt64BE() - 1n);
  return bytes.readDoubleBE();
}

export function allocationBudgetForCap(cap) {
  const capUnits = decimalUnits(assertDecimal(cap, "nativeCostCapGlm"));
  let budget = Number(cap);
  if (!Number.isFinite(budget) || budget <= 0) throw new ComputeBackendError("INVALID_CONTRACT", "native GLM cap cannot be represented by Yagna's numeric budget API");
  // Yagna's documented SDK accepts a JavaScript number. Round toward zero if
  // its decimal representation would otherwise exceed the exact signed cap.
  while (decimalUnits(decimalString(budget)) > capUnits) budget = nextRepresentableDown(budget);
  if (budget <= 0) throw new ComputeBackendError("INVALID_CONTRACT", "native GLM cap is below the Yagna numeric budget precision");
  return budget;
}

function decimalString(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) throw new TypeError("Golem SDK returned an invalid price");
  return number.toString();
}

function gibToMib(gib) {
  return Math.ceil(gib * 1024);
}

function requirementsToScan(binding) {
  const requirements = binding.resourceRequirements;
  const gpuRequired = requirements.gpuRequired === true;
  if (!Number.isSafeInteger(requirements.cpuCores) || requirements.cpuCores < 1
    || (requirements.cpuThreads !== undefined && (!Number.isSafeInteger(requirements.cpuThreads) || requirements.cpuThreads < requirements.cpuCores))
    || !Number.isSafeInteger(requirements.ramBytes) || requirements.ramBytes < 1
    || (requirements.storageBytes !== undefined && (!Number.isSafeInteger(requirements.storageBytes) || requirements.storageBytes < 1))
    || !Number.isSafeInteger(requirements.timeoutSeconds) || requirements.timeoutSeconds < 1) {
    throw new ComputeBackendError("INVALID_CONTRACT", "resource requirements contain invalid CPU, memory, storage or timeout values");
  }
  if (gpuRequired && binding.runtimeName !== "vm-nvidia") {
    throw new ComputeBackendError("UNAVAILABLE", "GPU execution requires an explicitly configured vm-nvidia runtime; arbitrary consumer GPUs are not implied");
  }
  if (gpuRequired && requirements.vramBytes > 0) {
    throw new ComputeBackendError("UNAVAILABLE", "Golem's current adapter path cannot enforce a minimum VRAM amount; remove the requirement or use another backend");
  }
  if (requirements.networkAccess === true) {
    throw new ComputeBackendError("UNAVAILABLE", "network-enabled workloads need a separately configured Golem payload manifest and outbound URL policy");
  }
  return {
    workload: {
      runtime: { name: binding.runtimeName, version: binding.runtimeVersion },
      capabilities: gpuRequired ? ["!exp:gpu"] : [],
      minMemGib: Math.ceil((requirements.ramBytes / GIB) * 100) / 100,
      minStorageGib: Math.ceil(((requirements.storageBytes ?? GIB) / GIB) * 100) / 100,
      minCpuThreads: requirements.cpuThreads ?? requirements.cpuCores,
      minCpuCores: requirements.cpuCores,
    },
  };
}

export function createGolemOfferSnapshot(offer, rentHours) {
  const cost = offer.getEstimatedCost(rentHours);
  const createdAt = Date.parse(offer.offerCreateAt);
  if (!Number.isFinite(createdAt)) throw new ComputeBackendError("PROVIDER_FAILED", "Golem offer has an invalid creation timestamp");
  const snapshot = {
    backend: "golem-yagna",
    offerId: offer.offerId,
    providerId: offer.provider.id,
    providerName: offer.provider.name,
    createdAt: new Date(createdAt).toISOString(),
    runtimeName: offer.runtimeName,
    runtimeVersion: offer.runtimeVersion,
    cpuCores: offer.cpuCores,
    cpuThreads: offer.cpuThreads,
    memoryMiB: gibToMib(offer.memoryGib),
    storageMiB: gibToMib(offer.storageGib),
    runtimeCapabilities: [...offer.runtimeCapabilities].sort(),
    cpuCapabilities: [...offer.cpuCapabilities].sort(),
    gpuBrand: offer.gpuBrand ?? null,
    ratesGlm: {
      start: decimalString(offer.pricing.start),
      cpuPerSecond: decimalString(offer.pricing.cpuSec),
      environmentPerSecond: decimalString(offer.pricing.envSec),
    },
    estimatedCostGlm: decimalString(cost),
    rentHours: decimalString(rentHours),
    paymentPlatforms: Object.fromEntries(Object.entries(offer.paymentPlatformAddresses ?? {}).sort(([a], [b]) => a.localeCompare(b))),
  };
  return { snapshot, offerSnapshotHash: contentHash(snapshot), expiresAt: new Date(createdAt + 60 * 60 * 1000).toISOString() };
}

function collectObservable(observable, timeoutMs, maxOffers) {
  return new Promise((resolve, reject) => {
    const values = [];
    let settled = false;
    let subscription;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      subscription?.unsubscribe();
      resolve(values);
    };
    const timer = setTimeout(finish, timeoutMs);
    subscription = observable.subscribe({
      next(value) {
        values.push(value);
        if (values.length >= maxOffers) finish();
      },
      error(error) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(error);
      },
      complete: finish,
    });
  });
}

function assertSdkResult(result, operation) {
  if (result?.result && result.result !== "Ok") {
    throw new ComputeBackendError("PROVIDER_FAILED", `Golem ${operation} failed: ${result.message ?? result.result}`, { retryable: true });
  }
}

function artifactFromDownload(result) {
  if (typeof result?.getOutputAsJson === "function") return result.getOutputAsJson();
  if (result && Object.prototype.hasOwnProperty.call(result, "data")) return result.data;
  throw new ComputeBackendError("ARTIFACT_MISSING", "Golem result download did not contain JSON data");
}

function capResultCost(spentAmount, cap) {
  try {
    return decimalUnits(spentAmount) <= decimalUnits(cap);
  } catch {
    return false;
  }
}

/**
 * Golem Yagna requestor adapter. It uses golem-js market scanning, ResourceRental,
 * VM image demands and a Yagna allocation as the native-token spend ceiling.
 * Private task payloads are sent directly to the negotiated VM, never to IPFS.
 */
export class GolemYagnaBackend {
  backendId = "golem-yagna";
  #config;
  #networkFactory;
  #network;
  #identityResolver;
  #replayStore;
  #jobStore;
  #jobs = new Map();
  #artifacts = new Map();
  #controllers = new Map();
  #inflight = new Map();
  #host;

  constructor({
    config = {},
    networkFactory = (options) => new GolemNetwork(options),
    identityResolver,
    replayStore,
    jobStore = new MemoryJobStore(),
    host = assessGolemHost(),
  } = {}) {
    this.#config = {
      mode: config.mode ?? process.env.AMN_ENV ?? "development",
      yagnaUrl: config.yagnaUrl ?? process.env.YAGNA_API_URL ?? "http://127.0.0.1:7465",
      appKey: config.appKey ?? process.env.YAGNA_APP_KEY,
      network: config.network ?? process.env.YAGNA_PAYMENT_NETWORK ?? "hoodi",
      driver: config.driver ?? "erc20",
      token: config.token ?? "tglm",
      paymentPlatform: config.paymentPlatform ?? "erc20-hoodi-tglm",
      subnetTag: config.subnetTag,
      scanTimeoutMs: config.scanTimeoutMs ?? 10_000,
      maxOffers: config.maxOffers ?? 100,
      maxInputBytes: config.maxInputBytes ?? 64 * 1024 * 1024,
      imagePath: config.imagePath ?? "/golem/amn-input.json",
      outputPath: config.outputPath ?? "/golem/amn-output.json",
      workerExecutable: config.workerExecutable ?? "/opt/amn/bin/amn-golem-worker",
      workerArgs: config.workerArgs ?? ["--input", "/golem/amn-input.json", "--output", "/golem/amn-output.json"],
      chainId: config.chainId ?? 31337,
      maxContractLifetimeMs: config.maxContractLifetimeMs ?? 24 * 60 * 60 * 1000,
    };
    this.#networkFactory = networkFactory;
    this.#identityResolver = identityResolver;
    this.#replayStore = replayStore ?? new MemoryReplayStore();
    this.#jobStore = jobStore;
    this.#host = host;
  }

  async capabilities() {
    const blockingReasons = [...this.#host.reasons];
    if (!this.#config.appKey) blockingReasons.push("YAGNA_APP_KEY is not configured.");
    const productionReasons = [];
    if (!this.#identityResolver) productionReasons.push("An AMN identity resolver is required to admit customer-signed dispatch contracts.");
    if (!this.#jobStore.durable) productionReasons.push("A durable job store is required for production idempotency and recovery.");
    if (!this.#replayStore?.durable) productionReasons.push("A durable replay store is required for production request admission.");
    if (!this.#config.workerExecutable.startsWith("/")) productionReasons.push("The trusted worker executable must be an absolute path inside the configured VM image.");
    const reasons = [...blockingReasons, ...productionReasons];
    let connected = false;
    if (blockingReasons.length === 0) {
      try {
        await this.#ensureConnected();
        connected = true;
      } catch (error) {
        const reason = `Yagna connection failed: ${error instanceof Error ? error.message : String(error)}`;
        blockingReasons.push(reason);
        reasons.push(reason);
      }
    }
    return {
      backendId: this.backendId,
      available: connected,
      productionReady: connected && reasons.length === 0,
      host: this.#host,
      yagnaUrl: this.#config.yagnaUrl,
      payment: { network: this.#config.network, driver: this.#config.driver, token: this.#config.token, paymentPlatform: this.#config.paymentPlatform },
      sdk: "@golem-sdk/golem-js@3.11.0",
      trustNotes: [
        "Yagna market offers and provider measurements are signed marketplace claims, not trusted hardware attestation.",
        "A Yagna allocation caps native GLM exposure; it does not settle the AMN customer quote or prove useful AI work.",
        "The provider can inspect plaintext inputs inside its VM; this adapter does not provide confidential-computing guarantees.",
      ],
      reasons,
    };
  }

  async #ensureConnected() {
    if (!this.#host.supported) throw new ComputeBackendError("UNAVAILABLE", this.#host.reasons.join(" "));
    if (!this.#config.appKey) throw new ComputeBackendError("UNAVAILABLE", "YAGNA_APP_KEY is not configured");
    if (!this.#network) {
      this.#network = this.#networkFactory({
        api: { url: this.#config.yagnaUrl, key: this.#config.appKey },
        payment: { network: this.#config.network, driver: this.#config.driver, token: this.#config.token },
      });
      await this.#network.connect();
    }
    return this.#network;
  }

  async close() {
    if (this.#network) {
      const network = this.#network;
      this.#network = undefined;
      await network.disconnect();
    }
  }

  async #scan(binding, { rentHours, timeoutMs = this.#config.scanTimeoutMs } = {}) {
    const network = await this.#ensureConnected();
    const demand = requirementsToScan(binding);
    const specification = network.market.buildScanSpecification({
      ...demand,
      subnetTag: this.#config.subnetTag,
      payment: { network: this.#config.network, driver: this.#config.driver, token: this.#config.token },
    });
    const offers = await collectObservable(network.market.scan(specification), timeoutMs, this.#config.maxOffers);
    const duration = rentHours ?? Math.max(1 / 60, binding.resourceRequirements.timeoutSeconds / 3600);
    return offers.map((offer) => {
      const mapped = createGolemOfferSnapshot(offer, duration);
      return { ...mapped.snapshot, estimatedCostGlm: mapped.snapshot.estimatedCostGlm, offerSnapshotHash: mapped.offerSnapshotHash, expiresAt: mapped.expiresAt };
    });
  }

  async offers(requirements) {
    const request = {
      backend: "golem-yagna",
      runtimeName: requirements.runtimeName ?? "vm",
      runtimeVersion: requirements.runtimeVersion ?? "0.6.0",
      resourceRequirements: {
        cpuCores: requirements.cpuCores ?? 1,
        cpuThreads: requirements.cpuThreads,
        ramBytes: Math.ceil((requirements.minMemoryGib ?? 2) * GIB),
        storageBytes: Math.ceil((requirements.minStorageGib ?? 1) * GIB),
        vramBytes: requirements.vramBytes ?? 0,
        timeoutSeconds: requirements.timeoutSeconds ?? 600,
        gpuRequired: requirements.gpuRequired === true,
        networkAccess: requirements.networkAccess === true,
      },
    };
    try {
      return await this.#scan(request, { rentHours: requirements.rentHours });
    } catch (error) {
      if (error instanceof ComputeBackendError) throw error;
      throw classifyGolemError(error);
    }
  }

  async #verifyContract(envelope) {
    if (!this.#identityResolver) {
      throw new ComputeBackendError("UNAVAILABLE", "Golem dispatch requires an AMN identity resolver; signer recovery alone is not identity authorization");
    }
    try {
      await validateEnvelope(envelope, {
        chainId: this.#config.chainId,
        maxLifetimeMs: this.#config.maxContractLifetimeMs,
        identityResolver: this.#identityResolver,
        replayStore: this.#replayStore,
      });
    } catch (error) {
      throw new ComputeBackendError(error?.code === "REPLAY" ? "REPLAY" : "INVALID_CONTRACT", `signed execution contract rejected: ${error.message}`, { cause: error });
    }
    if (envelope?.payload?.kind !== "ExecutionContract") {
      throw new ComputeBackendError("INVALID_CONTRACT", "signed payload kind must be ExecutionContract");
    }
  }

  async submit(request) {
    const { contractEnvelope } = request ?? {};
    if (!contractEnvelope || contractEnvelope.payload?.kind !== "ExecutionContract") {
      throw new ComputeBackendError("INVALID_CONTRACT", "signed execution contract is required");
    }
    const contractId = contractEnvelope.payload.contractId;
    const idempotencyKey = contractEnvelope.payload.idempotencyKey;
    if (!contractId || !idempotencyKey) throw new ComputeBackendError("INVALID_CONTRACT", "contract ID and idempotency key are required");
    let digest;
    try {
      digest = requestFingerprint(request);
    } catch (error) {
      throw new ComputeBackendError("INVALID_CONTRACT", `compute request is not JSON data: ${error.message}`);
    }
    if (this.#config.mode === "production" && (!this.#jobStore.durable || !this.#replayStore?.durable)) {
      throw new ComputeBackendError("UNAVAILABLE", "production Golem dispatch requires durable idempotency and replay stores");
    }
    const existing = await this.#jobStore.findByIdempotencyKey(idempotencyKey);
    if (existing) return validateIdempotentRetry(existing, digest);
    const sameContract = await this.#jobStore.get(contractId);
    if (sameContract) throw new ComputeBackendError("IDEMPOTENCY_CONFLICT", "execution contract ID was already used with another idempotency key");
    const pending = this.#inflight.get(idempotencyKey);
    if (pending) {
      const result = await pending;
      return validateIdempotentRetry(result, digest);
    }
    const submission = this.#submitOnce(request, digest);
    this.#inflight.set(idempotencyKey, submission);
    try {
      return await submission;
    } finally {
      this.#inflight.delete(idempotencyKey);
    }
  }

  async #submitOnce(request, digest) {
    await this.#verifyContract(request.contractEnvelope);
    const parsed = extractContract(request);
    const { binding, contract, contractId, idempotencyKey, deadline } = parsed;
    if (binding.backend !== "golem-yagna") throw new ComputeBackendError("INVALID_CONTRACT", "Golem backend requires a golem-yagna signed backend binding");
    if (!IMAGE_DIGEST.test(binding.imageDigest ?? "")) throw new ComputeBackendError("INVALID_CONTRACT", "Golem execution requires a pinned sha3-224 image digest");
    const maxCost = assertDecimal(binding.nativeCostCapGlm, "nativeCostCapGlm");
    if (!Array.isArray(request.inputManifest) || !Object.hasOwn(request, "privatePayload")) {
      throw new ComputeBackendError("INVALID_CONTRACT", "input manifest and private payload are required");
    }
    let privatePayloadHash;
    try {
      privatePayloadHash = contentHash(request.privatePayload);
    } catch (error) {
      throw new ComputeBackendError("INVALID_CONTRACT", `private payload is not canonical protocol JSON: ${error.message}`);
    }
    if (request.inputManifest.length !== 1 || request.inputManifest[0]?.payloadHash !== privatePayloadHash) {
      throw new ComputeBackendError("INVALID_CONTRACT", "Golem input manifest must bind the canonical private payload hash");
    }
    let inputJson;
    try {
      inputJson = JSON.stringify({ manifest: request.inputManifest, payload: request.privatePayload });
    } catch (error) {
      throw new ComputeBackendError("INVALID_CONTRACT", `private payload cannot be serialized: ${error.message}`);
    }
    if (Buffer.byteLength(inputJson, "utf8") > this.#config.maxInputBytes) {
      throw new ComputeBackendError("INVALID_CONTRACT", `private input exceeds the configured ${this.#config.maxInputBytes} byte limit`);
    }
    const now = Date.now();
    const timeoutMs = Math.min(binding.resourceRequirements.timeoutSeconds * 1000, deadline - now, Date.parse(contract.commitDeadline) - now);
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new ComputeBackendError("TIMEOUT", "execution deadline has already elapsed");
    // The customer quote binds a stable maximum rental duration. Re-scanning
    // with a shrinking wall-clock duration would make an unchanged offer hash
    // appear to mutate while the accepted task is waiting to start.
    const rentHours = Math.max(1 / 60, binding.resourceRequirements.timeoutSeconds / 3600);
    const snapshot = await this.#scan(binding, { rentHours });
    const selected = snapshot.find((offer) => offer.offerId === binding.selectedOfferId && offer.providerId === binding.selectedProviderId);
    if (!selected) throw new ComputeBackendError("OFFER_EXPIRED", "selected Golem offer is no longer available", { retryable: true });
    if (selected.offerSnapshotHash !== binding.offerSnapshotHash) {
      throw new ComputeBackendError("OFFER_CHANGED", "selected Golem offer terms changed after the accepted quote", { retryable: true, details: { expected: binding.offerSnapshotHash, actual: selected.offerSnapshotHash } });
    }
    if (Date.parse(selected.expiresAt) <= Date.now()) {
      throw new ComputeBackendError("OFFER_EXPIRED", "selected Golem offer has exceeded the adapter freshness window", { retryable: true });
    }
    if (decimalUnits(selected.estimatedCostGlm) > decimalUnits(maxCost)) {
      throw new ComputeBackendError("COST_CAP_EXCEEDED", "selected offer estimate exceeds the signed native cost cap");
    }

    const nowIso = new Date().toISOString();
    const record = {
      backendId: this.backendId,
      contractId,
      taskId: contract.taskId,
      quoteId: contract.quoteId,
      idempotencyKey,
      requestDigest: digest,
      state: "ASSIGNED",
      createdAt: nowIso,
      updatedAt: nowIso,
      selectedOfferId: selected.offerId,
      selectedProviderId: selected.providerId,
      offerSnapshotHash: selected.offerSnapshotHash,
      nativeCostCapGlm: maxCost,
      costReconciliation: "PENDING",
      events: [{ state: "ASSIGNED", at: nowIso, offerId: selected.offerId, providerId: selected.providerId }],
    };
    record.submitResponse = publicJob(record);
    if (!(await this.#jobStore.create(record))) {
      const raced = await this.#jobStore.findByIdempotencyKey(idempotencyKey);
      if (!raced) throw new ComputeBackendError("UNAVAILABLE", "durable job store reported duplicate without a record", { retryable: true });
      return validateIdempotentRetry(raced, digest);
    }
    const controller = new AbortController();
    this.#controllers.set(contractId, controller);
    this.#jobs.set(contractId, record);
    void this.#execute(request, parsed, selected, record, controller, timeoutMs, rentHours);
    return publicJob(record);
  }

  async #update(record, state, details = {}) {
    const at = new Date().toISOString();
    const updated = { ...record, ...details, state, updatedAt: at, events: [...record.events, { state, at, ...details }] };
    await this.#jobStore.update(updated);
    Object.assign(record, updated);
  }

  async #execute(request, parsed, selected, record, controller, timeoutMs, rentHours) {
    const { binding } = parsed;
    let rental;
    let rentalFinalized = false;
    let allocation;
    let timeoutSignal;
    try {
      timeoutSignal = AbortSignal.timeout(timeoutMs);
      const signal = AbortSignal.any([controller.signal, timeoutSignal]);
      const network = await this.#ensureConnected();
      const budget = allocationBudgetForCap(binding.nativeCostCapGlm);
      allocation = await network.payment.createAllocation({
        budget,
        expirationSec: Math.max(300, Math.ceil(timeoutMs / 1000) + 300),
        paymentPlatform: this.#config.paymentPlatform,
      });
      await this.#update(record, "RUNNING", { allocationId: allocation.id, agreementId: null });

      const requirements = binding.resourceRequirements;
      const rates = selected.ratesGlm;
      const maxStartPrice = Number(rates.start);
      const maxCpuPerHourPrice = Number(rates.cpuPerSecond) * 3600;
      const maxEnvPerHourPrice = Number(rates.environmentPerSecond) * 3600;
      const expectedCost = Number(selected.estimatedCostGlm);
      const order = {
        demand: {
          subnetTag: this.#config.subnetTag,
          expirationSec: Math.max(300, Math.ceil(timeoutMs / 1000) + 300),
          workload: {
            engine: binding.runtimeName,
            runtime: { name: binding.runtimeName, version: binding.runtimeVersion },
            capabilities: requirements.gpuRequired ? ["!exp:gpu"] : [],
            imageHash: binding.imageDigest.slice("sha3-224:".length),
            minMemGib: Math.ceil((requirements.ramBytes / GIB) * 100) / 100,
            minStorageGib: Math.ceil(((requirements.storageBytes ?? GIB) / GIB) * 100) / 100,
            minCpuThreads: requirements.cpuThreads ?? requirements.cpuCores,
            minCpuCores: requirements.cpuCores,
          },
        },
        market: {
          rentHours,
          pricing: { model: "linear", maxStartPrice, maxCpuPerHourPrice, maxEnvPerHourPrice },
          offerProposalFilter: (proposal) => {
            const properties = proposal.properties ?? {};
            const estimated = proposal.getEstimatedCost(rentHours);
            return proposal.provider?.id === selected.providerId
              && properties["golem.runtime.name"] === binding.runtimeName
              && properties["golem.runtime.version"] === binding.runtimeVersion
              && Number.isFinite(estimated)
              && estimated <= budget
              && estimated <= expectedCost + Number.EPSILON;
          },
        },
        payment: { allocation },
      };
      rental = await network.oneOf({ order, signalOrTimeout: signal });
      const actualProvider = rental.agreement?.provider?.id;
      if (actualProvider !== selected.providerId) {
        await rental.stopAndFinalize().catch(() => {});
        rentalFinalized = true;
        throw new ComputeBackendError("OFFER_CHANGED", "Golem negotiated an agreement with a provider other than the customer-selected provider");
      }
      await this.#update(record, "RUNNING", { agreementId: rental.agreement.id, providerId: actualProvider });
      const exe = await rental.getExeUnit(signal);
      assertSdkResult(await exe.uploadJson({ manifest: request.inputManifest, payload: request.privatePayload }, this.#config.imagePath, { signalOrTimeout: signal }), "private input upload");
      const runResult = await exe.run(this.#config.workerExecutable, this.#config.workerArgs, { signalOrTimeout: signal });
      assertSdkResult(runResult, "worker execution");
      const output = artifactFromDownload(await exe.downloadJson(this.#config.outputPath, { signalOrTimeout: signal }));
      const outputHash = artifactHash(output);
      await rental.stopAndFinalize();
      rentalFinalized = true;
      const allocationState = await network.payment.getAllocation(allocation.id);
      const spentAmount = allocationState.spentAmount;
      if (!capResultCost(spentAmount, binding.nativeCostCapGlm)) {
        await this.#update(record, "COST_CAP_EXCEEDED", { error: "Yagna-reported spend exceeded the signed native cost cap", actualCostGlm: String(spentAmount), costReconciliation: "FAILED" });
        return;
      }
      this.#artifacts.set(record.contractId, { output, outputHash });
      await this.#update(record, "SUCCEEDED", { outputHash, actualCostGlm: String(spentAmount), costReconciliation: "RECONCILED" });
    } catch (error) {
      let failure;
      if (controller.signal.aborted) {
        failure = new ComputeBackendError("CANCELLED", "Golem execution was cancelled", { retryable: true, cause: error });
      } else if (timeoutSignal?.aborted || error?.name === "TimeoutError") {
        failure = new ComputeBackendError("TIMEOUT", "Golem execution exceeded the signed deadline or timeout", { retryable: true, cause: error });
      } else if (error instanceof ComputeBackendError) {
        failure = error;
      } else {
        failure = classifyGolemError(error);
      }
      await this.#update(record, failure.code, { error: failure.message, retryable: failure.retryable, costReconciliation: allocation ? "PENDING" : "NOT_SPENT" }).catch(() => {});
    } finally {
      if (rental && !rentalFinalized) await rental.stopAndFinalize().catch(() => {});
      if (allocation) {
        const network = this.#network;
        if (network) {
          await network.payment.releaseAllocation(allocation).catch(() => {});
        }
      }
      this.#controllers.delete(record.contractId);
    }
  }

  async status(contractId) {
    const record = await this.#jobStore.get(contractId);
    if (!record) throw new ComputeBackendError("NOT_FOUND", `compute contract ${contractId} was not found`);
    if (["ASSIGNED", "RUNNING"].includes(record.state) && !this.#controllers.has(contractId)) {
      return publicJob({ ...record, recoveryRequired: true, error: "adapter restarted; reconcile the saved Yagna agreement before settlement" });
    }
    return publicJob(record);
  }

  async cancel(contractId, reason = "cancelled by customer") {
    const record = await this.#jobStore.get(contractId);
    if (!record) throw new ComputeBackendError("NOT_FOUND", `compute contract ${contractId} was not found`);
    if (["SUCCEEDED", "FAILED", "PROVIDER_FAILED", "TIMEOUT", "CANCELLED", "COST_CAP_EXCEEDED"].includes(record.state)) return publicJob(record);
    const controller = this.#controllers.get(contractId);
    if (!controller) throw new ComputeBackendError("UNAVAILABLE", "job is not attached to this adapter process; reconcile the stored Yagna agreement before cancellation", { retryable: true });
    controller.abort(new Error(reason));
    return this.status(contractId);
  }

  async fetchArtifacts(contractId) {
    const record = await this.#jobStore.get(contractId);
    if (!record) throw new ComputeBackendError("NOT_FOUND", `compute contract ${contractId} was not found`);
    const artifacts = this.#artifacts.get(contractId);
    if (!artifacts || record.state !== "SUCCEEDED") {
      throw new ComputeBackendError("ARTIFACT_MISSING", "private Golem output is unavailable from this requestor process");
    }
    return structuredClone(artifacts);
  }
}

export function validateGolemOfferBinding(binding, offer) {
  if (!IMAGE_DIGEST.test(binding.imageDigest ?? "")) return false;
  if (offer.providerId !== binding.selectedProviderId || offer.offerId !== binding.selectedOfferId) return false;
  return offer.offerSnapshotHash === binding.offerSnapshotHash;
}
