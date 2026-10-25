export const BACKEND_ERROR_CODES = Object.freeze([
  "UNAVAILABLE",
  "INVALID_CONTRACT",
  "REPLAY",
  "IDEMPOTENCY_CONFLICT",
  "OFFER_EXPIRED",
  "OFFER_CHANGED",
  "COST_CAP_EXCEEDED",
  "CANCELLED",
  "TIMEOUT",
  "PROVIDER_FAILED",
  "ARTIFACT_MISSING",
  "NOT_FOUND",
]);

export class ComputeBackendError extends Error {
  constructor(code, message, { retryable = false, details = undefined, cause = undefined } = {}) {
    super(message, { cause });
    this.name = "ComputeBackendError";
    this.code = code;
    this.retryable = retryable;
    if (details !== undefined) this.details = details;
  }
}

export function classifyGolemError(error) {
  const message = error instanceof Error ? error.message : String(error);
  const lower = message.toLowerCase();
  if (error?.name === "TimeoutError" || lower.includes("timeout") || lower.includes("timed out")) {
    return new ComputeBackendError("TIMEOUT", message, { retryable: true, cause: error });
  }
  if (lower.includes("offer") && (lower.includes("expired") || lower.includes("not found"))) {
    return new ComputeBackendError("OFFER_EXPIRED", message, { retryable: true, cause: error });
  }
  if (lower.includes("allocation") && (lower.includes("budget") || lower.includes("insufficient"))) {
    return new ComputeBackendError("COST_CAP_EXCEEDED", message, { cause: error });
  }
  return new ComputeBackendError("PROVIDER_FAILED", message, { retryable: true, cause: error });
}
