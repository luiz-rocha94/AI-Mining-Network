require("@nomicfoundation/hardhat-ethers");
const { subtask } = require("hardhat/config");
const { TASK_COMPILE_SOLIDITY_GET_SOLC_BUILD } = require("hardhat/builtin-tasks/task-names");

subtask(TASK_COMPILE_SOLIDITY_GET_SOLC_BUILD).setAction(async ({ solcVersion }, _hre, runSuper) => {
  if (solcVersion === "0.8.30") {
    return {
      compilerPath: require.resolve("solc/soljson.js"),
      isSolcJs: true,
      version: solcVersion,
      longVersion: require("solc").version(),
    };
  }
  return runSuper();
});

module.exports = {
  solidity: { version: "0.8.30", settings: { optimizer: { enabled: true, runs: 200 }, evmVersion: "cancun", viaIR: true } },
  paths: { sources: "./src", tests: "./test", cache: "../.hardhat-cache", artifacts: "../.hardhat-artifacts" },
  mocha: { timeout: 30000 },
};
