import type { SignedEnvelope } from "./index.js";
export class FileReplayStore {
  constructor(directory: string);
  consume(envelope: SignedEnvelope, chainId: number, now: number): Promise<boolean>;
}
