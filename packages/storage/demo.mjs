import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { HeliaArtifactStore, createRecipientKeyPair } from "./src/index.mjs";

const root = await mkdtemp(path.join(os.tmpdir(), "amn-storage-demo-"));
const allowPrivateAccess = async ({ action, principalId }) =>
  ["issue", "read"].includes(action) && principalId === "demo-validator";
const publisher = new HeliaArtifactStore({
  storageDir: path.join(root, "publisher"),
  listenAddresses: ["/ip4/127.0.0.1/tcp/0"],
  publicAuthorization: async () => true,
  privateGrantPolicy: allowPrivateAccess,
});
const consumer = new HeliaArtifactStore({
  storageDir: path.join(root, "consumer"),
  listenAddresses: ["/ip4/127.0.0.1/tcp/0"],
  privateGrantPolicy: async ({ action, principalId }) => action === "read" && principalId === "demo-validator",
});

try {
  await publisher.open();
  await consumer.open();
  const skill = await publisher.putPublic(
    JSON.stringify({ name: "invoice-extractor", version: "1.0.0", manifest: "public demo skill" }),
    { kind: "skill-package", authorization: { demo: "explicitly authorized for public demo" } },
  );
  const customerFile = Buffer.from("synthetic invoice: supplier=Example Ltd; total=42.00 USD");
  const recipient = createRecipientKeyPair();
  const privateArtifact = await publisher.putPrivate(customerFile, {
    taskId: "demo-task-1",
    recipients: [{ principalId: "demo-validator", publicKey: recipient.publicKey }],
  });

  await consumer.connect(publisher.multiaddrs[0]);
  const publicCopy = await consumer.get({
    cid: skill.cid,
    expectedCid: skill.cid,
    expectedSha256: skill.sha256,
  });
  const authorizedPrivateCopy = await consumer.getPrivate({
    envelope: privateArtifact.grants[0],
    recipientPrincipalId: "demo-validator",
    recipientPrivateKey: recipient.privateKey,
  });

  process.stdout.write(`${JSON.stringify({
    demo: "two local Helia peers; no gateway, cloud pinning service, or DHT bootstrap",
    publisherPeerId: publisher.peerId,
    consumerPeerId: consumer.peerId,
    publicSkillCid: skill.cid,
    publicSkillRetrievedByPeer: publicCopy.toString().includes("invoice-extractor"),
    privateArtifactCid: privateArtifact.cid,
    privateDataRetrievedAndDecryptedForAssignedValidator: authorizedPrivateCopy.equals(customerFile),
    publicAnnouncement: await publisher.announce(skill.cid),
    privacyNote: "Only encrypted bytes are stored for the private artifact; recipient grants are passed out of band.",
  }, null, 2)}\n`);
} finally {
  await Promise.allSettled([publisher.close(), consumer.close()]);
  await rm(root, { recursive: true, force: true });
}
