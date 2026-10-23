import json
import shutil
import tempfile
from pathlib import Path

import pytest
from eth_account import Account

from ai_mining_network.benchmark import fraud_detection_probability, summarize_runs
from ai_mining_network.benchmark_service import run_invoice_benchmark
from ai_mining_network.canonical import canonical_json_bytes, content_hash
from ai_mining_network.invoice import FixtureExtractor, generate_synthetic_invoices, normalize_record, write_workbook
from ai_mining_network.merkle import build_tree
from ai_mining_network.models import Currency, MinerOffer, ResourceRequirements, SignedMessage
from ai_mining_network.orchestrator import DemoService
from ai_mining_network.pricing import PricingError, fixed_quote
from ai_mining_network.reputation import aggregate_miner_reliability, aggregate_skill_quality, aggregate_validator_reliability
from ai_mining_network.registry import LocalSkillRegistry, compute_skill_hash, read_skill
from ai_mining_network.settlement import create_settlement, settle_once
from ai_mining_network.signing import create_message, verify_message
from ai_mining_network.state_machine import EventLog, InvalidTransition, validate_transition
from ai_mining_network.verification import decide, verify_invoice_unit


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tmp_path():
    # pytest's built-in tmp_path creates a symlink for "current" on Windows;
    # that is denied in the restricted workspace runner. Keep tests self-contained.
    directory = Path(tempfile.mkdtemp(prefix="amn-test-", dir=ROOT))
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def copy_skill(root: Path) -> Path:
    target = root / "skills" / "invoice-extractor"
    shutil.copytree(ROOT / "skills" / "invoice-extractor", target)
    return target


def copy_document_skill(root: Path) -> Path:
    target = root / "skills" / "document-assistant"
    shutil.copytree(ROOT / "skills" / "document-assistant", target)
    return target


def make_service(tmp_path: Path) -> DemoService:
    copy_skill(tmp_path)
    return DemoService(tmp_path, extractor_factory=FixtureExtractor)


def wait_for(service: DemoService, task_id: str, expected: set[str], timeout: float = 20) -> dict:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = service.status(task_id)
        if status["state"] in expected:
            worker = service._workers.get(task_id)
            if worker is not None:
                worker.join(timeout=5)
            return service.status(task_id)
        time.sleep(0.02)
    raise AssertionError(f"task did not reach {expected}; last state={service.status(task_id)['state']}")


def create_and_run(service: DemoService, **kwargs) -> dict:
    job = service.create_quote(**kwargs)
    service.accept_quote(job["task"]["taskId"])
    return wait_for(service, job["task"]["taskId"], {"SETTLED", "REFUNDED", "DISPUTED"})


def test_canonical_json_is_stable_and_rejects_floats():
    assert canonical_json_bytes({"z": "ç", "a": 2}) == '{"a":2,"z":"ç"}'.encode()
    assert content_hash({"x": 1}) == content_hash({"x": 1})
    with pytest.raises(TypeError):
        canonical_json_bytes({"cost": 0.1})


def test_generated_protocol_schema_matches_versioned_models():
    from ai_mining_network.schema_export import build_schema

    checked_in = json.loads((ROOT / "packages" / "protocol" / "v2" / "messages.schema.json").read_text())
    assert checked_in == build_schema()
    assert "ExecutionCommitment" in checked_in["$defs"]
    assert "VerificationReceipt" in checked_in["$defs"]
    assert "PaymentSettlement" in checked_in["$defs"]


def test_eip712_message_signature_detects_payload_mutation():
    account = Account.create()
    payload = {"kind": "SkillRating", "ratingId": "r-1", "taskId": "t-1", "skillHash": "sha256:" + "a" * 64, "stars": 5, "comment": "ok"}
    from ai_mining_network.models import SkillRating

    signed = create_message(SkillRating.model_validate(payload), account.key.hex())
    assert signed.signer.lower() == account.address.lower()
    assert verify_message(signed)
    tampered = signed.model_copy(update={"payload": signed.payload.model_copy(update={"stars": 1})})
    assert not verify_message(tampered)


def test_reputation_dimensions_are_separate_and_ratings_are_deduplicated():
    from ai_mining_network.models import SkillRating

    account = Account.create()
    first = create_message(SkillRating(ratingId="r1", taskId="task", skillHash="sha256:" + "a" * 64, stars=1), account.key.hex())
    duplicate = create_message(SkillRating(ratingId="r2", taskId="task", skillHash="sha256:" + "a" * 64, stars=5), account.key.hex())
    quality = aggregate_skill_quality([first, duplicate])
    assert quality["ratingCount"] == 1
    assert quality["averageStarsMilli"] == 1000
    assert quality["paymentEffect"] == "NONE"
    miner = aggregate_miner_reliability([{"outcome": "VERIFIED"}, {"outcome": "TIMEOUT"}, {"outcome": "FAILED"}])
    assert miner["reliabilityBps"] == 3333
    assert miner["customerRatingInputs"] == 0
    validator = aggregate_validator_reliability({"receipt-a": True, "receipt-b": False})
    assert validator["reliabilityBps"] == 5000


def test_merkle_membership_and_mutation_rejection():
    values = [("0x" + f"{index:064x}", "0x" + f"{index + 100:064x}", "0x" + f"{index + 200:064x}") for index in range(100)]
    tree = build_tree(values)
    assert tree.verify(0)
    assert tree.verify(99)
    proof = tree.proof(23)
    assert tree.verify(23, proof)
    assert not tree.verify(23, proof[:-1] + ["0x" + "00" * 32])
    with pytest.raises(ValueError):
        build_tree([values[0], values[0]])


def test_merkle_root_matches_openzeppelin_standard_tree_golden_vector():
    values = [("0x" + f"{index:064x}", "0x" + f"{index + 1:064x}", "0x" + f"{index + 2:064x}") for index in (0, 3, 6)]
    assert build_tree(values).root == "0x401f696a1b3468a5994a7a85811e537653fd611ec01b626d53dc09d7110035e0"


def test_quote_is_fixed_and_never_over_customer_max():
    currency = Currency(chainId=31337, tokenAddress="0x" + "1" * 40, decimals=6, symbol="TESTUSD")
    datetime = __import__("datetime")
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=1)
    requirements = ResourceRequirements()
    executor = MinerOffer(offerId="e", peerId="miner", skillHash="h", priceAtomic=100, currency=currency, estimatedDurationMs=10, expiresAt=now, requirements=requirements)
    validators = [MinerOffer(offerId=f"v{i}", peerId=f"peer{i}", skillHash="h", priceAtomic=10, currency=currency, estimatedDurationMs=2, expiresAt=now, requirements=requirements) for i in range(2)]
    quote = fixed_quote("task", currency, [executor], validators, 4, 2, 120)
    assert quote.totalAtomic == 120
    # The quote cannot outlive any selected miner offer.
    assert quote.expiresAt == now
    with pytest.raises(PricingError):
        fixed_quote("task", currency, [executor], validators, 4, 2, 119)
    with pytest.raises(PricingError):
        fixed_quote("task", currency, [executor], validators, 4, 2, 120, required_skill_hash="different-skill")
    duplicate_peer_offers = [validators[0], validators[0].model_copy(update={"offerId": "v0-second", "priceAtomic": 11})]
    with pytest.raises(PricingError):
        fixed_quote("task", currency, [executor], duplicate_peer_offers, 4, 2, 120, required_skill_hash="h")


def test_benchmark_percentiles_and_exact_sampling_probability():
    summary = summarize_runs([10, 20, 30, 40, 50], [True, True, True, False, True])
    assert (summary.p50_duration_ms, summary.p90_duration_ms) == (30, 50)
    assert fraud_detection_probability(100, 0, 30) == 0
    assert fraud_detection_probability(100, 100, 30) == 1
    assert fraud_detection_probability(100, 5, 100) == 1
    assert 0 < fraud_detection_probability(100, 5, 30) < 1


def test_skill_registry_is_content_addressed(tmp_path):
    skill = copy_skill(tmp_path)
    manifest = read_skill(skill)
    registry = LocalSkillRegistry(tmp_path / "registry.sqlite3")
    published = registry.publish(skill)
    assert published["contentHash"].startswith("sha256:")
    assert registry.discover("invoice")[0]["contentHash"] == published["contentHash"]
    (skill / "SKILL.md").write_text((skill / "SKILL.md").read_text() + "\nchanged\n")
    assert compute_skill_hash(skill) != manifest["contentHash"]


def test_synthetic_pdf_fixture_extractor_matches_private_ground_truth(tmp_path):
    invoices = generate_synthetic_invoices(tmp_path / "inputs", 3, seed=99)
    ground_truth = json.loads((tmp_path / "inputs" / "ground-truth.json").read_text())
    extracted = FixtureExtractor().extract_batch(invoices)
    assert [normalize_record(record) for record in extracted] == ground_truth


def test_xlsx_text_fields_cannot_become_formulas(tmp_path):
    from openpyxl import load_workbook

    path = tmp_path / "safe.xlsx"
    write_workbook([{"invoiceId": "=1+1", "supplier": " @SUM(A1:A3)", "invoiceDate": "2026-01-01", "amountMinor": 100, "currency": "USD"}], path)
    sheet = load_workbook(path, data_only=False).active
    assert sheet["A2"].data_type == "s" and sheet["A2"].value.startswith("'")
    assert sheet["B2"].data_type == "s" and sheet["B2"].value.startswith("'")


def test_benchmark_is_versioned_signed_and_uses_reproducible_semantic_dataset(tmp_path):
    skill = copy_skill(tmp_path)
    first, report_a = run_invoice_benchmark(skill, tmp_path / "bench-a", FixtureExtractor(), sample_count=20)
    second, report_b = run_invoice_benchmark(skill, tmp_path / "bench-b", FixtureExtractor(), sample_count=20)
    assert first.benchmarkVersion == "invoice-synthetic-v1"
    assert first.datasetHash == second.datasetHash == report_a["datasetHash"] == report_b["datasetHash"]
    assert first.sampleCount == 20 and first.successRateBps == 10_000
    assert first.cpuAverageMilliPct is None and first.peakVramBytes is None
    from ai_mining_network.models import SignedMessage

    signed = SignedMessage.model_validate_json((tmp_path / "bench-a" / "benchmark-receipt.json").read_text())
    assert verify_message(signed)


def test_state_machine_and_settlement_idempotency(tmp_path):
    with pytest.raises(InvalidTransition):
        validate_transition("QUOTED", "SETTLED")
    log = EventLog(tmp_path / "audit.sqlite3")
    currency = Currency(chainId=31337, tokenAddress="0x" + "1" * 40, decimals=6, symbol="TESTUSD")
    settlement = create_settlement("task", "quote", "key", "VERIFIED", currency, 100, 30, "miner", {"v": 30}, {"v"}, "decision")
    first, applied = settle_once(log, settlement)
    second, retried = settle_once(log, settlement)
    assert applied and not retried and first == second
    assert len([event for event in log.events("task") if event["type"] == "payment.settled"]) == 1
    log.append("task", "after-settlement", {"ok": True})
    assert log.verify_event_chain("task")
    with log._connect() as db:
        db.execute("UPDATE events SET payload_json=? WHERE event_type=?", ('{"ok":false}', "after-settlement"))
    assert not log.verify_event_chain("task")
    with pytest.raises(ValueError):
        modified = settlement.model_copy(update={"decisionHash": "different"})
        settle_once(log, modified)


def test_quote_is_not_an_execution_contract_until_customer_accepts(tmp_path):
    service = make_service(tmp_path)
    quoted = service.create_quote(invoice_count=3, sample_count=3, validator_count=1)
    task_id = quoted["task"]["taskId"]
    task_dir = tmp_path / ".amn" / "jobs" / task_id
    record = service._jobs[task_id]
    assert quoted["state"] == "QUOTED"
    assert "contract" not in record and "contractMessage" not in record
    assert not (task_dir / "execution-contract.json").exists()
    quote_event = next(event for event in service.audit_events(task_id) if event["type"] == "quote.issued")
    assert (quote_event["payload"]["from"], quote_event["payload"]["to"]) == ("CREATED", "QUOTED")

    service.accept_quote(task_id)
    record = service._jobs[task_id]
    assert record["contract"]["acceptedAt"]
    assert verify_message(SignedMessage.model_validate(record["contractMessage"]))
    assert record["contractMessage"]["signer"].lower() == record["taskMessage"]["signer"].lower()
    assert (task_dir / "execution-contract.json").is_file()
    status = wait_for(service, task_id, {"SETTLED", "REFUNDED"})
    assert status["state"] == "SETTLED"


def test_consensus_distinguishes_pass_failure_inconclusive_and_conflict():
    def receipt(identifier, result, reason="ok"):
        return {"receiptId": identifier, "unitId": "invoice-1", "result": result, "reasonCode": reason}

    assert decide([receipt("a", "PASS")], 1)["outcome"] == "VERIFIED"
    assert decide([receipt("a", "FAIL", "SOURCE_FIELD_MISMATCH")], 1)["outcome"] == "FAILED"
    assert decide([receipt("a", "INCONCLUSIVE")], 1)["outcome"] == "INCONCLUSIVE"
    assert decide([receipt("a", "PASS"), receipt("b", "INCONCLUSIVE")], 1)["outcome"] == "DISPUTED"


def test_full_local_happy_path_rating_cannot_change_settlement(tmp_path):
    service = make_service(tmp_path)
    status = create_and_run(service, invoice_count=4, sample_count=4, validator_count=2)
    assert status["state"] == "SETTLED", status.get("error")
    assert status["decision"]["outcome"] == "VERIFIED"
    assert status["receiptCount"] == 4
    assert status["settlementRetryWasIdempotent"] is True
    record = service._jobs[status["task"]["taskId"]]
    from ai_mining_network.models import SignedMessage

    assert verify_message(SignedMessage.model_validate(record["taskMessage"]))
    assert verify_message(SignedMessage.model_validate(record["quoteMessage"]))
    assert verify_message(SignedMessage.model_validate(record["contractMessage"]))
    assert all(verify_message(SignedMessage.model_validate(item)) for item in record["offerMessages"])
    before = status["settlement"]
    rating = service.add_rating(status["task"]["taskId"], 1, "Not satisfied")
    after = service.status(status["task"]["taskId"])["settlement"]
    assert rating["paymentUnaffected"] is True
    assert rating["rating"]["taskId"] == status["task"]["taskId"]
    assert before == after
    assert not any("private-input" in item["name"] for item in service.artifacts(status["task"]["taskId"]))
    from ai_mining_network.validator_cli import audit_task

    receipt_audit = audit_task(tmp_path, status["task"]["taskId"])
    assert receipt_audit["validReceiptCount"] == 4
    assert service.event_log.verify_event_chain(status["task"]["taskId"])


def test_general_document_task_runs_commit_verify_and_settle(tmp_path, monkeypatch):
    from ai_mining_network import orchestrator

    copy_skill(tmp_path)
    copy_document_skill(tmp_path)

    class StableRunner:
        def __init__(self, model, _url):
            self.model = model
            self.model_digest = "sha256:test-model-digest"
            self.model_info = {"parameterSize": "4B", "digest": self.model_digest}
            self.last_metrics = {"durationMs": 2, "inputTokens": 30, "outputTokens": 8}

        def run(self, **kwargs):
            return {"response": f"{kwargs['prompt']}: {kwargs['document_text']}"}

    monkeypatch.setattr(orchestrator, "OllamaDocumentRunner", StableRunner)
    service = DemoService(tmp_path, extractor_factory=FixtureExtractor)
    job = service.create_document_quote(
        uploads=[("meeting.txt", b"Decision: ship the release."), ("notes.md", b"Owner: Alex")],
        prompt="Summarize", skill_name="document-assistant", model="test:4b",
        sample_count=2, validator_count=2,
    )
    assert job["state"] == "QUOTED"
    assert job["task"]["inputCount"] == 2
    assert job["task"]["model"] == "test:4b"
    signed_task = SignedMessage.model_validate(service._jobs[job["task"]["taskId"]]["taskMessage"])
    assert verify_message(signed_task)
    assert signed_task.payload.promptHash == content_hash("Summarize")
    assert signed_task.payload.outputSchemaHash == content_hash(service._jobs[job["task"]["taskId"]]["outputSchema"])
    assert signed_task.payload.modelName == "test:4b"
    assert signed_task.payload.modelDigest == "sha256:test-model-digest"
    assert job["quote"]["totalAtomic"] == job["quote"]["executionRewardAtomic"] + job["quote"]["verificationBudgetAtomic"]
    assert job["quote"]["verificationPolicy"]["factualCorrectnessProven"] is False
    assert job["quote"]["verificationPolicy"]["disputeOnMismatch"] is False
    service.accept_quote(job["task"]["taskId"])
    status = wait_for(service, job["task"]["taskId"], {"SETTLED", "REFUNDED", "DISPUTED"})
    assert status["state"] == "SETTLED", status.get("error")
    assert status["decision"]["outcome"] == "VERIFIED"
    assert "does not prove factual correctness" in status["decision"]["explanation"]
    assert status["commitment"]["unitCount"] == 2
    assert status["receiptCount"] == 2
    assert status["settlementRetryWasIdempotent"] is True
    task_dir = tmp_path / ".amn" / "jobs" / job["task"]["taskId"]
    result = json.loads((task_dir / "results.json").read_text())
    assert len(result) == 2 and result[0]["result"]["response"].startswith("Summarize:")
    assert not any("private-input" in item["name"] for item in service.artifacts(job["task"]["taskId"]))
    previous_settlement = status["settlement"]
    rating = service.add_rating(job["task"]["taskId"], 1, "The answer was not useful")
    assert rating["paymentUnaffected"] is True
    assert service.status(job["task"]["taskId"])["settlement"] == previous_settlement


def test_multi_skill_document_task_sums_quote_and_binds_ordered_skill_line(tmp_path, monkeypatch):
    from ai_mining_network import orchestrator

    copy_skill(tmp_path)
    copy_document_skill(tmp_path)
    skill_inputs = []

    class StableRunner:
        def __init__(self, model, _url):
            self.model = model
            self.model_digest = "sha256:test-model-digest"
            self.model_info = {"parameterSize": "4B", "digest": self.model_digest}
            self.last_metrics = {"durationMs": 2, "inputTokens": 30, "outputTokens": 8}

        def run(self, **kwargs):
            skill_inputs.append((kwargs["skill_name"], kwargs["document_text"]))
            return {"response": f"{kwargs['skill_name']}:{kwargs['document_text']}"}

    monkeypatch.setattr(orchestrator, "OllamaDocumentRunner", StableRunner)
    service = DemoService(tmp_path, extractor_factory=FixtureExtractor)
    selected = ["document-assistant", "invoice-extractor"]
    job = service.create_document_quote(
        uploads=[("brief.txt", b"A short brief")], prompt="Summarize this",
        skill_name=selected[0], skill_names=selected, model="test:4b",
        sample_count=1, validator_count=1,
    )

    record = service._jobs[job["task"]["taskId"]]
    signed_task = SignedMessage.model_validate(record["taskMessage"])
    expected_prompt = "document-assistant. invoice-extractor\n\nSummarize this"
    assert signed_task.payload.promptHash == content_hash(expected_prompt)
    assert job["task"]["skillNames"] == selected
    assert len(job["pricing"]["skills"]) == 2
    for field in ("executionRewardAtomic", "verificationBudgetAtomic", "protocolFeeAtomic"):
        assert job["pricing"][field] == sum(item[field] for item in job["pricing"]["skills"])
    assert job["quote"]["totalAtomic"] == sum(
        job["pricing"][field]
        for field in ("executionRewardAtomic", "verificationBudgetAtomic", "protocolFeeAtomic")
    )

    service.accept_quote(job["task"]["taskId"])
    status = wait_for(service, job["task"]["taskId"], {"SETTLED", "REFUNDED", "DISPUTED"})
    assert status["state"] == "SETTLED", status.get("error")
    result = json.loads((tmp_path / ".amn" / "jobs" / job["task"]["taskId"] / "results.json").read_text())
    assert list(result[0]["result"]) == selected
    assert result[0]["result"]["document-assistant"]["response"].startswith("document-assistant:")
    assert result[0]["result"]["invoice-extractor"]["response"].startswith("invoice-extractor:")
    invoice_inputs = [value for name, value in skill_inputs if name == "invoice-extractor"]
    assert len(invoice_inputs) == 2  # one miner execution and one validator replay
    assert all("document-assistant:" in value for value in invoice_inputs)


def test_connected_workflow_binds_graph_and_executes_to_final_output(tmp_path, monkeypatch):
    from ai_mining_network import orchestrator

    copy_skill(tmp_path)
    copy_document_skill(tmp_path)
    skill_inputs = []

    class StableRunner:
        def __init__(self, model, _url):
            self.model = model
            self.model_digest = "sha256:test-model-digest"
            self.model_info = {"parameterSize": "4B", "digest": self.model_digest}
            self.last_metrics = {"durationMs": 2, "inputTokens": 30, "outputTokens": 8}

        def run(self, **kwargs):
            skill_inputs.append((kwargs["skill_name"], kwargs["document_text"]))
            return {"response": f"{kwargs['skill_name']}:{kwargs['document_text']}"}

    monkeypatch.setattr(orchestrator, "OllamaDocumentRunner", StableRunner)
    service = DemoService(tmp_path, extractor_factory=FixtureExtractor)
    workflow = {
        "version": 1,
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "normalize", "type": "skill", "skillName": "document-assistant"},
            {"id": "extract", "type": "skill", "skillName": "invoice-extractor"},
            {"id": "output", "type": "output"},
        ],
        "edges": [
            {"from": "input", "to": "normalize"},
            {"from": "normalize", "to": "extract"},
            {"from": "extract", "to": "output"},
        ],
    }
    job = service.create_document_quote(
        uploads=[("brief.txt", b"Invoice total: 42 USD")], prompt="Extract the amount",
        skill_name="document-assistant", model="test:4b", sample_count=1,
        validator_count=1, workflow=workflow,
    )
    record = service._jobs[job["task"]["taskId"]]
    signed_task = SignedMessage.model_validate(record["taskMessage"])
    assert signed_task.payload.workflowHash == job["task"]["workflowHash"]
    assert signed_task.payload.workflowHash.startswith("sha256:")
    assert signed_task.payload.promptHash == content_hash("Extract the amount")
    assert job["task"]["skillNames"] == ["document-assistant", "invoice-extractor"]
    assert len(job["pricing"]["skills"]) == 2
    assert "Use the customer's request" in service.skill_details("document-assistant")["instructions"]

    service.accept_quote(job["task"]["taskId"])
    status = wait_for(service, job["task"]["taskId"], {"SETTLED", "REFUNDED", "DISPUTED"})
    assert status["state"] == "SETTLED", status.get("error")
    result = json.loads((tmp_path / ".amn" / "jobs" / job["task"]["taskId"] / "results.json").read_text())
    assert result[0]["result"]["response"].startswith("invoice-extractor:")
    invoice_inputs = [value for name, value in skill_inputs if name == "invoice-extractor"]
    assert len(invoice_inputs) == 2  # miner run and independent validator replay
    assert all("document-assistant:" in value for value in invoice_inputs)


def test_workflow_output_joins_connected_branch_leaves():
    from types import SimpleNamespace
    from ai_mining_network.orchestrator import _normalize_workflow, _run_skill_workflow

    workflow = _normalize_workflow({
        "version": 1,
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "summary", "type": "skill", "skillName": "summary"},
            {"id": "tags", "type": "skill", "skillName": "tags"},
            {"id": "output", "type": "output"},
        ],
        "edges": [
            {"from": "input", "to": "summary"}, {"from": "input", "to": "tags"},
            {"from": "summary", "to": "output"}, {"from": "tags", "to": "output"},
        ],
    })

    class Runner:
        def run(self, *, skill_name, document_text, **_kwargs):
            return {"result": f"{skill_name}:{document_text}"}

    profiles = [
        {"nodeId": "summary", "name": "summary", "instructions": "", "outputSchema": {}},
        {"nodeId": "tags", "name": "tags", "instructions": "", "outputSchema": {}},
    ]
    result = _run_skill_workflow(Runner(), profiles, workflow, "Analyze", SimpleNamespace(text="source", file_name="source.txt"))
    assert result == {"summary": {"result": "summary:source"}, "tags": {"result": "tags:source"}}


def test_general_document_task_refunds_if_signed_prompt_is_changed(tmp_path, monkeypatch):
    from ai_mining_network import orchestrator

    copy_skill(tmp_path)
    copy_document_skill(tmp_path)

    class StableRunner:
        def __init__(self, model, _url):
            self.model = model
            self.model_digest = "sha256:test-model-digest"
            self.model_info = {"parameterSize": "4B", "digest": self.model_digest}
            self.last_metrics = {"durationMs": 1, "inputTokens": 1, "outputTokens": 1}

        def run(self, **kwargs):
            return {"response": "This should not execute"}

    monkeypatch.setattr(orchestrator, "OllamaDocumentRunner", StableRunner)
    service = DemoService(tmp_path, extractor_factory=FixtureExtractor)
    job = service.create_document_quote(
        uploads=[("brief.txt", b"A short brief")], prompt="Summarize", skill_name="document-assistant",
        model="test:4b", sample_count=1, validator_count=1,
    )
    service._jobs[job["task"]["taskId"]]["prompt"] = "Altered prompt"
    service.accept_quote(job["task"]["taskId"])
    status = wait_for(service, job["task"]["taskId"], {"REFUNDED", "SETTLED", "DISPUTED"})
    assert status["state"] == "REFUNDED"
    assert "prompt changed" in status["error"]


def test_complete_100_invoice_30_sample_three_validator_demo(tmp_path):
    service = make_service(tmp_path)
    status = create_and_run(service)
    task_id = status["task"]["taskId"]
    task_dir = tmp_path / ".amn" / "jobs" / task_id
    record = service._jobs[task_id]
    assert status["state"] == "SETTLED"
    assert status["task"]["inputCount"] == 100
    assert status["sampleCount"] == 30 and status["validatorCount"] == 3
    assert record["merkleTree"]["unitCount"] == 100
    assert status["receiptCount"] == 30
    assignment_file = json.loads((task_dir / "verification-assignments.json").read_text())
    assert sorted(len(units) for units in assignment_file["assignments"].values()) == [10, 10, 10]
    events = service.audit_events(task_id)
    commitment_index = next(i for i, event in enumerate(events) if event["type"] == "execution.committed")
    assignment_index = next(i for i, event in enumerate(events) if event["type"] == "verification.assigned")
    assert commitment_index < assignment_index
    assert assignment_file["randomness"]["verified"] is False


def test_corrupted_invoice_outputs_fail_and_refund_execution_reward(tmp_path):
    service = make_service(tmp_path)
    status = create_and_run(service, invoice_count=5, sample_count=5, validator_count=2, corrupt_count=2)
    assert status["state"] == "REFUNDED"
    assert status["decision"]["outcome"] == "FAILED"
    assert status["settlement"]["outcome"] == "REFUNDED"
    assert status["settlement"]["totalChargedAtomic"] < status["quote"]["totalAtomic"]


def test_post_commit_mutation_is_detected_and_refunded(tmp_path):
    service = make_service(tmp_path)
    status = create_and_run(service, invoice_count=3, sample_count=3, validator_count=1, tamper_after_commit=True)
    assert status["state"] == "REFUNDED"
    assert any(event["type"] == "execution.post_commit_tamper_detected" for event in service.audit_events(status["task"]["taskId"]))


def test_unavailable_validator_produces_inconclusive_and_refund(tmp_path):
    service = make_service(tmp_path)
    status = create_and_run(service, invoice_count=6, sample_count=6, validator_count=3, unavailable_validators=[0])
    assert status["decision"]["outcome"] == "INCONCLUSIVE"
    assert status["state"] == "REFUNDED"


def test_conflicting_validators_leave_task_disputed_and_funds_held(tmp_path):
    service = make_service(tmp_path)
    status = create_and_run(service, invoice_count=6, sample_count=6, validator_count=3, disagree=True)
    assert status["decision"]["outcome"] == "DISPUTED"
    assert status["settlement"] is None
    assert status["state"] == "DISPUTED"


def test_execution_timeout_refunds_without_commitment(tmp_path):
    service = make_service(tmp_path)
    status = create_and_run(service, invoice_count=3, sample_count=3, validator_count=1, execution_timeout=True)
    assert status["state"] == "REFUNDED"
    assert status["commitment"] is None
    assert status["settlement"]["totalChargedAtomic"] == 0


def test_local_api_rejects_dns_rebinding_origin(tmp_path):
    import http.client
    import threading

    from ai_mining_network.web import make_server

    server = make_server(port=0, root=tmp_path)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
        connection.request("GET", "/api/health", headers={"Origin": "https://attacker.invalid"})
        response = connection.getresponse()
        assert response.status == 403
        assert "loopback" in json.loads(response.read())["error"]
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
