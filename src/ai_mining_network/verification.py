"""Evidence-aware invoice verification and task consensus."""

from __future__ import annotations

import hashlib
import re

from .canonical import canonical_json_bytes, sha256_hex
from .invoice import ExtractedInvoice, InvoiceFile, normalize_record
from .merkle import MerkleTree


def source_line_reference(text: str, invoice_id: str) -> dict:
    """Deterministic verifier for the synthetic labeled invoice format only."""
    mapping = {
        "invoice id": "invoiceId",
        "supplier": "supplier",
        "invoice date": "invoiceDate",
        "total amount": "totalAmount",
        "currency": "currency",
    }
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([^:]+):\s*(.*?)\s*$", line)
        if match:
            normalized_key = match.group(1).strip().lower()
            if normalized_key in mapping:
                fields[mapping[normalized_key]] = match.group(2)
    if set(fields) != set(mapping.values()):
        raise ValueError("source evidence does not match the synthetic invoice verification format")
    record = ExtractedInvoice(**fields)
    normalized = normalize_record(record)
    if normalized["invoiceId"] != invoice_id.upper():
        raise ValueError("source invoice ID does not match the assigned unit")
    return {"record": normalized, "lines": fields}


def verify_invoice_unit(
    unit_index: int,
    invoice: InvoiceFile,
    committed: dict,
    independently_extracted: ExtractedInvoice,
    tree: MerkleTree,
) -> dict:
    proof = tree.proof(unit_index)
    proof_valid = tree.verify(unit_index, proof)
    observed = normalize_record(independently_extracted)
    source = source_line_reference(invoice.text, invoice.invoice_id)
    source_matches = committed == source["record"]
    replay_matches = committed == observed
    if not proof_valid:
        result, reason = "FAIL", "INVALID_MERKLE_PROOF"
    elif not source_matches:
        result, reason = "FAIL", "SOURCE_FIELD_MISMATCH"
    elif not replay_matches:
        result, reason = "INCONCLUSIVE", "INDEPENDENT_MODEL_DISAGREEMENT"
    else:
        result, reason = "PASS", "SOURCE_AND_REPLAY_MATCH"
    evidence = {
        "unitId": invoice.invoice_id,
        "inputHash": invoice.input_hash,
        "committedOutput": committed,
        "independentOutput": observed,
        "sourceReference": source["record"],
        "merkleProofValid": proof_valid,
    }
    return {
        "result": result,
        "reasonCode": reason,
        "evidenceHash": sha256_hex(canonical_json_bytes(evidence)),
        "proof": proof,
        "observedOutputHash": sha256_hex(canonical_json_bytes(observed)),
        "evidence": evidence,
    }


def decide(receipts: list[dict], expected_count: int) -> dict:
    if len(receipts) < expected_count:
        return {
            "outcome": "INCONCLUSIVE",
            "reasonCode": "VALIDATOR_RECEIPT_SHORTFALL",
            "explanation": f"Received {len(receipts)} of {expected_count} assigned verification results.",
            "acceptedReceiptIds": [r["receiptId"] for r in receipts if r["result"] == "PASS"],
            "rejectedReceiptIds": [],
            "auditRequired": True,
        }
    by_unit: dict[str, list[dict]] = {}
    for receipt in receipts:
        by_unit.setdefault(receipt["unitId"], []).append(receipt)
    conflicts = {
        unit_id: items
        for unit_id, items in by_unit.items()
        if len({item["result"] for item in items}) > 1
    }
    if conflicts:
        details = ", ".join(
            f"{unit_id}: {sorted({item['result'] for item in items})}"
            for unit_id, items in sorted(conflicts.items())
        )
        return {
            "outcome": "DISPUTED",
            "reasonCode": "CONFLICTING_VALIDATOR_RESULTS",
            "explanation": f"Validators returned conflicting results for the same committed unit ({details}); an additional audit is required.",
            "acceptedReceiptIds": [r["receiptId"] for r in receipts if r["result"] == "PASS"],
            "rejectedReceiptIds": [r["receiptId"] for items in conflicts.values() for r in items],
            "auditRequired": True,
        }
    failed = [r for r in receipts if r["result"] == "FAIL"]
    inconclusive = [r for r in receipts if r["result"] in {"INCONCLUSIVE", "TIMEOUT", "DISPUTE"}]
    if failed:
        source_proven = all(r.get("reasonCode") in {"SOURCE_FIELD_MISMATCH", "INVALID_MERKLE_PROOF"} for r in failed)
        outcome = "FAILED" if source_proven else "DISPUTED"
        reason = "OBJECTIVE_EVIDENCE_OF_MISMATCH" if source_proven else "VALIDATOR_DISAGREEMENT"
        return {
            "outcome": outcome,
            "reasonCode": reason,
            "explanation": f"{len(failed)} assigned unit(s) failed source or commitment checks; receipts identify the evidence and trust assumptions.",
            "acceptedReceiptIds": [r["receiptId"] for r in receipts if r["result"] == "PASS"],
            "rejectedReceiptIds": [r["receiptId"] for r in failed],
            "auditRequired": outcome == "DISPUTED",
        }
    if inconclusive:
        return {
            "outcome": "INCONCLUSIVE",
            "reasonCode": "VERIFICATION_INCONCLUSIVE",
            "explanation": f"{len(inconclusive)} assigned unit(s) could not be independently resolved.",
            "acceptedReceiptIds": [r["receiptId"] for r in receipts if r["result"] == "PASS"],
            "rejectedReceiptIds": [],
            "auditRequired": True,
        }
    return {
        "outcome": "VERIFIED",
        "reasonCode": "ALL_ASSIGNED_SAMPLES_PASSED",
        "explanation": "Every assigned sample passed source-field, independent extraction, and Merkle membership checks. This is policy evidence, not proof that inference occurred.",
        "acceptedReceiptIds": [r["receiptId"] for r in receipts],
        "rejectedReceiptIds": [],
        "auditRequired": False,
    }

