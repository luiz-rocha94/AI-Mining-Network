import { randomUUID } from "node:crypto";
import { copyFile, mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import { canonicalJsonString } from "@ai-mining-network/protocol";

const MAX_SCORED_PEERS = 10_000;
const MAX_SCORE_EVENTS = 64;
const MAX_SCORE = 100;
const MIN_SCORE = -100;

function eventKey(envelope) {
  const match = /^(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?Z$/.exec(envelope.createdAt);
  const timestamp = match == null ? envelope.createdAt : `${match[1]}.${(match[2] ?? "").padEnd(6, "0").slice(0, 6)}Z`;
  return `${timestamp}\u0000${envelope.signer.toLowerCase()}\u0000${envelope.id}`;
}

async function atomicWrite(filePath, contents) {
  await mkdir(path.dirname(filePath), { recursive: true });
  const temporary = `${filePath}.${process.pid}.${randomUUID()}.tmp`;
  try {
    await writeFile(temporary, contents, { flag: "wx", mode: 0o600 });
    try {
      await rename(temporary, filePath);
    } catch (error) {
      if (!new Set(["EPERM", "EEXIST", "ENOTSUP"]).has(error.code)) throw error;
      // Windows sandboxed filesystems may deny rename even when the temporary
      // file and destination share a volume. Preserve the complete staged
      // content and use the platform's replace-capable copy operation.
      await copyFile(temporary, filePath);
    }
  } finally {
    try {
      await unlink(temporary);
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
  }
}

export class PublicEventStore {
  #records = new Map();
  #loaded = false;
  #queue = Promise.resolve();

  constructor(filePath, { maxEvents = 10_000 } = {}) {
    if (!Number.isSafeInteger(maxEvents) || maxEvents < 1 || maxEvents > 1_000_000) {
      throw new RangeError("maxEvents must be an integer from 1 to 1000000");
    }
    this.filePath = filePath;
    this.maxEvents = maxEvents;
  }

  async load() {
    if (this.#loaded) return;
    await this.#serial(async () => {
      if (this.#loaded) return;
      try {
        const parsed = JSON.parse(await readFile(this.filePath, "utf8"));
        if (parsed.version !== 1 || !Array.isArray(parsed.events)) {
          throw new Error("unsupported public event store format");
        }
        for (const envelope of parsed.events.slice(-this.maxEvents)) {
          if (typeof envelope?.id === "string") this.#records.set(envelope.id, envelope);
        }
      } catch (error) {
        if (error.code !== "ENOENT") throw error;
      }
      this.#loaded = true;
    });
  }

  async append(envelope) {
    await this.load();
    return this.#serial(async () => {
      const existing = this.#records.get(envelope.id);
      if (existing != null) {
        if (canonicalJsonString(existing) !== canonicalJsonString(envelope)) {
          const error = new Error("public message ID was reused for a different signed body");
          error.code = "MESSAGE_ID_CONFLICT";
          throw error;
        }
        return false;
      }
      this.#records.set(envelope.id, envelope);
      const ordered = this.#ordered();
      while (ordered.length > this.maxEvents) {
        const removed = ordered.shift();
        this.#records.delete(removed.id);
      }
      await this.#persist();
      return true;
    });
  }

  async has(id) {
    await this.load();
    return this.#records.has(id);
  }

  async get(id) {
    await this.load();
    const record = this.#records.get(id);
    return record == null ? undefined : structuredClone(record);
  }

  async ids() {
    await this.load();
    return this.#ordered().map((envelope) => envelope.id);
  }

  async snapshot() {
    await this.load();
    return this.#ordered().map((envelope) => structuredClone(envelope));
  }

  async page(afterKey = "", maxBytes = 192 * 1024, maxEvents = 64) {
    const all = await this.snapshot();
    const remaining = all.filter((envelope) => eventKey(envelope) > afterKey);
    const events = [];
    let used = 128;
    for (const envelope of remaining.slice(0, maxEvents)) {
      const size = Buffer.byteLength(JSON.stringify(envelope), "utf8") + 1;
      if (used + size > maxBytes) {
        if (events.length === 0) throw new Error("single public event exceeds sync page limit");
        break;
      }
      events.push(envelope);
      used += size;
    }
    const nextKey = events.length === 0 ? afterKey : eventKey(events.at(-1));
    return {
      events,
      nextKey,
      done: nextKey === (remaining.length === 0 ? afterKey : eventKey(remaining.at(-1))),
    };
  }

  #ordered() {
    return [...this.#records.values()].sort((left, right) => eventKey(left).localeCompare(eventKey(right)));
  }

  async #persist() {
    await atomicWrite(this.filePath, canonicalJsonString({ version: 1, events: this.#ordered() }));
  }

  async #serial(action) {
    const operation = this.#queue.then(action);
    this.#queue = operation.catch(() => {});
    return operation;
  }
}

export class PeerScoreStore {
  #peers = new Map();
  #loaded = false;
  #queue = Promise.resolve();

  constructor(filePath) {
    this.filePath = filePath;
  }

  async load() {
    if (this.#loaded) return;
    await this.#serial(async () => {
      if (this.#loaded) return;
      try {
        const parsed = JSON.parse(await readFile(this.filePath, "utf8"));
        if (parsed.version !== 1 || !Array.isArray(parsed.peers)) {
          throw new Error("unsupported peer score store format");
        }
        for (const item of parsed.peers.slice(-MAX_SCORED_PEERS)) {
          if (typeof item?.peerId === "string" && Number.isFinite(item.score)) {
            this.#peers.set(item.peerId, {
              score: Math.max(MIN_SCORE, Math.min(MAX_SCORE, Math.trunc(item.score))),
              events: Array.isArray(item.events) ? item.events.slice(-MAX_SCORE_EVENTS) : [],
            });
          }
        }
      } catch (error) {
        if (error.code !== "ENOENT") throw error;
      }
      this.#loaded = true;
    });
  }

  async record(peerId, delta, reason, at = Date.now()) {
    await this.load();
    return this.#serial(async () => {
      const current = this.#peers.get(peerId) ?? { score: 0, events: [] };
      const next = {
        score: Math.max(MIN_SCORE, Math.min(MAX_SCORE, current.score + Math.trunc(delta))),
        events: [...current.events, { at, delta: Math.trunc(delta), reason }].slice(-MAX_SCORE_EVENTS),
      };
      this.#peers.delete(peerId);
      this.#peers.set(peerId, next);
      while (this.#peers.size > MAX_SCORED_PEERS) {
        this.#peers.delete(this.#peers.keys().next().value);
      }
      await this.#persist();
      return structuredClone(next);
    });
  }

  async get(peerId) {
    await this.load();
    const value = this.#peers.get(peerId) ?? { score: 0, events: [] };
    return structuredClone(value);
  }

  async snapshot() {
    await this.load();
    return [...this.#peers.entries()].map(([peerId, value]) => ({ peerId, ...structuredClone(value) }));
  }

  async #persist() {
    const peers = [...this.#peers.entries()].map(([peerId, value]) => ({ peerId, ...value }));
    await atomicWrite(this.filePath, canonicalJsonString({ version: 1, peers }));
  }

  async #serial(action) {
    const operation = this.#queue.then(action);
    this.#queue = operation.catch(() => {});
    return operation;
  }
}

export class PeerRateLimiter {
  #windows = new Map();

  constructor({ windowMs = 60_000, maxMessages = 60, maxBytes = 1_048_576, maxPeers = 10_000 } = {}) {
    if (![windowMs, maxMessages, maxBytes, maxPeers].every(Number.isSafeInteger)
      || windowMs < 1 || maxMessages < 1 || maxBytes < 1 || maxPeers < 1) {
      throw new RangeError("rate limit options must be positive safe integers");
    }
    this.windowMs = windowMs;
    this.maxMessages = maxMessages;
    this.maxBytes = maxBytes;
    this.maxPeers = maxPeers;
  }

  consume(peerId, byteLength, now = Date.now()) {
    if (!Number.isSafeInteger(byteLength) || byteLength < 0 || !Number.isSafeInteger(now)) return false;
    let state = this.#windows.get(peerId);
    if (!state || now - state.startAt >= this.windowMs) {
      state = { startAt: now, messages: 0, bytes: 0 };
    }
    if (state.messages + 1 > this.maxMessages || state.bytes + byteLength > this.maxBytes) {
      this.#windows.set(peerId, state);
      return false;
    }
    state.messages += 1;
    state.bytes += byteLength;
    this.#windows.delete(peerId);
    this.#windows.set(peerId, state);
    while (this.#windows.size > this.maxPeers) this.#windows.delete(this.#windows.keys().next().value);
    return true;
  }
}

export { eventKey as publicEventKey };
