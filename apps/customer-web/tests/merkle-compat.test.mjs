import test from "node:test";
import assert from "node:assert/strict";
import { StandardMerkleTree } from "@openzeppelin/merkle-tree";

test("Python implementation shares OpenZeppelin StandardMerkleTree golden root", () => {
  const values = [0n, 3n, 6n].map((value) => [
    `0x${value.toString(16).padStart(64, "0")}`,
    `0x${(value + 1n).toString(16).padStart(64, "0")}`,
    `0x${(value + 2n).toString(16).padStart(64, "0")}`,
  ]);
  const tree = StandardMerkleTree.of(values, ["bytes32", "bytes32", "bytes32"]);
  assert.equal(tree.root, "0x401f696a1b3468a5994a7a85811e537653fd611ec01b626d53dc09d7110035e0");
});
