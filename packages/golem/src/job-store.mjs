import { createHash, randomUUID } from "node:crypto";
import { mkdir, open, readFile, readdir, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";

function keyFile(key) {
  return `${createHash("sha256").update(key).digest("hex")}.json`;
}

/** Process-local store for tests and explicitly ephemeral development runs. */
export class MemoryJobStore {
  durable = false;
  #byId = new Map();
  #byKey = new Map();

  async findByIdempotencyKey(key) {
    const id = this.#byKey.get(key);
    return id ? structuredClone(this.#byId.get(id)) : undefined;
  }

  async get(contractId) {
    const record = this.#byId.get(contractId);
    return record ? structuredClone(record) : undefined;
  }

  async create(record) {
    if (this.#byKey.has(record.idempotencyKey) || this.#byId.has(record.contractId)) return false;
    this.#byId.set(record.contractId, structuredClone(record));
    this.#byKey.set(record.idempotencyKey, record.contractId);
    return true;
  }

  async update(record) {
    if (!this.#byId.has(record.contractId)) throw new Error("cannot update an unknown compute job");
    this.#byId.set(record.contractId, structuredClone(record));
  }
}

/** Local durable journal. It stores public lifecycle data only, never task inputs or outputs. */
export class FileJobStore {
  durable = true;

  constructor(directory) {
    if (!directory) throw new TypeError("FileJobStore requires a directory");
    this.directory = directory;
  }

  async #ensureDirectory() {
    await mkdir(this.directory, { recursive: true, mode: 0o700 });
  }

  async #read(path) {
    try {
      return JSON.parse(await readFile(path, "utf8"));
    } catch (error) {
      if (error?.code === "ENOENT") return undefined;
      throw error;
    }
  }

  async findByIdempotencyKey(key) {
    await this.#ensureDirectory();
    return this.#read(join(this.directory, keyFile(key)));
  }

  async get(contractId) {
    await this.#ensureDirectory();
    for (const file of await readdir(this.directory)) {
      if (!file.endsWith(".json")) continue;
      const record = await this.#read(join(this.directory, file));
      if (record?.contractId === contractId) return record;
    }
    return undefined;
  }

  async create(record) {
    await this.#ensureDirectory();
    const path = join(this.directory, keyFile(record.idempotencyKey));
    let handle;
    try {
      handle = await open(path, "wx", 0o600);
      await handle.writeFile(`${JSON.stringify(record)}\n`, "utf8");
      await handle.sync();
      return true;
    } catch (error) {
      if (error?.code === "EEXIST") return false;
      throw error;
    } finally {
      await handle?.close();
    }
  }

  async update(record) {
    await this.#ensureDirectory();
    const path = join(this.directory, keyFile(record.idempotencyKey));
    const temporary = `${path}.${randomUUID()}.tmp`;
    await writeFile(temporary, `${JSON.stringify(record)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
    try {
      await rename(temporary, path);
    } catch (error) {
      await rm(temporary, { force: true });
      throw error;
    }
  }
}
