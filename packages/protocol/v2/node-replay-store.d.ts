import type { SignedEnvelope } from "./index.js";
export class FileReplayStore {
  readonly durable: true;
  constructor(directory: string);
  consume(envelope: SignedEnvelope, chainId: number, now: number): Promise<boolean>;
}
