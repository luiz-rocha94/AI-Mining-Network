import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_mining_network.document_tasks import (
    DocumentTaskError,
    estimate_price,
    list_ollama_models,
    store_documents,
    write_result_workbook,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tmp_path():
    directory = Path(tempfile.mkdtemp(prefix="amn-document-test-", dir=ROOT))
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def test_ollama_catalog_returns_every_installed_model_and_digest():
    tags = {"models": [
        {"name": "z-model:latest", "digest": "sha256-z", "size": 42, "details": {"parameter_size": "2B", "family": "qwen"}},
        {"model": "a-model:7b", "digest": "sha256-a", "size": 84, "details": {"parameter_size": "7B"}},
        {"name": "broken", "size": 1},
    ]}
    with patch("ai_mining_network.document_tasks.urllib.request.urlopen", return_value=FakeResponse(tags)):
        models = list_ollama_models()
    assert [item["name"] for item in models] == ["a-model:7b", "z-model:latest"]
    assert models[1]["digest"] == "sha256-z"
    assert models[1]["parameterSize"] == "2B"


def test_uploaded_document_names_are_sanitized_and_files_stay_in_private_dir(tmp_path):
    directory = tmp_path / "private-input"
    docs = store_documents(directory, [("..\\private\\notes.txt", b"summarize this")])
    assert docs[0].file_name == "notes.txt"
    assert docs[0].path.startswith(str(directory))
    assert Path(docs[0].path).read_bytes() == b"summarize this"
    with pytest.raises(DocumentTaskError, match="unsupported file type"):
        store_documents(directory / "other", [("payload.exe", b"no")])
    with pytest.raises(DocumentTaskError, match="no extractable text"):
        store_documents(directory / "empty", [("blank.txt", b" \n")])


def test_local_quote_estimate_covers_execution_and_samples(tmp_path):
    docs = store_documents(tmp_path / "inputs", [("a.txt", b"A small document."), ("b.txt", b"Another document.")])
    estimate = estimate_price(
        prompt="Summarize these", skill_instructions="Return a summary", documents=docs,
        sample_count=1, validator_count=1, model={"parameterSize": "4B"},
    )
    more_verification = estimate_price(
        prompt="Summarize these", skill_instructions="Return a summary", documents=docs,
        sample_count=2, validator_count=2, model={"parameterSize": "4B"},
    )
    assert estimate["pricingMethod"] == "local-estimate-v1"
    assert estimate["executionRewardAtomic"] > 0
    assert more_verification["verificationBudgetAtomic"] > estimate["verificationBudgetAtomic"]
    assert "not a live" in estimate["explanation"]


def test_generic_result_xlsx_escapes_formula_like_model_output(tmp_path):
    from openpyxl import load_workbook

    path = tmp_path / "output.xlsx"
    write_result_workbook([{"fileName": "=SUM(A1:A3).txt", "result": {"answer": "=1+1", "=header": "literal"}}], path)
    sheet = load_workbook(path, data_only=False).active
    assert sheet["A2"].data_type == "s" and sheet["A2"].value.startswith("'=")
    assert sheet["B1"].data_type == "s" and sheet["B1"].value.startswith("'=")
    assert sheet["C2"].data_type == "s" and sheet["C2"].value == "'=1+1"
