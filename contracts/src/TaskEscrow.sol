// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

/// @notice Testable escrow primitive. Validator selection and drand proof
/// verification remain protocol-adapter responsibilities, not guarantees made
/// by this contract. This contract never creates an asset.
contract TaskEscrow is EIP712, ReentrancyGuard {
    using SafeERC20 for IERC20;

    enum State { NONE, FUNDED, COMMITTED, VERIFYING, SETTLED, REFUNDED }
    enum Outcome { VERIFIED, FAILED, INCONCLUSIVE, DISPUTED }

    struct Task {
        address customer;
        address executor;
        IERC20 token;
        uint256 total;
        uint256 executionReward;
        uint256 verificationBudget;
        uint64 deadline;
        State state;
        bytes32 quoteId;
        bytes32 commitmentRoot;
        bytes32 artifactHash;
        bytes32 randomnessEvidenceHash;
        bytes32 finalDecisionHash;
        uint32 verifierQuorum;
    }

    bytes32 private constant DECISION_TYPEHASH = keccak256(
        "Decision(bytes32 taskId,bytes32 commitmentRoot,uint8 outcome,bytes32 decisionHash)"
    );

    mapping(bytes32 => Task) public tasks;
    mapping(bytes32 => mapping(address => bool)) public assignedVerifier;
    mapping(bytes32 => address[]) private verifierList;
    mapping(bytes32 => mapping(address => uint256)) public verifierFee;

    error InvalidTask();
    error InvalidTerms();
    error Unauthorized();
    error InvalidCommitment();
    error InvalidVerifierSet();
    error InvalidDecision();
    error AlreadyFinalized();
    error DeadlinePassed();

    event TaskFunded(bytes32 indexed taskId, bytes32 indexed quoteId, address customer, address executor, uint256 total);
    event ExecutionCommitted(bytes32 indexed taskId, bytes32 indexed root, bytes32 artifactHash);
    event VerifiersAssigned(bytes32 indexed taskId, bytes32 randomnessEvidenceHash, uint256 count, uint256 quorum);
    event TaskSettled(bytes32 indexed taskId, Outcome outcome, bytes32 decisionHash, uint256 executorPaid, uint256 verifierPaid, uint256 refunded);
    event TaskRefunded(bytes32 indexed taskId, bytes32 reasonHash, uint256 amount);

    constructor() EIP712("AI Mining Network Task Escrow", "1") {}

    function fund(
        bytes32 taskId,
        bytes32 quoteId,
        IERC20 token,
        address executor,
        uint256 executionReward,
        uint256 verificationBudget,
        uint64 deadline
    ) external nonReentrant {
        if (taskId == bytes32(0) || tasks[taskId].state != State.NONE) revert InvalidTask();
        if (quoteId == bytes32(0) || address(token) == address(0) || executor == address(0) || executor == msg.sender) revert InvalidTerms();
        if (executionReward == 0 || deadline <= block.timestamp) revert InvalidTerms();
        uint256 total = executionReward + verificationBudget;
        tasks[taskId] = Task({
            customer: msg.sender,
            executor: executor,
            token: token,
            total: total,
            executionReward: executionReward,
            verificationBudget: verificationBudget,
            deadline: deadline,
            state: State.FUNDED,
            quoteId: quoteId,
            commitmentRoot: bytes32(0),
            artifactHash: bytes32(0),
            randomnessEvidenceHash: bytes32(0),
            finalDecisionHash: bytes32(0),
            verifierQuorum: 0
        });
        uint256 balanceBefore = token.balanceOf(address(this));
        token.safeTransferFrom(msg.sender, address(this), total);
        if (token.balanceOf(address(this)) - balanceBefore != total) revert InvalidTerms();
        emit TaskFunded(taskId, quoteId, msg.sender, executor, total);
    }

    function commit(bytes32 taskId, bytes32 root, bytes32 artifactHash) external {
        Task storage task = tasks[taskId];
        if (task.state != State.FUNDED) revert InvalidTask();
        if (msg.sender != task.executor) revert Unauthorized();
        if (block.timestamp > task.deadline) revert DeadlinePassed();
        if (root == bytes32(0) || artifactHash == bytes32(0)) revert InvalidCommitment();
        task.commitmentRoot = root;
        task.artifactHash = artifactHash;
        task.state = State.COMMITTED;
        emit ExecutionCommitted(taskId, root, artifactHash);
    }

    /// @dev Called after commitment. The evidence hash binds this assignment
    /// to public randomness/assignment records, but this primitive does not
    /// validate drand cryptography or Sybil resistance itself.
    function assignVerifiers(
        bytes32 taskId,
        address[] calldata verifiers,
        uint256[] calldata fees,
        uint32 quorum,
        bytes32 randomnessEvidenceHash
    ) external {
        Task storage task = tasks[taskId];
        if (task.state != State.COMMITTED) revert InvalidTask();
        if (block.timestamp > task.deadline) revert DeadlinePassed();
        if (randomnessEvidenceHash == bytes32(0) || verifiers.length == 0 || verifiers.length != fees.length || quorum == 0 || quorum > verifiers.length) revert InvalidVerifierSet();
        uint256 feeSum;
        for (uint256 i; i < verifiers.length; ++i) {
            address verifier = verifiers[i];
            if (verifier == address(0) || verifier == task.executor || verifier == task.customer || assignedVerifier[taskId][verifier]) revert InvalidVerifierSet();
            assignedVerifier[taskId][verifier] = true;
            verifierList[taskId].push(verifier);
            verifierFee[taskId][verifier] = fees[i];
            feeSum += fees[i];
        }
        if (feeSum > task.verificationBudget) revert InvalidVerifierSet();
        task.verifierQuorum = quorum;
        task.randomnessEvidenceHash = randomnessEvidenceHash;
        task.state = State.VERIFYING;
        emit VerifiersAssigned(taskId, randomnessEvidenceHash, verifiers.length, quorum);
    }

    /// @notice Settle only a quorum-signed decision. Repeating the same
    /// idempotency terms is a no-op; a conflicting decision is rejected.
    function settle(
        bytes32 taskId,
        Outcome outcome,
        bytes32 decisionHash,
        address[] calldata signers,
        bytes[] calldata signatures
    ) external nonReentrant {
        Task storage task = tasks[taskId];
        if (task.state == State.SETTLED || task.state == State.REFUNDED) {
            if (task.finalDecisionHash == decisionHash && decisionHash != bytes32(0)) return;
            revert AlreadyFinalized();
        }
        if (task.state != State.VERIFYING || decisionHash == bytes32(0) || outcome == Outcome.DISPUTED) revert InvalidDecision();
        if (block.timestamp > task.deadline) revert DeadlinePassed();
        if (signers.length != signatures.length || signers.length < task.verifierQuorum) revert InvalidDecision();
        bytes32 digest = _hashTypedDataV4(keccak256(abi.encode(DECISION_TYPEHASH, taskId, task.commitmentRoot, uint8(outcome), decisionHash)));
        uint256 verifierPaid;
        address previous;
        for (uint256 i; i < signers.length; ++i) {
            address signer = signers[i];
            if (signer <= previous || !assignedVerifier[taskId][signer] || ECDSA.recover(digest, signatures[i]) != signer) revert InvalidDecision();
            previous = signer;
            uint256 fee = verifierFee[taskId][signer];
            verifierPaid += fee;
            if (fee != 0) task.token.safeTransfer(signer, fee);
        }
        if (verifierPaid > task.verificationBudget) revert InvalidDecision();
        uint256 refund = task.verificationBudget - verifierPaid;
        uint256 executorPaid;
        if (outcome == Outcome.VERIFIED) {
            executorPaid = task.executionReward;
            task.token.safeTransfer(task.executor, executorPaid);
            task.state = State.SETTLED;
        } else {
            refund += task.executionReward;
            task.state = State.REFUNDED;
        }
        if (refund != 0) task.token.safeTransfer(task.customer, refund);
        task.finalDecisionHash = decisionHash;
        emit TaskSettled(taskId, outcome, decisionHash, executorPaid, verifierPaid, refund);
    }

    function refundExpired(bytes32 taskId, bytes32 reasonHash) external nonReentrant {
        Task storage task = tasks[taskId];
        if (task.state == State.SETTLED || task.state == State.REFUNDED) revert AlreadyFinalized();
        if (task.state == State.NONE || block.timestamp <= task.deadline) revert InvalidTask();
        uint256 amount = task.total;
        task.state = State.REFUNDED;
        task.finalDecisionHash = reasonHash;
        task.token.safeTransfer(task.customer, amount);
        emit TaskRefunded(taskId, reasonHash, amount);
    }

    function getVerifiers(bytes32 taskId) external view returns (address[] memory) {
        return verifierList[taskId];
    }
}
