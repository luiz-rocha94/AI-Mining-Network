import { createHash } from "node:crypto";
import { mkdir, readFile, unlink, writeFile } from "node:fs/promises";
import path from "node:path";

function storageKey(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

export class FileReplayStore {
  constructor(directory) {
    this.directory = directory;
  }

  async #claim(key, expiresAt, now) {
    const file = path.join(this.directory, `${storageKey(key)}.json`);
    try {
      await writeFile(file, JSON.stringify({ expiresAt }), { flag: "wx", mode: 0o600 });
      return true;
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      try {
        const previous = JSON.parse(await readFile(file, "utf8"));
        if (previous.expiresAt <= now) {
          try {
            await unlink(file);
          } catch (unlinkError) {
            if (unlinkError.code !== "ENOENT") throw unlinkError;
          }
          return this.#claim(key, expiresAt, now);
        }
      } catch (readError) {
        if (readError.code !== "ENOENT") throw readError;
        return this.#claim(key, expiresAt, now);
      }
      return false;
    }
  }

  async consume(envelope, chainId, now) {
    await mkdir(this.directory, { recursive: true });
    const expiresAt = Date.parse(envelope.expiresAt);
    const nonceClaimed = await this.#claim(
      `${chainId}:nonce:${envelope.signer.toLowerCase()}:${envelope.nonce.toLowerCase()}`,
      expiresAt,
      now,
    );
    if (!nonceClaimed) return false;
    const idClaimed = await this.#claim(`${chainId}:id:${envelope.id}`, expiresAt, now);
    return idClaimed;
  }
}
