const assert = require("node:assert/strict");
const { ethers } = require("hardhat");

describe("TaskEscrow", function () {
  async function fixture() {
    const [customer, executor, validatorA, validatorB, outsider] = await ethers.getSigners();
    const Token = await ethers.getContractFactory("TestUSD");
    const token = await Token.deploy();
    const Escrow = await ethers.getContractFactory("TaskEscrow");
    const escrow = await Escrow.deploy();
    const total = 1_000_000n;
    await token.mint(customer.address, total * 5n);
    await token.connect(customer).approve(escrow.target, total * 5n);
    const taskId = ethers.id("invoice-task-1");
    const quoteId = ethers.id("quote-1");
    const root = ethers.id("root-1");
    const artifact = ethers.id("artifact-1");
    const evidence = ethers.id("drand-and-assignment-record");
    const decision = ethers.id("decision-1");
    const expiry = BigInt((await ethers.provider.getBlock("latest")).timestamp + 3600);
    await escrow.connect(customer).fund(taskId, quoteId, token.target, executor.address, 700_000n, 300_000n, expiry);
    await escrow.connect(executor).commit(taskId, root, artifact);
    await escrow.assignVerifiers(taskId, [validatorA.address, validatorB.address], [100_000n, 100_000n], 2, evidence);
    return { customer, executor, validatorA, validatorB, outsider, token, escrow, taskId, root, decision, total, expiry };
  }

  async function signaturesFor(ctx, outcome = 0, signers = [ctx.validatorA, ctx.validatorB]) {
    const network = await ethers.provider.getNetwork();
    const domain = { name: "AI Mining Network Task Escrow", version: "1", chainId: network.chainId, verifyingContract: ctx.escrow.target };
    const types = { Decision: [
      { name: "taskId", type: "bytes32" },
      { name: "commitmentRoot", type: "bytes32" },
      { name: "outcome", type: "uint8" },
      { name: "decisionHash", type: "bytes32" },
    ] };
    const value = { taskId: ctx.taskId, commitmentRoot: ctx.root, outcome, decisionHash: ctx.decision };
    const ordered = [...signers].sort((a, b) => a.address.toLowerCase().localeCompare(b.address.toLowerCase()));
    const sigs = await Promise.all(ordered.map(signer => signer.signTypedData(domain, types, value)));
    return { signers: ordered.map(s => s.address), sigs };
  }

  it("funds, requires a committed root, and pays a quorum-signed verified decision once", async function () {
    const ctx = await fixture();
    const { signers, sigs } = await signaturesFor(ctx, 0);
    await ctx.escrow.settle(ctx.taskId, 0, ctx.decision, signers, sigs);
    assert.equal(await ctx.token.balanceOf(ctx.executor.address), 700_000n);
    assert.equal(await ctx.token.balanceOf(ctx.validatorA.address) + await ctx.token.balanceOf(ctx.validatorB.address), 200_000n);
    assert.equal(await ctx.token.balanceOf(ctx.customer.address), ctx.total * 5n - 900_000n);
    await ctx.escrow.settle(ctx.taskId, 0, ctx.decision, signers, sigs);
    assert.equal(await ctx.token.balanceOf(ctx.executor.address), 700_000n);
    await assert.rejects(ctx.escrow.settle(ctx.taskId, 1, ethers.id("conflict"), signers, sigs));
  });

  it("refunds the execution reward after a quorum-signed failure", async function () {
    const ctx = await fixture();
    const { signers, sigs } = await signaturesFor(ctx, 1);
    await ctx.escrow.settle(ctx.taskId, 1, ctx.decision, signers, sigs);
    assert.equal(await ctx.token.balanceOf(ctx.executor.address), 0n);
    assert.equal(await ctx.token.balanceOf(ctx.customer.address), ctx.total * 5n - 200_000n);
  });

  it("rejects outsider decisions and duplicate assigned verifiers", async function () {
    const ctx = await fixture();
    const impostor = await signaturesFor(ctx, 0, [ctx.outsider, ctx.validatorA]);
    await assert.rejects(ctx.escrow.settle(ctx.taskId, 0, ctx.decision, impostor.signers, impostor.sigs));
  });

  it("refunds if the protocol deadline elapses", async function () {
    const ctx = await fixture();
    await ethers.provider.send("evm_setNextBlockTimestamp", [Number(ctx.expiry + 1n)]);
    await ctx.escrow.refundExpired(ctx.taskId, ethers.id("timeout"));
    assert.equal(await ctx.token.balanceOf(ctx.customer.address), ctx.total * 5n);
  });
});
