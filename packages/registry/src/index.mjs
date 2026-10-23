import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import Ajv2020 from "ajv/dist/2020.js";
import { createOrbitDB, IPFSAccessController } from "@orbitdb/core";
import {
  MemoryReplayStore,
  canonicalJsonString,
  validateEnvelope,
} from "@ai-mining-network/protocol";

const DATABASE_NAME = "amn-public-skill-registry-v1";
const PUBLIC_KINDS = new Set([
  "SkillManifest",
  "SkillBenchmark",
  "SkillRating",
  "SkillRevocation",
  "PublicAuditCheckpoint",
]);
const PRIVATE_OR_UNSUPPORTED_KINDS = new Set([
  "CustomerTask",
  "FixedPriceQuote",
  "ExecutionContract",
  "ExecutionCommitment",
  "VerificationAssignment",
  "VerificationReceipt",
  "VerificationDecision",
  "PaymentSettlement",
]);
const MAX_RECORD_BYTES = 256 * 1024;
const MESSAGE_SCHEMA = JSON.parse(readFileSync(new URL("../../protocol/v2/messages.schema.json", import.meta.url), "utf8"));
const REGISTRY_SCHEMA = JSON.parse(readFileSync(new URL("../schemas/public-records.schema.json", import.meta.url), "utf8"));
// The protocol's generated SkillManifest schema refers to this nested model
// from the document root. Add the expected root alias in the validator copy.
const REGISTRY_MESSAGE_SCHEMA = structuredClone(MESSAGE_SCHEMA);
REGISTRY_MESSAGE_SCHEMA.$defs.ResourceRequirements = structuredClone(MESSAGE_SCHEMA.$defs.SkillManifest.$defs.ResourceRequirements);
const ajv = new Ajv2020({ allErrors: true, strict: false });
ajv.addSchema(REGISTRY_MESSAGE_SCHEMA);
ajv.addSchema(REGISTRY_SCHEMA);
const validatePayload = new Map([
  ["SkillManifest", ajv.compile({ $ref: `${REGISTRY_MESSAGE_SCHEMA.$id}#/$defs/SkillManifest` })],
  ["SkillBenchmark", ajv.compile({ $ref: `${MESSAGE_SCHEMA.$id}#/$defs/SkillBenchmark` })],
  ["SkillRating", ajv.compile({ $ref: `${MESSAGE_SCHEMA.$id}#/$defs/SkillRating` })],
  ["SkillRevocation", ajv.compile({ $ref: `${REGISTRY_SCHEMA.$id}#/$defs/SkillRevocation` })],
  ["PublicAuditCheckpoint", ajv.compile({ $ref: `${REGISTRY_SCHEMA.$id}#/$defs/PublicAuditCheckpoint` })],
]);

export class RegistryError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "RegistryError";
    this.code = code;
  }
}

/**
 * OrbitDB-backed immutable public metadata view. Only signed public metadata
 * payloads are accepted; task payloads and private artifacts use other stores.
 * OrbitDB and the supplied HeliaArtifactStore retain their own lifecycle.
 */
export class OrbitDbPublicMetadataStore {
  #artifactStore;
  #orbitdb;
  #db;
  #identityResolver;
  #authorizeRecord;
  #chainId;
  #replayStore = new MemoryReplayStore();
  #records = [];
  #invalidRecords = [];
  #appendTail = Promise.resolve();
  #listeners = new Set();
  #updateHandler;

  static async create({
    artifactStore,
    directory,
    databaseAddress,
    databaseName = DATABASE_NAME,
    identityId,
    chainId = 31337,
    identityResolver,
    authorizeRecord = ({ kind }) => kind !== "PublicAuditCheckpoint",
  } = {}) {
    if (!artifactStore || typeof artifactStore.getHeliaInstance !== "function") {
      throw new TypeError("an open HeliaArtifactStore is required");
    }
    if (typeof directory !== "string" || directory.length === 0) {
      throw new TypeError("a persistent OrbitDB directory is required");
    }
    if (typeof identityResolver !== "function") {
      throw new TypeError("identityResolver is required; registry writes must bind to AMN identities");
    }
    if (typeof authorizeRecord !== "function") throw new TypeError("authorizeRecord must be a function");
    if (!Number.isSafeInteger(chainId) || chainId <= 0) throw new TypeError("chainId must be a positive safe integer");

    const ipfs = artifactStore.getHeliaInstance();
    const orbitdb = await createOrbitDB({
      ipfs,
      id: identityId ?? path.basename(path.resolve(directory)),
      directory: path.resolve(directory),
    });
    try {
      const db = await orbitdb.open(databaseAddress ?? databaseName, databaseAddress
        ? {}
        : {
            type: "events",
            meta: { description: "Public signed AI Mining Network skill metadata; no private task data" },
            AccessController: IPFSAccessController({ write: ["*"] }),
          });
      const store = new OrbitDbPublicMetadataStore({
        artifactStore,
        orbitdb,
        db,
        identityResolver,
        authorizeRecord,
        chainId,
      });
      await store.refresh();
      store.#watchUpdates();
      return store;
    } catch (error) {
      await orbitdb.stop().catch(() => {});
      throw error;
    }
  }

  constructor(options) {
    this.#artifactStore = options.artifactStore;
    this.#orbitdb = options.orbitdb;
    this.#db = options.db;
    this.#identityResolver = options.identityResolver;
    this.#authorizeRecord = options.authorizeRecord;
    this.#chainId = options.chainId;
  }

  get address() { return this.#db.address; }

  get peerId() { return this.#artifactStore.peerId; }

  async append(envelope) {
    const run = this.#appendTail.then(() => this.#append(envelope));
    this.#appendTail = run.catch(() => {});
    return run;
  }

  async #append(envelope) {
    await this.refresh();
    const serialized = serializePublicEnvelope(envelope);
    const duplicate = this.#records.find((record) => record.envelope.id === envelope?.id);
    if (duplicate) {
      if (duplicate.canonical !== serialized) {
        throw new RegistryError("MESSAGE_ID_CONFLICT", "a different signed public record already uses this message ID");
      }
      return { recordId: duplicate.envelope.id, orbitEntryHash: duplicate.orbitEntryHash, status: "duplicate" };
    }
    await this.#validateAndAuthorize(envelope, { replayStore: this.#replayStore, historical: false });
    if (envelope.payload.kind === "SkillRevocation") {
      const owner = this.#records.find(({ envelope: known }) => known.payload.kind === "SkillManifest"
        && known.payload.contentHash === envelope.payload.contentHash)?.envelope.principalId;
      if (!owner || owner !== envelope.principalId) {
        throw new RegistryError("UNAUTHORIZED_REVOCATION", "only the manifest author can revoke an already published skill version");
      }
    }
    const orbitEntryHash = await this.#db.add(serialized);
    await this.refresh();
    const accepted = this.#records.find((record) => record.envelope.id === envelope.id);
    if (!accepted || accepted.canonical !== serialized) {
      throw new RegistryError("CONFLICT", "record was appended but a concurrent signed record won deterministic ID conflict resolution");
    }
    return {
      recordId: envelope.id,
      orbitEntryHash,
      status: "accepted",
      replication: this.#replicationStatus(),
    };
  }

  async get(recordId) {
    await this.refresh();
    return this.#records.find((record) => record.envelope.id === recordId)?.envelope ?? null;
  }

  async query({ kind, skillHash, name, principalId, includeRevoked = true } = {}) {
    await this.refresh();
    const revoked = this.#revokedSkillVersions();
    return this.#records
      .filter(({ envelope }) => {
        const payload = envelope.payload;
        if (kind && payload.kind !== kind) return false;
        if (skillHash && payload.skillHash !== skillHash && payload.contentHash !== skillHash) return false;
        if (name && payload.name !== name) return false;
        if (principalId && envelope.principalId !== principalId) return false;
        if (!includeRevoked && payload.kind === "SkillManifest" && revoked.has(`${envelope.principalId}:${payload.contentHash}`)) return false;
        return true;
      })
      .map(({ envelope }) => envelope);
  }

  /** Deterministic, author/version-aware view for search indexes. */
  async discoverSkills({ name, includeRevoked = false } = {}) {
    await this.refresh();
    const revoked = this.#revokedSkillVersions();
    return this.#records
      .filter(({ envelope }) => envelope.payload.kind === "SkillManifest")
      .filter(({ envelope }) => !name || envelope.payload.name === name)
      .filter(({ envelope }) => includeRevoked || !revoked.has(`${envelope.principalId}:${envelope.payload.contentHash}`))
      .map(({ envelope }) => envelope)
      .sort(compareSkillRecords);
  }

  async conflicts() {
    await this.refresh();
    const groups = new Map();
    for (const { envelope } of this.#records) {
      const p = envelope.payload;
      let key;
      if (p.kind === "SkillManifest") key = `manifest:${envelope.principalId}:${p.name}:${p.version}`;
      else if (p.kind === "SkillRating") key = `rating:${envelope.principalId}:${p.ratingId}`;
      else if (p.kind === "SkillRevocation") key = `revocation:${envelope.principalId}:${p.revocationId}`;
      else if (p.kind === "PublicAuditCheckpoint") key = `checkpoint:${envelope.principalId}:${p.checkpointId}`;
      else continue;
      const list = groups.get(key) ?? [];
      list.push(envelope);
      groups.set(key, list);
    }
    return [...groups.entries()]
      .filter(([, records]) => records.length > 1)
      .map(([key, records]) => ({
        key,
        resolution: "all immutable claims retained; lexical canonical envelope order is stable",
        records: records.sort((a, b) => a.payloadHash.localeCompare(b.payloadHash)
          || canonicalJsonString(a).localeCompare(canonicalJsonString(b))).map((record) => record.id),
      }))
      .sort((a, b) => a.key.localeCompare(b.key));
  }

  async refresh() {
    const rawValues = await this.#db.all();
    const parsed = [];
    const invalid = [];
    for (const entry of rawValues) {
      try {
        const value = entry && typeof entry === "object" && Object.hasOwn(entry, "value") ? entry.value : entry;
        const envelope = typeof value === "string" ? JSON.parse(value) : value;
        const canonical = serializePublicEnvelope(envelope);
        parsed.push({ envelope, canonical, orbitEntryHash: entry?.hash });
      } catch (error) {
        invalid.push({ code: error.code ?? "INVALID_RECORD", message: String(error.message).slice(0, 256) });
      }
    }
    parsed.sort(compareCanonicalRecords);
    const replayStore = new MemoryReplayStore();
    const valid = [];
    const byMessageId = new Map();
    for (const record of parsed) {
      try {
        await this.#validateAndAuthorize(record.envelope, { replayStore, historical: true });
        const previous = byMessageId.get(record.envelope.id);
        if (previous) continue;
        byMessageId.set(record.envelope.id, record);
        valid.push(record);
      } catch (error) {
        invalid.push({ recordId: record.envelope.id, code: error.code ?? "INVALID_RECORD", message: String(error.message).slice(0, 256) });
      }
    }
    this.#records = valid.sort((a, b) => a.envelope.id.localeCompare(b.envelope.id));
    this.#invalidRecords = invalid;
    this.#replayStore = replayStore;
    return this.#records.map(({ envelope }) => envelope);
  }

  subscribe(listener) {
    if (typeof listener !== "function") throw new TypeError("listener must be a function");
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  async status() {
    await this.refresh();
    return {
      databaseAddress: this.#db.address,
      peerId: this.peerId,
      connectedPeers: this.#artifactStore.getHeliaInstance().libp2p.getPeers().length,
      validRecordCount: this.#records.length,
      rejectedRecordCount: this.#invalidRecords.length,
      rejectedRecords: structuredClone(this.#invalidRecords),
      replication: this.#replicationStatus(),
      consistency: "eventual; records are immutable signed claims, not truth",
      privateTaskData: "not accepted by this adapter",
    };
  }

  async close() {
    if (this.#updateHandler) this.#db.events.off("update", this.#updateHandler);
    this.#listeners.clear();
    await this.#db.close();
    await this.#orbitdb.stop();
  }

  async #validateAndAuthorize(envelope, { replayStore, historical }) {
    const payload = envelope?.payload;
    if (!payload || !PUBLIC_KINDS.has(payload.kind)) {
      const code = PRIVATE_OR_UNSUPPORTED_KINDS.has(payload?.kind) ? "PRIVATE_RECORD_FORBIDDEN" : "UNSUPPORTED_PUBLIC_KIND";
      throw new RegistryError(code, "only the documented signed public metadata kinds are accepted");
    }
    const validate = validatePayload.get(payload.kind);
    if (!validate(payload)) {
      throw new RegistryError("INVALID_PUBLIC_SCHEMA", `${payload.kind} failed public metadata schema validation: ${ajv.errorsText(validate.errors).slice(0, 512)}`);
    }
    if (Buffer.byteLength(canonicalJsonString(envelope), "utf8") > MAX_RECORD_BYTES) {
      throw new RegistryError("RECORD_TOO_LARGE", `public records are limited to ${MAX_RECORD_BYTES} bytes`);
    }
    const now = historical ? parseTimestamp(envelope.createdAt) + 1 : Date.now();
    await validateEnvelope(envelope, {
      chainId: this.#chainId,
      now,
      identityResolver: (principalId) => this.#identityResolver(principalId, envelope),
      replayStore,
    });
    const authorized = await this.#authorizeRecord({ envelope, kind: payload.kind, historical });
    if (authorized !== true) throw new RegistryError("UNAUTHORIZED_RECORD", "local public-record authorization policy rejected the signer or record");
  }

  #revokedSkillVersions() {
    const manifests = this.#records.filter(({ envelope }) => envelope.payload.kind === "SkillManifest");
    const revocations = this.#records.filter(({ envelope }) => envelope.payload.kind === "SkillRevocation");
    const owners = new Set(manifests.map(({ envelope }) => `${envelope.principalId}:${envelope.payload.contentHash}`));
    return new Set(revocations
      .filter(({ envelope }) => owners.has(`${envelope.principalId}:${envelope.payload.contentHash}`))
      .map(({ envelope }) => `${envelope.principalId}:${envelope.payload.contentHash}`));
  }

  #replicationStatus() {
    return this.#artifactStore.getHeliaInstance().libp2p.getPeers().length > 0
      ? "replication-pending-or-connected"
      : "local-only-until-peer-connects";
  }

  #watchUpdates() {
    this.#updateHandler = () => {
      void this.refresh().then(() => {
        for (const listener of this.#listeners) {
          try { listener({ databaseAddress: this.#db.address, recordCount: this.#records.length }); } catch { /* isolate subscriber errors */ }
        }
      }).catch(() => {});
    };
    this.#db.events.on("update", this.#updateHandler);
  }
}

function serializePublicEnvelope(envelope) {
  try {
    if (!envelope || typeof envelope !== "object" || Array.isArray(envelope)) throw new Error("envelope must be an object");
    if (typeof envelope.id !== "string" || envelope.id.length < 1 || envelope.id.length > 128) throw new Error("envelope id is invalid");
    if (typeof envelope.principalId !== "string" || envelope.principalId.length < 1 || envelope.principalId.length > 128) throw new Error("principalId is invalid");
    return canonicalJsonString(envelope);
  } catch (error) {
    if (error instanceof RegistryError) throw error;
    throw new RegistryError("INVALID_ENVELOPE", error.message);
  }
}

function parseTimestamp(value) {
  if (typeof value !== "string" || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,3})?Z$/.test(value)) {
    throw new RegistryError("INVALID_TIMESTAMP", "signed creation time must be millisecond UTC RFC3339");
  }
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) throw new RegistryError("INVALID_TIMESTAMP", "signed creation time is invalid");
  return parsed;
}

function compareCanonicalRecords(a, b) {
  const ah = a.envelope.payloadHash ?? "";
  const bh = b.envelope.payloadHash ?? "";
  return ah.localeCompare(bh) || a.canonical.localeCompare(b.canonical);
}

function compareSkillRecords(a, b) {
  const left = a.payload;
  const right = b.payload;
  return left.name.localeCompare(right.name)
    || left.version.localeCompare(right.version)
    || a.principalId.localeCompare(b.principalId)
    || left.contentHash.localeCompare(right.contentHash)
    || a.id.localeCompare(b.id);
}

export function publicRecordDigest(envelope) {
  return `sha256:${createHash("sha256").update(serializePublicEnvelope(envelope), "utf8").digest("hex")}`;
}

export const PUBLIC_METADATA_KINDS = Object.freeze([...PUBLIC_KINDS]);
