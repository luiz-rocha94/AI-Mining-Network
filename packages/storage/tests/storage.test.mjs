import assert from "node:assert/strict";
import { mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { HeliaArtifactStore, createRecipientKeyPair } from "../src/index.mjs";

const listen = ["/ip4/127.0.0.1/tcp/0"];
const publicAuthorization = async ({ authorization }) => authorization?.approved === true;

test("public artifacts require approval, are pinned, and verify expected CID and hash", async (t) => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "amn-storage-public-"));
  const store = await makeStore(dir).open();
  t.after(async () => { await store.close(); await rm(dir, { recursive: true, force: true }); });

  await assert.rejects(
    store.putPublic("unapproved", { kind: "skill-package", authorization: { approved: false } }),
    /not explicitly authorized/,
  );
  const body = Buffer.from('{"name":"safe-skill","version":"1.0.0"}');
  const published = await store.putPublic(body, {
    kind: "skill-package",
    authorization: { approved: true },
  });
  assert.equal(published.visibility, "public");
  assert.equal((await store.getStatus(published.cid)).availability, "local-pinned");
  assert.deepEqual(await store.get({
    cid: published.cid,
    expectedCid: published.cid,
    expectedSha256: published.sha256,
  }), body);
  await assert.rejects(store.get({
    cid: published.cid,
    expectedCid: published.cid,
    expectedSha256: "0".repeat(64),
  }), /SHA-256/);
  await assert.rejects(store.get({
    cid: published.cid,
    expectedCid: "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3o4z7z4p5h6bqg3j5s5e5f5oi",
    expectedSha256: published.sha256,
  }), /CID does not match/);
  await assert.rejects(
    store.putPublic(body, { kind: "customer-input", authorization: { approved: true } }),
    /kind is not allowed/,
  );
});

test("private artifacts are encrypted at rest and grants enforce assignment, expiry, and revocation", async (t) => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "amn-storage-private-"));
  const policy = async ({ action, principalId }) => {
    if (action === "issue" || action === "read" || action === "revoke") return principalId === "validator-7";
    return false;
  };
  const store = await makeStore(dir, { privateGrantPolicy: policy }).open();
  const stores = [store];
  t.after(async () => {
    for (const instance of stores) await instance.close();
    await rm(dir, { recursive: true, force: true });
  });

  const recipient = createRecipientKeyPair();
  const outsider = createRecipientKeyPair();
  const privateMarkers = [
    "private task prompt marker",
    "source document marker",
    "ground truth marker",
    "plaintext output marker",
  ];
  const secret = Buffer.from(JSON.stringify({
    prompt: privateMarkers[0],
    sourceDocument: privateMarkers[1],
    groundTruth: privateMarkers[2],
    result: privateMarkers[3],
  }));
  const result = await store.putPrivate(secret, {
    taskId: "task-private-42",
    recipients: [{ principalId: "validator-7", publicKey: recipient.publicKey }],
  });
  const grant = result.grants[0];
  assert.equal(result.visibility, "encrypted-private");
  assert.deepEqual(await store.getPrivate({
    envelope: grant,
    recipientPrincipalId: "validator-7",
    recipientPrivateKey: recipient.privateKey,
  }), secret);
  await assert.rejects(store.getPrivate({
    envelope: grant,
    recipientPrincipalId: "outsider",
    recipientPrivateKey: outsider.privateKey,
  }), /assigned to another recipient/);
  await assert.rejects(store.getPrivate({
    envelope: grant,
    recipientPrincipalId: "validator-7",
    recipientPrivateKey: outsider.privateKey,
  }));
  await assert.rejects(store.getPrivate({
    envelope: { ...grant, expiresAt: new Date(Date.now() - 1_000).toISOString() },
    recipientPrincipalId: "validator-7",
    recipientPrivateKey: recipient.privateKey,
  }), /expired/);
  await assert.rejects(store.putPrivate(secret, {
    taskId: "task-private-43",
    recipients: [{ principalId: "outsider", publicKey: outsider.publicKey }],
  }), /not authorized/);

  const files = await allFiles(path.join(dir, "blocks"));
  const storedContent = await Promise.all(files.map((file) => readFile(file)));
  for (const marker of privateMarkers) {
    assert.equal(storedContent.some((content) => content.includes(marker)), false, `${marker} must not reach the Helia blockstore`);
    assert.equal(JSON.stringify(result).includes(marker), false, `${marker} must not appear in the public grant metadata`);
  }

  await store.revokeGrant(grant.grantId);
  await assert.rejects(store.getPrivate({
    envelope: grant,
    recipientPrincipalId: "validator-7",
    recipientPrivateKey: recipient.privateKey,
  }), /revoked/);
  await store.close();
  const restarted = await makeStore(dir, { privateGrantPolicy: policy }).open();
  stores.push(restarted);
  await assert.rejects(restarted.getPrivate({
    envelope: grant,
    recipientPrincipalId: "validator-7",
    recipientPrivateKey: recipient.privateKey,
  }), /revoked/);
});

test("two independent persistent Helia peers retrieve and retain the same artifact across restart", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "amn-storage-peers-"));
  const aDir = path.join(root, "peer-a");
  const bDir = path.join(root, "peer-b");
  let a = await makeStore(aDir).open();
  const stores = [a];
  t.after(async () => {
    for (const instance of stores) await instance.close();
    await rm(root, { recursive: true, force: true });
  });
  const originalPeerId = a.peerId;
  const data = Buffer.from("independent peer payload after restart");
  const published = await a.putPublic(data, { kind: "benchmark", authorization: { approved: true } });
  await a.close();
  a = await makeStore(aDir).open();
  stores.push(a);
  assert.equal(a.peerId, originalPeerId, "persistent datastore should preserve peer identity");
  const b = await makeStore(bDir).open();
  stores.push(b);
  const originalPeerIdB = b.peerId;

  await b.connect(a.multiaddrs[0]);
  assert.deepEqual(await a.announce(published.cid), {
    cid: published.cid,
    status: "failed; pinned locally",
  }, "without configured bootstrap peers, local pinning must not claim global announcement");
  const received = await b.get({
    cid: published.cid,
    expectedCid: published.cid,
    expectedSha256: published.sha256,
    timeoutMs: 20_000,
  });
  assert.deepEqual(received, data);
  await b.pin({ cid: published.cid, expectedCid: published.cid, expectedSha256: published.sha256 });
  await b.close();
  const bRestarted = await makeStore(bDir).open();
  stores.push(bRestarted);
  assert.equal(bRestarted.peerId, originalPeerIdB, "peer identity survives a restart");
  assert.deepEqual(await bRestarted.get({
    cid: published.cid,
    expectedCid: published.cid,
    expectedSha256: published.sha256,
  }), data);
});

test("missing provider fails without an HTTP gateway fallback", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "amn-storage-missing-"));
  const source = await makeStore(path.join(root, "source")).open();
  const isolated = await makeStore(path.join(root, "isolated")).open();
  t.after(async () => { await source.close(); await isolated.close(); await rm(root, { recursive: true, force: true }); });
  const bytes = Buffer.from("only available on an offline peer");
  const artifact = await source.putPublic(bytes, { kind: "public-receipt", authorization: { approved: true } });
  await source.close();
  await assert.rejects(isolated.get({
    cid: artifact.cid,
    expectedCid: artifact.cid,
    expectedSha256: artifact.sha256,
    timeoutMs: 500,
  }));
});

test("retention expiration unpins and garbage collects owned local blocks", async (t) => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "amn-storage-retention-"));
  const store = await makeStore(dir).open();
  t.after(async () => { await store.close(); await rm(dir, { recursive: true, force: true }); });
  const published = await store.putPublic("short lived benchmark", {
    kind: "benchmark",
    authorization: { approved: true },
    retentionMs: 60_000,
  });
  assert.equal((await store.getStatus(published.cid)).pinned, true);
  assert.deepEqual(await store.releaseExpired(Date.now() + 120_000), [published.cid]);
  assert.deepEqual(await store.getStatus(published.cid), {
    cid: published.cid,
    availability: "not-local",
    local: false,
    pinned: false,
    scope: "local-node-only; no global availability claim",
  });
});

test("altered block bytes are rejected by the expected digest or CID", async (t) => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "amn-storage-tamper-"));
  const store = await makeStore(dir).open();
  t.after(async () => { await store.close(); await rm(dir, { recursive: true, force: true }); });
  const body = Buffer.from("block bytes that should be integrity checked");
  const artifact = await store.putPublic(body, { kind: "skill-package", authorization: { approved: true } });
  const blockFiles = await allFiles(path.join(dir, "blocks"));
  const dataFile = await firstMatchingFile(blockFiles, body);
  assert.ok(dataFile, "test fixture should be stored as an identifiable raw block");
  const altered = Buffer.from(body);
  altered[0] ^= 0xff;
  await writeFile(dataFile, altered);
  await assert.rejects(store.get({
    cid: artifact.cid,
    expectedCid: artifact.cid,
    expectedSha256: artifact.sha256,
  }));
});

function makeStore(storageDir, overrides = {}) {
  return new HeliaArtifactStore({
    storageDir,
    listenAddresses: listen,
    publicAuthorization,
    privateGrantPolicy: async ({ action }) => action === "read",
    ...overrides,
  });
}

async function allFiles(directory) {
  const files = [];
  async function walk(current) {
    for (const entry of await readdir(current, { withFileTypes: true })) {
      const fullPath = path.join(current, entry.name);
      if (entry.isDirectory()) await walk(fullPath);
      else files.push(fullPath);
    }
  }
  await walk(directory);
  return files;
}

async function firstMatchingFile(files, bytes) {
  for (const file of files) if ((await readFile(file)).equals(bytes)) return file;
  return undefined;
}
