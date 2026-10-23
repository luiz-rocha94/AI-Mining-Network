"""Safe local document inputs, Ollama model discovery, and generic skill execution."""

from __future__ import annotations

import json
import math
import os
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openpyxl import Workbook
from pypdf import PdfReader

from .skill_packages import (
    SkillPackageError,
    validate_json_value,
    validate_schema_definition,
)
from .worker import WorkerFailure, WorkerLimits, run_isolated_skill

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TASK_BYTES = 25 * 1024 * 1024
MAX_DOCUMENTS = 100
MAX_EXTRACTED_CHARS = 250_000
MAX_TASK_EXTRACTED_CHARS = 2_000_000
SUPPORTED_EXTENSIONS = {".pdf", ".txt", ".md", ".csv", ".json"}


class DocumentTaskError(ValueError):
    pass


class SkillExecutionError(DocumentTaskError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_local_worker_requirements(requirements: dict | None) -> None:
    """Reject capabilities that the local Ollama worker cannot enforce per task."""
    if not requirements:
        return
    if requirements.get("gpuRequired") or int(requirements.get("vramBytes", 0)) > 0:
        raise SkillExecutionError(
            "UNSUPPORTED_RESOURCE_REQUIREMENT",
            "the local Ollama worker cannot reserve or meter per-task GPU/VRAM; use a compatible provider adapter",
        )
    if requirements.get("networkAccess"):
        raise SkillExecutionError(
            "UNSUPPORTED_RESOURCE_REQUIREMENT",
            "skills cannot request arbitrary network access; only the configured Ollama endpoint is available",
        )


@dataclass(frozen=True)
class DocumentInput:
    unit_id: str
    file_name: str
    path: str
    input_hash: str
    text: str


def _base_url_allowed(base_url: str) -> None:
    parsed = urlparse(base_url)
    hostname = parsed.hostname
    local = hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise DocumentTaskError(
            "Ollama endpoint must be an origin without credentials, path, or query"
        )
    if local and parsed.scheme != "http":
        raise DocumentTaskError("local Ollama endpoints must use HTTP")
    if not local and (
        parsed.scheme != "https" or os.environ.get("AMN_ALLOW_REMOTE_OLLAMA") != "1"
    ):
        raise DocumentTaskError(
            "remote Ollama requires HTTPS and AMN_ALLOW_REMOTE_OLLAMA=1 after authorizing data sharing"
        )


def list_ollama_models(
    base_url: str = "http://127.0.0.1:11434", timeout: int = 5
) -> list[dict]:
    """Return every model advertised by Ollama's documented /api/tags endpoint."""
    _base_url_allowed(base_url)
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/tags", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise DocumentTaskError(
            f"Ollama model catalog is unavailable at {base_url}: {exc}"
        ) from exc
    models = []
    for item in payload.get("models", []):
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("model")
        digest = item.get("digest")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(digest, str)
            or not digest
        ):
            continue
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        models.append(
            {
                "name": name,
                "digest": digest,
                "sizeBytes": int(item.get("size", 0) or 0),
                "parameterSize": str(details.get("parameter_size", "unknown")),
                "family": str(details.get("family", "unknown")),
                "format": str(details.get("format", "unknown")),
            }
        )
    return sorted(models, key=lambda item: item["name"].casefold())


def find_model(models: list[dict], name: str) -> dict:
    for model in models:
        if model["name"] == name:
            return model
    raise DocumentTaskError(
        f"Ollama model {name!r} is not available in the configured Ollama catalog"
    )


def _safe_filename(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    if not name or name in {".", ".."}:
        raise DocumentTaskError("each uploaded file needs a valid filename")
    if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise DocumentTaskError(
            f"unsupported file type for {name!r}; supported types: {supported}"
        )
    return name[:180]


def _extract_text(file_name: str, content: bytes) -> str:
    suffix = Path(file_name).suffix.lower()
    if not content:
        raise DocumentTaskError(f"{file_name!r} is empty")
    if len(content) > MAX_FILE_BYTES:
        raise DocumentTaskError(f"{file_name!r} exceeds the 10 MiB per-file limit")
    try:
        if suffix == ".pdf":
            reader = PdfReader(BytesIO(content), strict=True)
            if reader.is_encrypted:
                raise DocumentTaskError(f"encrypted PDF {file_name!r} is not supported")
            if len(reader.pages) > 200:
                raise DocumentTaskError(f"{file_name!r} exceeds the 200-page limit")
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        else:
            text = content.decode("utf-8-sig", errors="strict")
    except DocumentTaskError:
        raise
    except Exception as exc:
        raise DocumentTaskError(f"could not read {file_name!r}: {exc}") from exc
    text = text.strip()
    if not text:
        raise DocumentTaskError(
            f"{file_name!r} contains no extractable text; OCR and binary files are not supported"
        )
    if len(text) > MAX_EXTRACTED_CHARS:
        raise DocumentTaskError(
            f"{file_name!r} exceeds the {MAX_EXTRACTED_CHARS:,}-character text limit"
        )
    return text


def store_documents(
    directory: Path, uploads: list[tuple[str, bytes]]
) -> list[DocumentInput]:
    if len(uploads) > MAX_DOCUMENTS:
        raise DocumentTaskError(f"upload no more than {MAX_DOCUMENTS} files")
    if sum(len(data) for _, data in uploads) > MAX_TASK_BYTES:
        raise DocumentTaskError("combined uploads exceed the 25 MiB per-task limit")
    if not uploads:
        # A prompt-only request still needs one stable input unit for commitment
        # and sampled replay verification. This marker carries no customer data;
        # the signed customer prompt is separately bound into the task envelope.
        uploads = [("task-request.txt", b"No files were attached to this task.")]
    names = [_safe_filename(name) for name, _ in uploads]
    directory.mkdir(parents=True, exist_ok=True)
    result = []
    extracted_total = 0
    for index, ((_, content), name) in enumerate(zip(uploads, names), start=1):
        text = _extract_text(name, content)
        extracted_total += len(text)
        if extracted_total > MAX_TASK_EXTRACTED_CHARS:
            raise DocumentTaskError(
                "combined extracted text exceeds the 2,000,000-character per-task limit"
            )
        digest = sha256(content).hexdigest()
        unit_id = f"doc-{index:04d}-{digest[:12]}"
        path = directory / f"{index:04d}-{digest[:12]}{Path(name).suffix.lower()}"
        path.write_bytes(content)
        result.append(DocumentInput(unit_id, name, str(path), "sha256:" + digest, text))
    return result


def load_document(file_info: dict) -> DocumentInput:
    path = Path(file_info["path"])
    content = path.read_bytes()
    digest = "sha256:" + sha256(content).hexdigest()
    if digest != file_info["input_hash"]:
        raise DocumentTaskError(
            f"input artifact changed after task creation: {file_info['unit_id']}"
        )
    return DocumentInput(
        unit_id=file_info["unit_id"],
        file_name=file_info["file_name"],
        path=str(path),
        input_hash=digest,
        text=_extract_text(file_info["file_name"], content),
    )


def estimate_price(
    *,
    prompt: str,
    skill_instructions: str,
    documents: list[DocumentInput],
    sample_count: int,
    validator_count: int,
    model: dict,
) -> dict:
    """Transparent local TESTUSD estimate; this is a rate-card estimate, not an offer from a real network."""
    chars_by_document = [
        len(item.text) + len(prompt) + len(skill_instructions) for item in documents
    ]
    input_tokens = sum(math.ceil(chars / 4) for chars in chars_by_document)
    output_tokens = max(
        128, min(1200, math.ceil(input_tokens / max(1, len(documents)) * 0.35))
    ) * len(documents)
    sampled_input_tokens = math.ceil(
        sum(chars_by_document) * sample_count / len(documents) / 4
    )
    parameter_size = model.get("parameterSize", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*[Bb]", str(parameter_size))
    parameter_billions = float(match.group(1)) if match else 4.0
    estimated_tokens_per_second = max(2.0, 70.0 / max(parameter_billions, 0.5))
    p50_ms = math.ceil(
        (input_tokens + output_tokens) / estimated_tokens_per_second * 1000
    )
    p90_ms = math.ceil(p50_ms * 1.7)
    execution_atomic = 30_000 + input_tokens * 4 + output_tokens * 12
    verification_atomic = (
        20_000 * validator_count + sampled_input_tokens * 3 * validator_count
    )
    # The current local offer adapter sets no extra protocol fee. Keep the
    # estimate breakdown aligned with the fixed_quote result.
    protocol_fee_atomic = 0
    return {
        "pricingMethod": "local-estimate-v1",
        "currency": "TESTUSD",
        "inputTokensEstimated": input_tokens,
        "outputTokensEstimated": output_tokens,
        "sampledInputTokensEstimated": sampled_input_tokens,
        "estimatedTokensPerSecond": round(estimated_tokens_per_second),
        "p50DurationMs": p50_ms,
        "p90DurationMs": p90_ms,
        "executionRewardAtomic": execution_atomic,
        "verificationBudgetAtomic": verification_atomic,
        "protocolFeeAtomic": protocol_fee_atomic,
        "storageBudgetAtomic": 0,
        "settlementBudgetAtomic": 0,
        "selfReportedOrEstimated": True,
        "assumptions": {
            "pricingMethod": "local-estimate-v1",
            "inputTokenCharactersPerToken": 4,
            "modelThroughput": "70 / parameter-billions tokens per second, floored at 2",
            "executionRateAtomicPerInputToken": 4,
            "executionRateAtomicPerOutputToken": 12,
            "verificationBaseAtomicPerValidator": 20_000,
            "storageBudgetAtomic": 0,
            "settlementBudgetAtomic": 0,
            "zeroCostReasons": [
                "local fixture storage has no metered provider charge",
                "local SQLite settlement has no transaction or gas charge",
                "local development protocol fee is configured as zero",
            ],
        },
        "explanation": "Calculated from extracted text size, a model-size throughput heuristic, verification sample count, and the local TESTUSD rate card. It is not a live decentralized miner offer or a benchmark guarantee.",
    }


class OllamaDocumentRunner:
    """Runs a declarative SKILL.md prompt through Ollama; it never executes skill package code."""

    def __init__(
        self, model: str, base_url: str = "http://127.0.0.1:11434", timeout: int = 600
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        _base_url_allowed(self.base_url)
        self.model_info = find_model(list_ollama_models(self.base_url), model)
        self.model_digest = self.model_info["digest"]

    def run(
        self,
        *,
        skill_name: str,
        skill_instructions: str,
        prompt: str,
        document_name: str,
        document_text: str,
        output_schema: dict,
        input_schema: dict | None = None,
        input_values: dict | None = None,
        allowed_tools: list[str] | None = None,
        requirements: dict | None = None,
        timeout_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
        scratch_root: Path | None = None,
    ) -> Any:
        schema = validate_schema_definition(output_schema, "skill outputSchema")
        inputs_schema = input_schema or {
            "type": "object",
            "properties": {
                "customerPrompt": {"type": "string"},
                "documentName": {"type": "string"},
                "documentText": {"type": "string"},
            },
            "required": ["customerPrompt", "documentName", "documentText"],
            "additionalProperties": False,
        }
        values = (
            input_values
            if input_values is not None
            else {
                "customerPrompt": prompt,
                "documentName": document_name,
                "documentText": document_text,
            }
        )
        try:
            validate_json_value(values, inputs_schema, "skill input")
        except SkillPackageError as exc:
            raise SkillExecutionError("INVALID_SKILL_INPUT", str(exc)) from exc
        requested_timeout = (
            self.timeout if timeout_seconds is None else float(timeout_seconds)
        )
        timeout = min(requested_timeout, float(self.timeout), 600.0)
        if timeout <= 0:
            raise SkillExecutionError(
                "DEADLINE_EXCEEDED", "skill deadline has already expired"
            )
        try:
            validate_local_worker_requirements(requirements)
        except SkillExecutionError:
            raise
        except (TypeError, ValueError) as exc:
            raise SkillExecutionError(
                "INVALID_RESOURCE_REQUIREMENT",
                f"invalid local worker requirements: {exc}",
            ) from exc
        payload = {
            "model": self.model,
            "modelDigest": self.model_digest,
            "baseUrl": self.base_url,
            "allowRemote": os.environ.get("AMN_ALLOW_REMOTE_OLLAMA") == "1",
            "skillName": skill_name,
            "skillInstructions": skill_instructions,
            "customerPrompt": prompt,
            "inputValues": values,
            "inputSchema": inputs_schema,
            "outputSchema": schema,
            "allowedTools": list(allowed_tools or []),
            "timeoutSeconds": math.ceil(timeout),
        }
        try:
            result = run_isolated_skill(
                payload,
                timeout_seconds=timeout,
                cancel_event=cancel_event,
                scratch_root=scratch_root,
                limits=WorkerLimits(timeout_seconds=timeout),
            )
        except WorkerFailure as exc:
            raise SkillExecutionError(exc.code, exc.message) from exc
        self.last_metrics = result["metrics"]
        return result["output"]


def _validate_schema(value: Any, schema: dict, path: str) -> None:
    if "enum" in schema and value not in schema["enum"]:
        raise DocumentTaskError(
            f"model output at {path} is not one of the allowed values"
        )
    if "const" in schema and value != schema["const"]:
        raise DocumentTaskError(
            f"model output at {path} does not match the required constant"
        )
    expected = schema.get("type")
    type_ok = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "number": lambda item: (
            isinstance(item, (int, float)) and not isinstance(item, bool)
        ),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }.get(expected)
    if type_ok and not type_ok(value):
        raise DocumentTaskError(f"model output at {path} must be a JSON {expected}")
    if isinstance(value, float):
        raise DocumentTaskError(
            f"model output at {path} contains a floating-point number; use an exact decimal string"
        )
    if expected == "string":
        if len(value) < int(schema.get("minLength", 0)):
            raise DocumentTaskError(f"model output at {path} is shorter than minLength")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise DocumentTaskError(f"model output at {path} is longer than maxLength")
        if "pattern" in schema and not re.search(str(schema["pattern"]), value):
            raise DocumentTaskError(
                f"model output at {path} does not match its required pattern"
            )
        if schema.get("format") == "date":
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise DocumentTaskError(
                    f"model output at {path} is not an ISO date"
                ) from exc
    if expected in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise DocumentTaskError(f"model output at {path} is below its minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise DocumentTaskError(f"model output at {path} is above its maximum")
    if expected == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise DocumentTaskError(
                    f"model output is missing required field {path}.{key}"
                )
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                raise DocumentTaskError(
                    f"model output contains unexpected fields at {path}: {sorted(extra)}"
                )
        for key, item in value.items():
            if key in properties:
                _validate_schema(item, properties[key], f"{path}.{key}")
            elif isinstance(schema.get("additionalProperties"), dict):
                _validate_schema(item, schema["additionalProperties"], f"{path}.{key}")
    elif expected == "array" and "items" in schema:
        if len(value) < int(schema.get("minItems", 0)):
            raise DocumentTaskError(f"model output at {path} has too few array items")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise DocumentTaskError(f"model output at {path} has too many array items")
        for index, item in enumerate(value):
            _validate_schema(item, schema["items"], f"{path}[{index}]")


def write_result_workbook(records: list[dict], path: Path) -> None:
    keys = sorted({key for record in records for key in record["result"]})

    def safe_cell(value: Any) -> Any:
        if isinstance(value, str) and value.lstrip().startswith(
            ("=", "+", "-", "@", "\t", "\r")
        ):
            return "'" + value
        return value

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Results"
    sheet.append(["File name", *(safe_cell(key) for key in keys)])
    for record in records:
        row = [safe_cell(record["fileName"])]
        for key in keys:
            value = record["result"].get(key, "")
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            row.append(safe_cell(value))
        sheet.append(row)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for index, column in enumerate(sheet.columns, start=1):
        letter = column[0].column_letter
        width = min(
            48,
            max(
                14, max((len(str(cell.value or "")) for cell in column), default=0) + 2
            ),
        )
        sheet.column_dimensions[letter].width = width
    workbook.save(path)
