import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from ai_mining_network.orchestrator import DemoError, DemoService

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workflow_service():
    root = Path(tempfile.mkdtemp(prefix="amn-workflow-test-", dir=ROOT))
    try:
        shutil.copytree(ROOT / "skills" / "document-assistant", root / "skills" / "document-assistant")
        yield DemoService(root)
    finally:
        shutil.rmtree(root)


def simple_workflow():
    return {
        "version": 1,
        "nodes": [
            {"id": "input", "type": "input"},
            {"id": "skill-1", "type": "skill", "skillName": "document-assistant"},
            {"id": "output", "type": "output"},
        ],
        "edges": [{"from": "input", "to": "skill-1"}, {"from": "skill-1", "to": "output"}],
    }


def test_saved_workflow_round_trips_without_task_inputs(workflow_service):
    saved = workflow_service.save_workflow("Research summary", simple_workflow())

    assert saved["workflowId"]
    assert saved["name"] == "Research summary"
    assert saved["contentHash"].startswith("sha256:")
    assert saved["workflow"] == simple_workflow()
    assert len(saved["skills"]) == 1
    assert saved["skills"][0]["nodeId"] == "skill-1"
    assert saved["skills"][0]["name"] == "document-assistant"
    assert saved["skills"][0]["version"] == "1.0.0"
    assert saved["skills"][0]["contentHash"].startswith("sha256:")
    assert workflow_service.get_workflow(saved["workflowId"]) == saved
    assert workflow_service.list_workflows() == [saved]

    # The persisted record contains the reusable graph and skill hash only.
    connection = sqlite3.connect(workflow_service.workflow_db)
    try:
        persisted = connection.execute("SELECT workflow_json FROM workflows").fetchone()[0]
    finally:
        connection.close()
    assert "prompt" not in persisted.lower()
    assert "files" not in persisted.lower()


def test_saved_workflow_updates_are_idempotent_and_reject_unknown_ids(workflow_service):
    saved = workflow_service.save_workflow("First name", simple_workflow())
    updated = workflow_service.save_workflow("Updated name", simple_workflow(), saved["workflowId"])

    assert updated["workflowId"] == saved["workflowId"]
    assert updated["name"] == "Updated name"
    assert len(workflow_service.list_workflows()) == 1
    with pytest.raises(DemoError, match="workflow not found"):
        workflow_service.save_workflow("Unknown", simple_workflow(), "00000000-0000-0000-0000-000000000000")


@pytest.mark.parametrize("name", ["", " " * 3, "x" * 121, "bad\nname"])
def test_saved_workflow_rejects_invalid_names(workflow_service, name):
    with pytest.raises(ValueError, match="workflow name"):
        workflow_service.save_workflow(name, simple_workflow())


def test_saved_workflow_rejects_unpublished_skills(workflow_service):
    graph = simple_workflow()
    graph["nodes"][1]["skillName"] = "not-published"
    with pytest.raises(DemoError, match="skill not found"):
        workflow_service.save_workflow("Unknown skill", graph)
