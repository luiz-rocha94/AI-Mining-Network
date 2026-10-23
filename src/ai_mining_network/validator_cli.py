"""Local validator receipt and Merkle-evidence audit CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from eth_account import Account

from .identity import load_or_create_private_key
from .invoice import OllamaExtractor
from .merkle import verify_proof
from .models import SignedMessage, ValidatorCapabilities
from .signing import create_message, verify_message
from .canonical import content_hash


def audit_task(root: Path, task_id: str) -> dict:
    task_dir = (root / ".amn" / "jobs" / task_id).resolve()
    if task_dir.parent != (root / ".amn" / "jobs").resolve() or not task_dir.is_dir():
        raise ValueError("task not found")
    commitment = json.loads((task_dir / "execution-commitment.json").read_text(encoding="utf-8"))
    if not verify_message(SignedMessage.model_validate(commitment)):
        raise ValueError("execution commitment signature is invalid")
    commitment_payload = commitment["payload"]
    root_hash = commitment_payload["merkleRoot"]
    checked = []
    for path in sorted((task_dir / "receipts").glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        evidence = raw.get("evidence")
        message = SignedMessage.model_validate({key: value for key, value in raw.items() if key != "evidence"})
        payload = message.payload
        signature_valid = verify_message(message)
        identity_bound = message.signer.lower() == payload.validatorPeerId.lower()
        evidence_hash_valid = bool(evidence) and payload.evidenceHash == content_hash(evidence)
        unit_id = evidence.get("unitId", "") if evidence else ""
        unit_key = "0x" + hashlib.sha256(unit_id.encode("utf-8")).hexdigest()
        leaf = (unit_key, evidence.get("inputHash", "") if evidence else "", _json_hash(evidence.get("committedOutput")) if evidence else "")
        proof_valid = bool(evidence) and payload.commitmentRoot == root_hash and verify_proof(leaf, payload.merkleProof, root_hash)
        checked.append({
            "receiptId": payload.receiptId,
            "unitId": payload.unitId,
            "result": payload.result,
            "signatureValid": signature_valid,
            "identityBound": identity_bound,
            "evidenceHashValid": evidence_hash_valid,
            "merkleInclusionValid": proof_valid,
            "evidence": evidence,
        })
    if not checked:
        raise ValueError("no validator receipts are available for this task")
    return {
        "taskId": task_id,
        "commitmentRoot": root_hash,
        "commitmentSignatureValid": True,
        "receiptCount": len(checked),
        "validReceiptCount": sum(item["signatureValid"] and item["identityBound"] and item["evidenceHashValid"] and item["merkleInclusionValid"] for item in checked),
        "receipts": checked,
    }


def _json_hash(value) -> str:
    from .canonical import content_hash

    return content_hash(value)


def main() -> None:
    parser = argparse.ArgumentParser(prog="amn-validator", description="Local AI Mining Network validator tools")
    subcommands = parser.add_subparsers(dest="command", required=True)
    capability = subcommands.add_parser("capabilities", help="create a signed local validator capability receipt")
    capability.add_argument("--root", type=Path, default=Path.cwd())
    capability.add_argument("--model", default="qwen2.5:1.5b")
    audit = subcommands.add_parser("audit", help="verify execution commitment and validator receipt evidence")
    audit.add_argument("task_id")
    audit.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if args.command == "capabilities":
        root = args.root.resolve()
        account = Account.from_key(load_or_create_private_key(root, "validator-1"))
        model_digests = []
        ollama_ready = False
        try:
            model_digests = [OllamaExtractor(args.model).model_digest]
            ollama_ready = True
        except Exception as exc:
            print(f"Ollama is not ready; validator advertisement will be marked unavailable: {exc}")
        capabilities = ValidatorCapabilities(
            peerId=account.address,
            paymentAddress=account.address,
            verificationStrategies=["per-document"],
            modelDigests=model_digests,
            maxUnitsPerTask=100,
            available=ollama_ready,
        )
        signed = create_message(capabilities, account.key.hex())
        target = root / ".amn" / "capabilities" / "validator.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(signed.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")
        print(json.dumps({"capability": capabilities.model_dump(mode="json"), "signature": signed.signature, "advertisement": str(target)}, indent=2))
        print("This command creates a local development identity; it does not join a peer-to-peer validator set.")
        return
    result = audit_task(args.root.resolve(), args.task_id)
    print(json.dumps(result, indent=2))
    if result["validReceiptCount"] != result["receiptCount"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
