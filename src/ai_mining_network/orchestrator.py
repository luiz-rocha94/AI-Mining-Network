"""Single-host local network harness for the complete invoice MVP workflow.

This adapter composes independently signed customer/miner/validator records on
one host. It is a repeatable development network, not the production P2P mesh.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from eth_account import Account

from .benchmark_service import run_invoice_benchmark
from .canonical import canonical_json_bytes, content_hash, sha256_hex
from .document_tasks import (
    DocumentTaskError,
    OllamaDocumentRunner,
    estimate_price,
    list_ollama_models,
    load_document,
    store_documents,
    write_result_workbook,
)
from .identity import load_or_create_private_key
from .invoice import (
    ExtractedInvoice,
    Extractor,
    OllamaExtractor,
    generate_synthetic_invoices,
    load_invoice_file,
    normalize_record,
    write_workbook,
)
from .merkle import LEAF_ENCODING, build_tree
from .models import (
    Currency,
    CustomerTask,
    ExecutionCommitment,
    ExecutionContract,
    FixedPriceQuote,
    MinerOffer,
    OfferCapabilities,
    PaymentSettlement,
    ResourceRequirements,
    SignedMessage,
    SkillRating,
    VerificationAssignment,
    VerificationDecision,
    VerificationReceipt,
)
from .pricing import currency_matches, fixed_quote, local_fixture_offer_prices
from .randomness import SimulatedBeacon, challenge_seed, select_units, select_validators
from .registry import LocalSkillRegistry, read_skill, read_skill_instructions
from .settlement import create_settlement, settle_once, validate_settlement_within_quote
from .signing import create_message, verify_message
from .skill_packages import SkillPackageError, validate_json_value
from .state_machine import EventLog, InvalidTransition, validate_transition
from .verification import decide, verify_invoice_unit


class DemoError(RuntimeError):
    pass


def _json_file(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )


def _hash_json(value: object) -> str:
    return sha256_hex(canonical_json_bytes(value))


def _hash_unit_id(value: str) -> str:
    return "0x" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _skill_chain_schema(profiles: list[dict]) -> dict:
    if len(profiles) == 1:
        return profiles[0]["outputSchema"]
    names = [profile["name"] for profile in profiles]
    return {
        "type": "object",
        "properties": {
            profile["name"]: profile["outputSchema"] for profile in profiles
        },
        "required": names,
        "additionalProperties": False,
    }


def _skill_input_schema(profile: dict) -> dict:
    return profile.get("inputSchema") or {
        "type": "object",
        "properties": {
            "customerPrompt": {"type": "string"},
            "documentName": {"type": "string"},
            "documentText": {"type": "string"},
        },
        "required": ["customerPrompt", "documentName", "documentText"],
        "additionalProperties": False,
    }


def _skill_input_values(
    profile: dict, prompt: str, document, source_node: str, source_value: object
) -> dict:
    source_text = (
        source_value
        if isinstance(source_value, str)
        else json.dumps(
            {"upstreamNode": source_node, "output": source_value},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    available = {
        "customerPrompt": prompt,
        "prompt": prompt,
        "documentName": document.file_name,
        "documentText": source_text,
        "invoiceId": Path(document.file_name).stem,
        "sourceNode": source_node,
        "sourceValue": source_value,
    }
    schema = _skill_input_schema(profile)
    properties = schema.get("properties", {})
    values = {name: available[name] for name in properties if name in available}
    try:
        validate_json_value(values, schema, f"skill {profile['name']} input")
    except SkillPackageError as exc:
        raise DemoError(str(exc)) from exc
    return values


def _run_skill_chain(
    runner: OllamaDocumentRunner, profiles: list[dict], prompt: str, document
) -> dict:
    output = {}
    current_text = document.text
    source_node = "input"
    for profile in profiles:
        input_values = _skill_input_values(
            profile, prompt, document, source_node, current_text
        )
        skill_output = runner.run(
            skill_name=profile["name"],
            skill_instructions=profile["instructions"],
            prompt=prompt,
            document_name=document.file_name,
            document_text=input_values.get("documentText", current_text),
            input_schema=_skill_input_schema(profile),
            input_values=input_values,
            output_schema=profile["outputSchema"],
            allowed_tools=profile.get("allowedTools", []),
            requirements=profile.get("requirements"),
        )
        output[profile["name"]] = skill_output
        current_text = json.dumps(
            {"previousSkill": profile["name"], "output": skill_output},
            ensure_ascii=False,
            sort_keys=True,
        )
        source_node = profile["nodeId"]
    return output[profiles[0]["name"]] if len(profiles) == 1 else output


def _normalize_workflow(workflow: object) -> dict:
    """Validate the v1 workflow graph: fixed input/output, one parent per skill, output joins leaves."""
    if not isinstance(workflow, dict):
        raise ValueError("workflow must be an object")
    if workflow.get("version") != 1:
        raise ValueError("unsupported workflow version")
    nodes = workflow.get("nodes")
    edges = workflow.get("edges")
    if (
        not isinstance(nodes, list)
        or not isinstance(edges, list)
        or not 3 <= len(nodes) <= 12
    ):
        raise ValueError(
            "workflow must contain input, output, and one to ten skill nodes"
        )
    if len(edges) > 20:
        raise ValueError("workflow has too many connections")
    normalized_nodes = []
    by_id = {}
    skill_names = set()
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise ValueError("workflow nodes must be objects")
        node_id = node.get("id")
        node_type = node.get("type")
        if not isinstance(node_id, str) or not re.fullmatch(
            r"[a-zA-Z0-9_-]{1,64}", node_id
        ):
            raise ValueError(
                "workflow node ids must be 1–64 ASCII letters, digits, underscores, or hyphens"
            )
        if not isinstance(node_type, str):
            raise ValueError("workflow nodes need a supported string type")
        if node_id in by_id:
            raise ValueError(f"duplicate workflow node id: {node_id}")
        if index == 0 and (node_id != "input" or node_type != "input"):
            raise ValueError("the input node must be first and use id 'input'")
        if index == len(nodes) - 1 and (node_id != "output" or node_type != "output"):
            raise ValueError("the output node must be last and use id 'output'")
        if node_type == "skill":
            name = node.get("skillName")
            if not isinstance(name, str) or not name or name != name.strip():
                raise ValueError("skill workflow nodes need a non-empty skillName")
            if name in skill_names:
                raise ValueError("a skill can appear only once in this workflow")
            skill_names.add(name)
            normalized = {"id": node_id, "type": "skill", "skillName": name}
        elif node_type in {"input", "output"}:
            expected_id = "input" if node_type == "input" else "output"
            if node_id != expected_id:
                raise ValueError(f"the {node_type} node must use id '{expected_id}'")
            normalized = {"id": node_id, "type": node_type}
        else:
            raise ValueError(f"unsupported workflow node type: {node_type}")
        normalized_nodes.append(normalized)
        by_id[node_id] = normalized
    if len(skill_names) != len(nodes) - 2:
        raise ValueError(
            "workflow must contain exactly one input, one output, and one to ten skill nodes"
        )

    normalized_edges = []
    incoming: dict[str, list[str]] = {node["id"]: [] for node in normalized_nodes}
    seen_edges = set()
    node_order = {node["id"]: index for index, node in enumerate(normalized_nodes)}
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError("workflow edges must be objects")
        source, target = edge.get("from"), edge.get("to")
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source not in by_id
            or target not in by_id
            or source == target
        ):
            raise ValueError("workflow edges must connect two distinct existing nodes")
        key = (source, target)
        if key in seen_edges:
            raise ValueError("duplicate workflow edge")
        if by_id[source]["type"] == "output" or by_id[target]["type"] == "input":
            raise ValueError("workflow edges cannot leave output or enter input")
        if (
            by_id[target]["type"] == "skill"
            and node_order[source] >= node_order[target]
        ):
            raise ValueError(
                "skill nodes can connect only from input or an earlier skill node"
            )
        seen_edges.add(key)
        incoming[target].append(source)
        normalized_edges.append({"from": source, "to": target})

    skill_nodes = [node for node in normalized_nodes if node["type"] == "skill"]
    for node in skill_nodes:
        if len(incoming[node["id"]]) != 1:
            raise ValueError(
                f"skill node {node['id']} must have exactly one incoming connection"
            )
    if any(not incoming[node["id"]] for node in skill_nodes):
        raise ValueError("every skill node must be connected to the input")
    has_skill_child = {
        source
        for target, sources in incoming.items()
        if by_id[target]["type"] == "skill"
        for source in sources
    }
    leaves = {node["id"] for node in skill_nodes if node["id"] not in has_skill_child}
    if set(incoming["output"]) != leaves:
        raise ValueError("the output node must connect from every terminal skill node")
    if not leaves:
        raise ValueError("connect at least one skill node to the output")
    return {"version": 1, "nodes": normalized_nodes, "edges": normalized_edges}


def _workflow_hash(workflow: dict, profiles: list[dict]) -> str:
    by_node = {profile["nodeId"]: profile for profile in profiles}
    nodes = []
    for node in workflow["nodes"]:
        if node["type"] == "skill":
            profile = by_node[node["id"]]
            nodes.append({**node, "skillHash": profile["contentHash"]})
        else:
            nodes.append(node)
    return content_hash(
        {"version": workflow["version"], "nodes": nodes, "edges": workflow["edges"]}
    )


def _workflow_output_schema(profiles: list[dict], workflow: dict) -> dict:
    profile_by_node = {profile["nodeId"]: profile for profile in profiles}
    terminal_ids = [
        edge["from"] for edge in workflow["edges"] if edge["to"] == "output"
    ]
    if len(terminal_ids) == 1:
        return profile_by_node[terminal_ids[0]]["outputSchema"]
    terminal_profiles = [profile_by_node[node_id] for node_id in terminal_ids]
    return {
        "type": "object",
        "properties": {
            profile["name"]: profile["outputSchema"] for profile in terminal_profiles
        },
        "required": [profile["name"] for profile in terminal_profiles],
        "additionalProperties": False,
    }


def _runtime_value_preview(value, *, depth: int = 0):
    """Bound live inspector values; previews are never written to the job record on disk."""
    if isinstance(value, str):
        limit = 1800
        return (
            value
            if len(value) <= limit
            else {
                "preview": value[:limit] + "…",
                "truncated": True,
                "totalCharacters": len(value),
            }
        )
    if isinstance(value, dict):
        if depth >= 4:
            return {"preview": "Object preview depth limit reached", "truncated": True}
        items = list(value.items())
        preview = {
            str(key): _runtime_value_preview(item, depth=depth + 1)
            for key, item in items[:24]
        }
        if len(items) > 24:
            preview["_previewNote"] = f"{len(items) - 24} additional fields omitted"
        return preview
    if isinstance(value, (list, tuple)):
        if depth >= 4:
            return [{"preview": "Array preview depth limit reached", "truncated": True}]
        preview = [_runtime_value_preview(item, depth=depth + 1) for item in value[:8]]
        if len(value) > 8:
            preview.append(
                {"_previewNote": f"{len(value) - 8} additional items omitted"}
            )
        return preview
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text if len(text) <= 300 else text[:300] + "…"


def _mark_pending_nodes_failed(record: dict) -> None:
    execution = record.get("nodeExecution")
    if not isinstance(execution, dict):
        return
    execution["currentNodeId"] = None
    for node in execution.get("nodes", []):
        if node.get("status") in {"QUEUED", "RUNNING", "IN_PROGRESS"}:
            node["status"] = "FAILED"
            node["currentUnit"] = None


def _run_skill_workflow(
    runner: OllamaDocumentRunner,
    profiles: list[dict],
    workflow: dict,
    prompt: str,
    document,
    *,
    on_node_update=None,
    unit_index: int = 1,
    unit_count: int = 1,
):
    """Run connected skill nodes in order; terminal values form the final Output node."""
    profile_by_node = {profile["nodeId"]: profile for profile in profiles}
    input_by_node: dict[str, list[str]] = {node["id"]: [] for node in workflow["nodes"]}
    for edge in workflow["edges"]:
        input_by_node[edge["to"]].append(edge["from"])
    values = {"input": document.text}
    for node in workflow["nodes"]:
        if node["type"] != "skill":
            continue
        source_id = input_by_node[node["id"]][0]
        source_value = values[source_id]
        profile = profile_by_node[node["id"]]
        input_values = _skill_input_values(
            profile, prompt, document, source_id, source_value
        )
        source_text = input_values.get(
            "documentText",
            source_value
            if isinstance(source_value, str)
            else json.dumps(
                {"upstreamNode": source_id, "output": source_value},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        node_input = {
            "prompt": prompt,
            "documentName": document.file_name,
            "sourceNode": source_id,
            "sourceValue": source_value,
            "skillInputs": input_values,
        }
        if on_node_update:
            on_node_update(
                node["id"],
                profile["name"],
                "RUNNING",
                unit_index,
                unit_count,
                node_input,
                None,
            )
        try:
            values[node["id"]] = runner.run(
                skill_name=profile["name"],
                skill_instructions=profile["instructions"],
                prompt=prompt,
                document_name=document.file_name,
                document_text=source_text,
                input_schema=_skill_input_schema(profile),
                input_values=input_values,
                output_schema=profile["outputSchema"],
                allowed_tools=profile.get("allowedTools", []),
                requirements=profile.get("requirements"),
            )
        except Exception:
            if on_node_update:
                on_node_update(
                    node["id"],
                    profile["name"],
                    "FAILED",
                    unit_index,
                    unit_count,
                    node_input,
                    None,
                )
            raise
        if on_node_update:
            status = "COMPLETED" if unit_index >= unit_count else "IN_PROGRESS"
            on_node_update(
                node["id"],
                profile["name"],
                status,
                unit_index,
                unit_count,
                node_input,
                values[node["id"]],
            )
    terminal_ids = input_by_node["output"]
    if len(terminal_ids) == 1:
        return values[terminal_ids[0]]
    return {
        profile_by_node[node_id]["name"]: values[node_id] for node_id in terminal_ids
    }


class DemoService:
    def __init__(
        self,
        root: Path,
        ollama_model: str = "qwen2.5:1.5b",
        ollama_url: str = "http://127.0.0.1:11434",
        extractor_factory=None,
    ):
        self.root = root.resolve()
        self.skill_dir = self.root / "skills" / "invoice-extractor"
        self.state_dir = self.root / ".amn"
        self.jobs_dir = self.state_dir / "jobs"
        self.state_dir.mkdir(exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.model = ollama_model
        self.ollama_url = ollama_url
        # Tests may inject the explicitly test-only FixtureExtractor. The CLI
        # and web app use OllamaExtractor and never silently fall back.
        self.extractor_factory = extractor_factory or (
            lambda: OllamaExtractor(self.model, self.ollama_url)
        )
        self.event_log = EventLog(self.state_dir / "state.sqlite3")
        self.registry = LocalSkillRegistry(self.state_dir / "registry.sqlite3")
        self.workflow_db = self.state_dir / "workflows.sqlite3"
        with closing(sqlite3.connect(self.workflow_db)) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS workflows (
                    workflow_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    workflow_json TEXT NOT NULL,
                    skill_refs_json TEXT NOT NULL DEFAULT '[]',
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(workflows)")
            }
            if "skill_refs_json" not in columns:
                connection.execute(
                    "ALTER TABLE workflows ADD COLUMN skill_refs_json TEXT NOT NULL DEFAULT '[]'"
                )
            connection.commit()
        self._jobs: dict[str, dict] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

    def health(self) -> dict:
        skill = read_skill(self.skill_dir)
        result = {
            "ok": True,
            "skill": skill["name"],
            "skillHash": skill["contentHash"],
            "ollama": "unavailable",
            "model": self.model,
        }
        try:
            models = list_ollama_models(self.ollama_url, timeout=5)
            selected = next(
                (item for item in models if item["name"] == self.model),
                models[0] if models else None,
            )
            if selected:
                result.update(
                    {
                        "ollama": "ready",
                        "model": selected["name"],
                        "modelDigest": selected["digest"],
                        "availableModelCount": len(models),
                    }
                )
            else:
                result["ollamaError"] = (
                    "Ollama is reachable but has no installed models"
                )
        except ValueError as exc:
            result["ollamaError"] = str(exc)
        return result

    def list_models(self) -> dict:
        try:
            models = list_ollama_models(self.ollama_url)
            return {"available": True, "models": models}
        except DocumentTaskError as exc:
            return {"available": False, "models": [], "error": str(exc)}

    def list_skills(self) -> list[dict]:
        skills_root = self.root / "skills"
        skills = []
        if not skills_root.is_dir():
            return skills
        for directory in sorted(
            path for path in skills_root.iterdir() if path.is_dir()
        ):
            try:
                manifest = self.registry.publish(directory)
            except (OSError, ValueError) as exc:
                print(f"[amn] skipping invalid skill package {directory.name}: {exc}")
                continue
            skills.append(
                {
                    "name": manifest["name"],
                    "version": manifest["version"],
                    "contentHash": manifest["contentHash"],
                    "description": manifest.get("description", ""),
                    "requiredModels": manifest.get("requiredModels", []),
                    "verificationStrategy": manifest.get(
                        "verificationStrategy", "per-document"
                    ),
                }
            )
        return skills

    def skill_details(self, name: str) -> dict:
        """Return the public skill text for the local node-library inspector."""
        skills_root = (self.root / "skills").resolve()
        directories = (
            sorted(path for path in skills_root.iterdir() if path.is_dir())
            if skills_root.is_dir()
            else []
        )
        for directory in directories:
            package = directory.resolve()
            try:
                package.relative_to(skills_root)
            except ValueError:
                continue
            try:
                manifest = self.registry.publish(package)
            except (OSError, ValueError):
                continue
            if manifest["name"] != name:
                continue
            skill_file = (package / manifest.get("skillFile", "SKILL.md")).resolve()
            try:
                skill_file.relative_to(package)
            except ValueError as exc:
                raise DemoError(
                    "skill instructions escaped the published package"
                ) from exc
            if not skill_file.is_file() or skill_file.stat().st_size > 1_000_000:
                raise DemoError(
                    "skill instructions are missing or exceed the size limit"
                )
            return {
                "name": manifest["name"],
                "version": manifest["version"],
                "contentHash": manifest["contentHash"],
                "description": manifest.get("description", ""),
                "instructions": skill_file.read_text(encoding="utf-8"),
                "manifest": manifest,
            }
        raise DemoError(f"skill not found: {name}")

    def list_workflows(self) -> list[dict]:
        """List locally saved workflow templates; task prompts and files are never stored here."""
        with closing(sqlite3.connect(self.workflow_db)) as connection:
            rows = connection.execute(
                "SELECT workflow_id, name, workflow_json, skill_refs_json, content_hash, created_at, updated_at "
                "FROM workflows ORDER BY updated_at DESC"
            ).fetchall()
        return [self._workflow_response(row) for row in rows]

    def get_workflow(self, workflow_id: str) -> dict:
        with closing(sqlite3.connect(self.workflow_db)) as connection:
            row = connection.execute(
                "SELECT workflow_id, name, workflow_json, skill_refs_json, content_hash, created_at, updated_at "
                "FROM workflows WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
        if row is None:
            raise DemoError(f"workflow not found: {workflow_id}")
        return self._workflow_response(row)

    @staticmethod
    def _workflow_response(row: tuple) -> dict:
        return {
            "workflowId": row[0],
            "name": row[1],
            "workflow": json.loads(row[2]),
            "skills": json.loads(row[3]),
            "contentHash": row[4],
            "createdAt": row[5],
            "updatedAt": row[6],
        }

    def save_workflow(
        self, name: str, workflow: object, workflow_id: str | None = None
    ) -> dict:
        """Persist a public workflow template without retaining any task input or customer data."""
        if not isinstance(name, str):
            raise ValueError("workflow name must be text")
        name = name.strip()
        if not name or len(name) > 120 or any(ord(char) < 32 for char in name):
            raise ValueError("workflow name must be 1–120 printable characters")
        normalized = _normalize_workflow(workflow)
        skills = []
        for node in normalized["nodes"]:
            if node["type"] == "skill":
                details = self.skill_details(node["skillName"])
                skills.append(
                    {
                        "nodeId": node["id"],
                        "name": details["name"],
                        "version": details["version"],
                        "contentHash": details["contentHash"],
                    }
                )
        content_hash_value = _hash_json(
            {"name": name, "workflow": normalized, "skills": skills}
        )
        now = datetime.now(timezone.utc).isoformat()
        requested_workflow_id = workflow_id is not None
        workflow_id = workflow_id or str(uuid4())
        try:
            if not isinstance(workflow_id, str):
                raise ValueError
            workflow_id = str(UUID(workflow_id))
        except ValueError:
            raise ValueError("workflowId must be a UUID")
        with closing(sqlite3.connect(self.workflow_db)) as connection:
            existing = connection.execute(
                "SELECT created_at FROM workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            if requested_workflow_id and existing is None:
                raise DemoError(f"workflow not found: {workflow_id}")
            created_at = existing[0] if existing else now
            connection.execute(
                "INSERT INTO workflows (workflow_id, name, workflow_json, skill_refs_json, content_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(workflow_id) DO UPDATE SET "
                "name=excluded.name, workflow_json=excluded.workflow_json, skill_refs_json=excluded.skill_refs_json, "
                "content_hash=excluded.content_hash, updated_at=excluded.updated_at",
                (
                    workflow_id,
                    name,
                    json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(skills, ensure_ascii=False, separators=(",", ":")),
                    content_hash_value,
                    created_at,
                    now,
                ),
            )
            connection.commit()
        return self.get_workflow(workflow_id)

    def create_document_quote(
        self,
        uploads: list[tuple[str, bytes]],
        prompt: str,
        skill_name: str,
        model: str,
        sample_count: int,
        validator_count: int,
        skill_names: list[str] | None = None,
        workflow: dict | None = None,
    ) -> dict:
        if not prompt.strip() or len(prompt) > 20_000:
            raise ValueError("prompt must contain 1–20,000 characters")
        normalized_workflow = (
            _normalize_workflow(workflow) if workflow is not None else None
        )
        if workflow is not None and skill_names is not None:
            raise ValueError("send a workflow graph or a legacy skill list, not both")
        if normalized_workflow is not None:
            skill_specs = [
                {"nodeId": node["id"], "name": node["skillName"]}
                for node in normalized_workflow["nodes"]
                if node["type"] == "skill"
            ]
            selected_names = [spec["name"] for spec in skill_specs]
        else:
            selected_names = (
                list(skill_names) if skill_names is not None else [skill_name]
            )
            if not 1 <= len(selected_names) <= 10:
                raise ValueError("select between one and ten skills")
            if any(
                not isinstance(name, str) or not name or name != name.strip()
                for name in selected_names
            ):
                raise ValueError("skill names must be non-empty and unique")
            if len(set(selected_names)) != len(selected_names):
                raise ValueError("skill names must be non-empty and unique")
            if any(not name.isascii() for name in selected_names):
                raise ValueError("skill names must use ASCII characters")
            skill_line = ". ".join(selected_names)
            skill_specs = [
                {"nodeId": f"legacy-skill-{index + 1}", "name": name}
                for index, name in enumerate(selected_names)
            ]
            if skill_names is not None:
                prompt = f"{skill_line}\n\n{prompt.strip()}"
                if len(prompt) > 20_000:
                    raise ValueError(
                        "skill names and task prompt exceed 20,000 characters"
                    )
        unit_count = max(1, len(uploads))
        if not 1 <= sample_count <= unit_count:
            raise ValueError(
                "verification samples must be between one and the task unit count"
            )
        if not 1 <= validator_count <= min(10, sample_count):
            raise ValueError(
                "validators must be between one and the sample count (maximum 10)"
            )
        available = {item["name"] for item in self.list_skills()}
        skill_profiles = []
        for spec in skill_specs:
            selected_name = spec["name"]
            if selected_name not in available:
                raise ValueError(f"unknown skill: {selected_name}")
            skill_dir = (self.root / "skills" / selected_name).resolve()
            try:
                skill_dir.relative_to((self.root / "skills").resolve())
            except ValueError as exc:
                raise ValueError("unknown skill") from exc
            skill = self.registry.publish(skill_dir)
            skill_file = (skill_dir / skill["skillFile"]).resolve()
            try:
                skill_file.relative_to(skill_dir.resolve())
            except ValueError as exc:
                raise ValueError(
                    "skillFile must remain inside the published skill package"
                ) from exc
            if not skill_file.is_file() or skill_file.stat().st_size > 1_000_000:
                raise ValueError("skill instructions must be a file smaller than 1 MiB")
            skill_profiles.append(
                {
                    "nodeId": spec["nodeId"],
                    "name": selected_name,
                    "contentHash": skill["contentHash"],
                    "directory": str(skill_dir),
                    "instructions": read_skill_instructions(skill_dir),
                    "inputSchema": skill["inputSchema"],
                    "outputSchema": skill.get("outputSchema", {}),
                    "allowedTools": skill["allowedTools"],
                    "requirements": skill["requirements"],
                }
            )
        output_schema = (
            _workflow_output_schema(skill_profiles, normalized_workflow)
            if normalized_workflow
            else _skill_chain_schema(skill_profiles)
        )
        skill_hash = (
            skill_profiles[0]["contentHash"]
            if len(skill_profiles) == 1
            else content_hash(
                [
                    {"name": item["name"], "contentHash": item["contentHash"]}
                    for item in skill_profiles
                ]
            )
        )
        workflow_hash = (
            _workflow_hash(normalized_workflow, skill_profiles)
            if normalized_workflow
            else None
        )
        runner = OllamaDocumentRunner(model, self.ollama_url)
        task_id = str(uuid4())
        task_dir = self.jobs_dir / task_id
        documents = store_documents(task_dir / "private-input", uploads)
        skill_estimates = []
        for profile in skill_profiles:
            estimate = estimate_price(
                prompt=prompt,
                skill_instructions=profile["instructions"],
                documents=documents,
                sample_count=sample_count,
                validator_count=validator_count,
                model=runner.model_info,
            )
            skill_estimates.append(
                {
                    "nodeId": profile["nodeId"],
                    "name": profile["name"],
                    "skillHash": profile["contentHash"],
                    "executionRewardAtomic": estimate["executionRewardAtomic"],
                    "verificationBudgetAtomic": estimate["verificationBudgetAtomic"],
                    "protocolFeeAtomic": estimate["protocolFeeAtomic"],
                    "storageBudgetAtomic": estimate["storageBudgetAtomic"],
                    "settlementBudgetAtomic": estimate["settlementBudgetAtomic"],
                    "inputTokensEstimated": estimate["inputTokensEstimated"],
                    "outputTokensEstimated": estimate["outputTokensEstimated"],
                    "sampledInputTokensEstimated": estimate[
                        "sampledInputTokensEstimated"
                    ],
                    "estimatedTokensPerSecond": estimate["estimatedTokensPerSecond"],
                    "p50DurationMs": estimate["p50DurationMs"],
                    "p90DurationMs": estimate["p90DurationMs"],
                }
            )
        summed_fields = (
            "executionRewardAtomic",
            "verificationBudgetAtomic",
            "protocolFeeAtomic",
            "storageBudgetAtomic",
            "settlementBudgetAtomic",
            "inputTokensEstimated",
            "outputTokensEstimated",
            "sampledInputTokensEstimated",
            "p50DurationMs",
            "p90DurationMs",
        )
        pricing = {
            "pricingMethod": "sum-of-skill-estimates-v2",
            "currency": "TESTUSD",
            "priceBasis": "local-fixture",
            **{
                field: sum(item[field] for item in skill_estimates)
                for field in summed_fields
            },
            "estimatedTokensPerSecond": skill_estimates[0]["estimatedTokensPerSecond"],
            "selfReportedOrEstimated": True,
            "skills": skill_estimates,
            "assumptions": {
                "source": "local fixture offers generated from the per-skill rate card",
                "benchmark": "no independent task-specific benchmark is available for arbitrary customer workflows",
                "storageAndSettlement": "zero-priced by the local SQLite/filesystem adapter",
                "repriceOnOfferChange": True,
            },
            "explanation": "The fixed total sums the local TESTUSD estimate for every skill node, including verification work. This is not a live miner offer or benchmark guarantee.",
        }
        customer = Account.from_key(load_or_create_private_key(self.root, "customer"))
        task = CustomerTask(
            taskId=task_id,
            skillHash=skill_hash,
            workflowHash=workflow_hash,
            inputSetHash=_hash_json([item.input_hash for item in documents]),
            promptHash=content_hash(prompt.strip()),
            outputSchemaHash=content_hash(output_schema),
            modelName=model,
            modelDigest=runner.model_digest,
            inputCount=len(documents),
            partitioning="per-document",
            requestedOutput="workflow-output+xlsx"
            if normalized_workflow
            else (
                "skill-pipeline-json+xlsx"
                if len(skill_profiles) > 1
                else "schema-constrained-json+xlsx"
            ),
            maxPriceAtomic=10**12,
            currency=self._currency(),
            deadline=datetime.now(timezone.utc) + timedelta(hours=2),
            verificationSampleCount=sample_count,
            validatorCount=validator_count,
        )
        executor_account = Account.from_key(
            load_or_create_private_key(self.root, "miner")
        )
        validator_accounts = [
            Account.from_key(
                load_or_create_private_key(self.root, f"validator-{index + 1}")
            )
            for index in range(validator_count)
        ]
        executor_peer = executor_account.address
        validator_peers = [account.address for account in validator_accounts]
        expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
        requirements = ResourceRequirements(
            cpuCores=1, ramBytes=2_147_483_648, timeoutSeconds=7200
        )
        execution_offer = MinerOffer(
            offerId=f"{task_id}:executor",
            peerId=executor_peer,
            skillHash=skill_hash,
            priceAtomic=pricing["executionRewardAtomic"],
            currency=task.currency,
            estimatedDurationMs=pricing["p90DurationMs"],
            expiresAt=expiry,
            requirements=requirements,
            offerSource="local-fixture",
            availableResources=OfferCapabilities(
                available=True,
                cpuCores=1,
                ramBytes=2_147_483_648,
                modelDigests=[runner.model_digest],
            ),
        )
        validator_offers = []
        per_validator = pricing["verificationBudgetAtomic"] // validator_count
        for index, peer in enumerate(validator_peers):
            validator_offers.append(
                MinerOffer(
                    offerId=f"{task_id}:validator:{index}",
                    peerId=peer,
                    skillHash=skill_hash,
                    priceAtomic=per_validator
                    + (
                        1
                        if index < pricing["verificationBudgetAtomic"] % validator_count
                        else 0
                    ),
                    currency=task.currency,
                    estimatedDurationMs=max(
                        1, pricing["p90DurationMs"] * sample_count // len(documents)
                    ),
                    expiresAt=expiry,
                    requirements=requirements,
                    offerSource="local-fixture",
                    availableResources=OfferCapabilities(
                        available=True,
                        cpuCores=1,
                        ramBytes=2_147_483_648,
                        modelDigests=[runner.model_digest],
                        verificationStrategies=["per-document"],
                    ),
                )
            )
        quote = fixed_quote(
            task_id=task_id,
            currency=task.currency,
            executor_offers=[execution_offer],
            validator_offers=validator_offers,
            sample_count=sample_count,
            validator_count=validator_count,
            max_price_atomic=task.maxPriceAtomic,
            required_skill_hash=task.skillHash,
            required_resources=requirements,
            required_model_digest=runner.model_digest,
            required_verification_strategy="per-document",
            protocol_fee_atomic=pricing["protocolFeeAtomic"],
            storage_budget_atomic=pricing["storageBudgetAtomic"],
            settlement_budget_atomic=pricing["settlementBudgetAtomic"],
            assumptions={
                "pricingMethod": pricing["pricingMethod"],
                "skills": [
                    {"nodeId": item["nodeId"], "name": item["name"], "skillHash": item["skillHash"]}
                    for item in skill_estimates
                ],
                "rateCard": pricing["assumptions"],
            },
        )
        quote.verificationPolicy = {
            **quote.verificationPolicy,
            "strategy": "seeded-independent-replay",
            "disputeOnMismatch": False,
            "mismatchOutcome": "INCONCLUSIVE",
            "evidenceScope": "schema-validation-and-sampled-output-repeatability-only",
            "factualCorrectnessProven": False,
        }
        quote.failureRules = {
            **quote.failureRules,
            "inconclusive": "refund-execution-reward-pay-valid-verifier-receipts",
        }
        signed_offers = []
        for offer, account in zip(
            [execution_offer, *validator_offers],
            [executor_account, *validator_accounts],
        ):
            signed_offer = create_message(offer, account.key.hex())
            if (
                not verify_message(signed_offer)
                or signed_offer.signer.lower() != offer.peerId.lower()
            ):
                raise DemoError("local offer signature failed verification")
            signed_offers.append(signed_offer.model_dump(mode="json"))
        task_message = create_message(task, customer.key.hex())
        if (
            not verify_message(task_message)
            or task_message.signer.lower() != customer.address.lower()
        ):
            raise DemoError("customer task signature did not verify")
        quote_message = create_message(
            quote, load_or_create_private_key(self.root, "marketplace")
        )
        record = {
            "taskMode": "document",
            "promptOnly": not uploads,
            "task": task.model_dump(mode="json"),
            "taskMessage": task_message.model_dump(mode="json"),
            "quote": quote.model_dump(mode="json"),
            "quoteMessage": quote_message.model_dump(mode="json"),
            "offerMessages": signed_offers,
            "benchmark": {
                "modelDigest": runner.model_digest,
                "profile": "local-estimate-v1",
            },
            "benchmarkReport": pricing,
            "documents": [
                {
                    "unit_id": item.unit_id,
                    "file_name": item.file_name,
                    "path": item.path,
                    "input_hash": item.input_hash,
                }
                for item in documents
            ],
            "prompt": prompt.strip(),
            "workflow": normalized_workflow,
            "workflowHash": workflow_hash,
            "skillNames": selected_names,
            "skillName": ". ".join(selected_names),
            "skillDirectory": skill_profiles[0]["directory"],
            "skillHash": skill_hash,
            "skillInstructions": skill_profiles[0]["instructions"],
            "skillProfiles": skill_profiles,
            "outputSchema": output_schema,
            "model": model,
            "modelDigest": runner.model_digest,
            "customerPrivateKey": customer.key.hex(),
            "executorPrivateKey": executor_account.key.hex(),
            "executorPeer": executor_peer,
            "validatorPrivateKeys": [
                account.key.hex() for account in validator_accounts
            ],
            "validatorPeers": validator_peers,
            "unavailableValidators": [],
            "disagree": False,
            "tamperAfterCommit": False,
            "executionTimeout": False,
            "state": "CREATED",
            "progress": "Calculated a fixed local estimate; waiting for customer acceptance.",
            # Live input/output previews are served to this customer's UI only and
            # deliberately excluded from persisted quote.json below.
            "nodeExecution": {
                "currentNodeId": None,
                "totalUnits": len(documents),
                "inputValues": _runtime_value_preview(
                    {
                        "prompt": prompt.strip(),
                        "model": model,
                        "files": [item.file_name for item in documents],
                    }
                ),
                "outputValues": None,
                "nodes": [
                    {
                        "nodeId": profile["nodeId"],
                        "skillName": profile["name"],
                        "status": "QUEUED",
                        "completedUnits": 0,
                        "totalUnits": len(documents),
                        "currentUnit": None,
                        "inputValues": None,
                        "outputValues": None,
                    }
                    for profile in skill_profiles
                ],
            },
        }
        _json_file(
            task_dir / "customer-task.json", task_message.model_dump(mode="json")
        )
        _json_file(task_dir / "miner-offers.json", signed_offers)
        _json_file(
            task_dir / "fixed-price-quote.json", quote_message.model_dump(mode="json")
        )
        self._persist_record(task_id, record)
        self.event_log.append(
            task_id,
            "task.created",
            {
                "taskHash": _hash_json(task.model_dump(mode="json")),
                "inputCount": len(documents),
            },
        )
        self.event_log.append(task_id, "pricing.estimated", pricing)
        self.event_log.append(
            task_id,
            "offers.received",
            {
                "offerIds": [
                    execution_offer.offerId,
                    *(item.offerId for item in validator_offers),
                ],
                "signatures": [item["signature"] for item in signed_offers],
            },
        )
        self._transition(
            record,
            "QUOTED",
            "quote.issued",
            {
                "quoteId": quote.quoteId,
                "totalAtomic": quote.totalAtomic,
                "expiresAt": quote.expiresAt.isoformat(),
            },
        )
        self._persist_record(task_id, record)
        with self._lock:
            self._jobs[task_id] = record
        return self._public_job(record)

    def create_quote(
        self,
        invoice_count: int = 100,
        sample_count: int = 30,
        validator_count: int = 3,
        corrupt_count: int = 0,
        unavailable_validators: list[int] | None = None,
        disagree: bool = False,
        tamper_after_commit: bool = False,
        execution_timeout: bool = False,
    ) -> dict:
        if not 1 <= invoice_count <= 1000:
            raise ValueError("invoice_count must be between 1 and 1000")
        if not 1 <= sample_count <= invoice_count:
            raise ValueError("sample_count must be between 1 and invoice_count")
        if not 1 <= validator_count <= 10:
            raise ValueError("validator_count must be between 1 and 10")
        if not 0 <= corrupt_count <= invoice_count:
            raise ValueError("corrupt_count must be between 0 and invoice_count")
        if validator_count > sample_count:
            raise ValueError("validator_count cannot exceed sample_count")
        if disagree and validator_count < 2:
            raise ValueError(
                "the disagreement simulation requires at least two validators"
            )
        if any(
            index < 0 or index >= validator_count
            for index in (unavailable_validators or [])
        ):
            raise ValueError(
                "unavailable validator indexes must identify configured validators"
            )

        skill_info = self.registry.publish(self.skill_dir)
        extractor = self.extractor_factory()
        benchmark, benchmark_report = self._benchmark_if_needed(skill_info, extractor)
        task_id = str(uuid4())
        task_dir = self.jobs_dir / task_id
        invoices = generate_synthetic_invoices(
            task_dir / "private-input", invoice_count
        )
        account_customer = Account.from_key(
            load_or_create_private_key(self.root, "customer")
        )
        task = CustomerTask(
            taskId=task_id,
            skillHash=skill_info["contentHash"],
            inputSetHash=_hash_json([item.input_hash for item in invoices]),
            inputCount=invoice_count,
            partitioning="per-document",
            requestedOutput="xlsx+json",
            maxPriceAtomic=100_000_000,
            currency=self._currency(),
            deadline=datetime.now(timezone.utc) + timedelta(hours=1),
            verificationSampleCount=sample_count,
            validatorCount=validator_count,
        )
        executor_account = Account.from_key(
            load_or_create_private_key(self.root, "miner")
        )
        validator_accounts = [
            Account.from_key(
                load_or_create_private_key(self.root, f"validator-{index + 1}")
            )
            for index in range(validator_count)
        ]
        executor_peer = executor_account.address
        validator_peers = [account.address for account in validator_accounts]
        offers = self._local_offers(
            task_id,
            benchmark,
            invoice_count,
            sample_count,
            validator_count,
            executor_peer,
            validator_peers,
            skill_info["contentHash"],
        )
        quote = fixed_quote(
            task_id=task_id,
            currency=task.currency,
            executor_offers=[offers[0]],
            validator_offers=offers[1:],
            sample_count=sample_count,
            validator_count=validator_count,
            max_price_atomic=task.maxPriceAtomic,
            required_skill_hash=task.skillHash,
            required_resources=ResourceRequirements(
                cpuCores=1, ramBytes=2_147_483_648, timeoutSeconds=3600
            ),
            required_model_digest=benchmark.modelDigest,
            required_runtime_digest=benchmark.runtimeDigest,
            required_verification_strategy="per-document",
            assumptions={
                "benchmarkReceiptId": benchmark.receiptId,
                "benchmarkVersion": benchmark.benchmarkVersion,
                "benchmarkSampleCount": benchmark.sampleCount,
                "benchmarkMetricsAreSelfReported": benchmark.selfReportedMetrics,
                "estimatedDurationBasis": "benchmark P90 per input unit",
                "localStorageCost": 0,
                "localSettlementCost": 0,
                "localProtocolFee": 0,
            },
        )
        offer_messages = []
        for offer, account in zip(offers, [executor_account, *validator_accounts]):
            signed_offer = create_message(offer, account.key.hex())
            if (
                not verify_message(signed_offer)
                or signed_offer.signer.lower() != offer.peerId.lower()
            ):
                raise DemoError(
                    f"miner offer signature failed verification for {offer.offerId}"
                )
            offer_messages.append(signed_offer.model_dump(mode="json"))
        message_task = create_message(task, account_customer.key.hex())
        marketplace_key = load_or_create_private_key(self.root, "marketplace")
        message_quote = create_message(quote, marketplace_key)
        record = {
            "task": task.model_dump(mode="json"),
            "taskMessage": message_task.model_dump(mode="json"),
            "quote": quote.model_dump(mode="json"),
            "quoteMessage": message_quote.model_dump(mode="json"),
            "offerMessages": offer_messages,
            "benchmark": benchmark.model_dump(mode="json"),
            "benchmarkReport": benchmark_report,
            "invoices": [
                {
                    "invoice_id": item.invoice_id,
                    "path": item.path,
                    "input_hash": item.input_hash,
                }
                for item in invoices
            ],
            "customerPrivateKey": account_customer.key.hex(),
            "executorPrivateKey": executor_account.key.hex(),
            "executorPeer": executor_peer,
            "validatorPrivateKeys": [
                account.key.hex() for account in validator_accounts
            ],
            "validatorPeers": validator_peers,
            "corruptCount": corrupt_count,
            "unavailableValidators": sorted(set(unavailable_validators or [])),
            "disagree": disagree,
            "tamperAfterCommit": tamper_after_commit,
            "executionTimeout": execution_timeout,
            "state": "CREATED",
            "progress": "Fixed quote created; waiting for customer acceptance.",
        }
        _json_file(
            task_dir / "customer-task.json", message_task.model_dump(mode="json")
        )
        _json_file(task_dir / "miner-offers.json", offer_messages)
        _json_file(
            task_dir / "fixed-price-quote.json", message_quote.model_dump(mode="json")
        )
        benchmark_receipt = self.state_dir / "benchmark" / "benchmark-receipt.json"
        if benchmark_receipt.is_file():
            shutil.copyfile(benchmark_receipt, task_dir / "benchmark-receipt.json")
        self._persist_record(task_id, record)
        self.event_log.append(
            task_id,
            "task.created",
            {
                "taskHash": _hash_json(task.model_dump(mode="json")),
                "inputCount": invoice_count,
            },
        )
        self.event_log.append(
            task_id,
            "benchmark.attested",
            {
                "benchmarkId": benchmark.receiptId,
                "sampleCount": benchmark.sampleCount,
                "modelDigest": benchmark.modelDigest,
            },
        )
        self.event_log.append(
            task_id,
            "offers.received",
            {
                "offerIds": [item.offerId for item in offers],
                "signatures": [item["signature"] for item in offer_messages],
            },
        )
        self._transition(
            record,
            "QUOTED",
            "quote.issued",
            {
                "quoteId": quote.quoteId,
                "totalAtomic": quote.totalAtomic,
                "expiresAt": quote.expiresAt.isoformat(),
            },
        )
        self._persist_record(task_id, record)
        with self._lock:
            self._jobs[task_id] = record
        return self._public_job(record)

    def accept_quote(self, task_id: str) -> dict:
        with self._lock:
            record = self._jobs.get(task_id) or self._load_record(task_id)
            if record["state"] != "QUOTED":
                raise DemoError(
                    f"quote is not awaiting acceptance (state={record['state']})"
                )
            if not record.get("executorPrivateKey") or not record.get(
                "validatorPrivateKeys"
            ):
                raise DemoError(
                    "this quote was created by another process; local development quotes cannot resume after restart"
                )
            quote = record["quote"]
            if datetime.fromisoformat(quote["expiresAt"]) <= datetime.now(timezone.utc):
                self._transition(record, "EXPIRED", "quote.expired", {})
                self._persist_record(task_id, record)
                raise DemoError("fixed quote has expired")
            try:
                accepted_quote = self._validate_quote_acceptance(record)
            except (TypeError, ValueError) as exc:
                raise DemoError(f"quote or selected offer changed; issue a new quote: {exc}") from exc
            # The execution contract is created only at customer acceptance so
            # its acceptance timestamp and deadlines have contractual meaning.
            accepted_at = datetime.now(timezone.utc)
            challenge_round = int(time.time()) + 3600
            contract = ExecutionContract(
                contractId=str(uuid4()),
                taskId=task_id,
                quoteId=quote["quoteId"],
                executorPeerId=record["executorPeer"],
                acceptedAt=accepted_at,
                commitDeadline=accepted_at + timedelta(minutes=50),
                challengeRound=challenge_round,
                idempotencyKey=f"task:{task_id}:settle:v1",
            )
            contract_message = create_message(contract, record["customerPrivateKey"])
            if contract_message.signer.lower() != record["taskMessage"][
                "signer"
            ].lower() or not verify_message(contract_message):
                raise DemoError("accepted execution contract signature did not verify")
            record["contract"] = contract.model_dump(mode="json")
            record["contractMessage"] = contract_message.model_dump(mode="json")
            record["challengeRound"] = challenge_round
            _json_file(
                self.jobs_dir / task_id / "execution-contract.json",
                contract_message.model_dump(mode="json"),
            )
            self._transition(
                record,
                "FUNDED",
                "escrow.funded",
                {
                    "amountAtomic": accepted_quote.totalAtomic,
                    "currency": accepted_quote.currency.symbol,
                },
            )
            self._transition(
                record,
                "ASSIGNED",
                "execution.assigned",
                {
                    "executor": record["executorPeer"],
                    "validatorCount": quote["validatorCount"],
                },
            )
            record["progress"] = (
                "Quote accepted; miner assigned and execution is starting."
            )
            self._persist_record(task_id, record)
            worker = threading.Thread(
                target=self._run_task, args=(task_id,), daemon=True
            )
            self._workers[task_id] = worker
            worker.start()
        return self._public_job(record)

    def _validate_quote_acceptance(self, record: dict) -> FixedPriceQuote:
        """Recheck signed quote, ceiling, offer freshness, and offer snapshot at acceptance."""
        quote = FixedPriceQuote.model_validate(record["quote"])
        task = CustomerTask.model_validate(record["task"])
        task_envelope = SignedMessage.model_validate(record["taskMessage"])
        if (
            not isinstance(task_envelope.payload, CustomerTask)
            or task_envelope.payload != task
            or not verify_message(task_envelope)
            or task_envelope.signer.lower()
            != Account.from_key(record["customerPrivateKey"]).address.lower()
        ):
            raise ValueError("signed task payload does not match its stored price ceiling")
        if quote.taskId != task.taskId or quote.totalAtomic > task.maxPriceAtomic:
            raise ValueError("quote is not bound to this task or exceeds its accepted maximum")
        if quote.expiresAt > task.deadline:
            raise ValueError("quote outlives the signed customer task deadline")
        if any(
            getattr(quote, name) > 0
            for name in ("protocolFeeAtomic", "storageBudgetAtomic", "settlementBudgetAtomic")
        ):
            raise ValueError(
                "the local settlement adapter supports zero protocol, storage, and settlement fees only"
            )
        quote_envelope = SignedMessage.model_validate(record["quoteMessage"])
        expected_quote_issuer = Account.from_key(
            load_or_create_private_key(self.root, "marketplace")
        ).address
        if (
            not isinstance(quote_envelope.payload, FixedPriceQuote)
            or quote_envelope.payload != quote
            or not verify_message(quote_envelope)
            or quote_envelope.signer.lower() != expected_quote_issuer.lower()
        ):
            raise ValueError("signed quote payload does not match the stored quote")

        by_id: dict[str, MinerOffer] = {}
        for raw_envelope in record.get("offerMessages", []):
            envelope = SignedMessage.model_validate(raw_envelope)
            if not isinstance(envelope.payload, MinerOffer) or not verify_message(envelope):
                raise ValueError("selected offer signature is invalid")
            offer = envelope.payload
            if envelope.signer.lower() != offer.peerId.lower():
                raise ValueError("offer signer does not match the offering peer")
            if offer.offerId in by_id:
                raise ValueError("duplicate offer ID in task record")
            by_id[offer.offerId] = offer

        ordered_ids = [quote.executorOfferId, *quote.validatorOfferIds]
        if any(offer_id not in by_id for offer_id in ordered_ids):
            raise ValueError("a selected offer is missing; request a replacement quote")
        selected = [by_id[offer_id] for offer_id in ordered_ids]
        now = datetime.now(timezone.utc)
        if any(offer.expiresAt.tzinfo is None or offer.expiresAt <= now for offer in selected):
            raise ValueError("a selected offer expired; request a replacement quote")
        if quote.expiresAt > min(offer.expiresAt for offer in selected):
            raise ValueError("quote outlives a selected offer; request a replacement quote")
        if any(
            not currency_matches(offer.currency, quote.currency)
            or offer.skillHash != task.skillHash
            for offer in selected
        ):
            raise ValueError("a selected offer no longer matches task currency or skill")
        if selected[0].priceAtomic != quote.executionRewardAtomic:
            raise ValueError("executor offer price differs from the quote execution reward")
        if sum(offer.priceAtomic for offer in selected[1:]) != quote.verificationBudgetAtomic:
            raise ValueError("validator offer prices differ from the quote verification budget")
        peer_ids = [offer.peerId.lower() for offer in selected]
        if len(set(peer_ids)) != len(peer_ids):
            raise ValueError("executor and validators must be distinct peers")
        if quote.sampleCount != task.verificationSampleCount or quote.validatorCount != task.validatorCount:
            raise ValueError("quote verification workload differs from the signed task")
        expected_basis = (
            "local-fixture"
            if any(offer.offerSource == "local-fixture" for offer in selected)
            else "compatible-offers"
        )
        if quote.priceBasis != expected_basis:
            raise ValueError("quote basis does not match the selected offer sources")
        current_snapshot = content_hash(
            [offer.model_dump(mode="json") for offer in selected]
        )
        if current_snapshot != quote.offerSnapshotHash:
            raise ValueError("selected offer terms changed after quote issuance")
        return quote

    def _benchmark_if_needed(self, skill_info: dict, extractor: Extractor):
        cache_path = self.state_dir / "benchmark-cache.json"
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if (
                    cached["benchmark"]["skillHash"] == skill_info["contentHash"]
                    and cached["benchmark"]["modelDigest"] == extractor.model_digest
                    and cached["benchmark"]["sampleCount"] >= 20
                ):
                    from .models import SkillBenchmark

                    return SkillBenchmark.model_validate(cached["benchmark"]), cached[
                        "report"
                    ]
            except Exception:
                pass
        benchmark_dir = self.state_dir / "benchmark"
        benchmark, report = run_invoice_benchmark(
            self.skill_dir,
            benchmark_dir,
            extractor,
            sample_count=20,
            private_key=load_or_create_private_key(self.root, "benchmark-contributor"),
        )
        _json_file(
            cache_path,
            {"benchmark": benchmark.model_dump(mode="json"), "report": report},
        )
        return benchmark, report

    @staticmethod
    def _currency() -> Currency:
        # Test-only asset address. Production deployments configure an existing ERC-20.
        return Currency(
            chainId=31337,
            tokenAddress="0x0000000000000000000000000000000000000001",
            decimals=6,
            symbol="TESTUSD",
        )

    @staticmethod
    def _local_offers(
        task_id,
        benchmark,
        invoice_count,
        sample_count,
        validator_count,
        executor,
        validators,
        skill_hash,
    ):
        currency = DemoService._currency()
        p90 = max(1, benchmark.p90DurationMs)
        execution_price, per_validator = local_fixture_offer_prices(
            p90,
            invoice_count,
            sample_count,
            validator_count,
        )
        expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
        requirements = ResourceRequirements(
            cpuCores=1, ramBytes=2_147_483_648, timeoutSeconds=3600
        )
        executor_offer = MinerOffer(
            offerId=f"{task_id}:executor",
            peerId=executor,
            skillHash=skill_hash,
            priceAtomic=execution_price,
            currency=currency,
            estimatedDurationMs=p90 * invoice_count,
            expiresAt=expiry,
            requirements=requirements,
            offerSource="local-fixture",
            availableResources=OfferCapabilities(
                available=True,
                cpuCores=1,
                ramBytes=2_147_483_648,
                modelDigests=[benchmark.modelDigest],
                runtimeDigests=[benchmark.runtimeDigest],
            ),
        )
        validator_offers = [
            MinerOffer(
                offerId=f"{task_id}:validator:{index}",
                peerId=peer,
                skillHash=skill_hash,
                priceAtomic=per_validator,
                currency=currency,
                estimatedDurationMs=p90 * math.ceil(sample_count / validator_count),
                expiresAt=expiry,
                requirements=requirements,
                offerSource="local-fixture",
                availableResources=OfferCapabilities(
                    available=True,
                    cpuCores=1,
                    ramBytes=2_147_483_648,
                    modelDigests=[benchmark.modelDigest],
                    runtimeDigests=[benchmark.runtimeDigest],
                    verificationStrategies=["per-document"],
                ),
            )
            for index, peer in enumerate(validators)
        ]
        return [executor_offer, *validator_offers]

    def _run_task(self, task_id: str) -> None:
        with self._lock:
            record = self._jobs[task_id]
        try:
            self._run_task_locked(record)
        except Exception as exc:
            record["error"] = str(exc)
            record["progress"] = (
                "Execution stopped with an error; refunding the fixed quote."
            )
            with self._lock:
                _mark_pending_nodes_failed(record)
            current = record["state"]
            if current not in {
                "FAILED",
                "INCONCLUSIVE",
                "DISPUTED",
                "REFUNDED",
                "SETTLED",
            }:
                try:
                    self._transition(
                        record, "FAILED", "task.failed", {"error": str(exc)}
                    )
                except InvalidTransition:
                    pass
            self._finalize_refund(record, reason="EXECUTION_ERROR")
        finally:
            self._persist_record(task_id, record)
            with self._lock:
                self._workers.pop(task_id, None)

    def _run_task_locked(self, record: dict) -> None:
        if record.get("taskMode") == "document":
            self._run_document_task_locked(record)
            return
        task_id = record["task"]["taskId"]
        task_dir = self.jobs_dir / task_id
        if record["executionTimeout"]:
            time.sleep(0.01)
            self._transition(
                record, "FAILED", "execution.timeout", {"deadlineExceeded": True}
            )
            self._finalize_refund(record, reason="EXECUTION_TIMEOUT")
            return

        self._transition(
            record,
            "EXECUTED",
            "execution.completed",
            {"unitCount": len(record["invoices"])},
        )
        record["progress"] = (
            "Miner completed the task; committing output before revealing the challenge."
        )
        executor = self.extractor_factory()
        invoices = [load_invoice_file(**item) for item in record["invoices"]]
        extracted: list[ExtractedInvoice] = []
        batch_size = 10
        for offset in range(0, len(invoices), batch_size):
            batch = invoices[offset : offset + batch_size]
            extracted.extend(executor.extract_batch(batch))
            if (
                time.time()
                > datetime.fromisoformat(
                    record["contract"]["commitDeadline"]
                ).timestamp()
            ):
                self._transition(
                    record, "FAILED", "execution.timeout", {"deadlineExceeded": True}
                )
                self._finalize_refund(record, reason="EXECUTION_TIMEOUT")
                return
        records = [normalize_record(item) for item in extracted]
        records.sort(key=lambda item: item["invoiceId"])
        for index in range(min(record["corruptCount"], len(records))):
            records[index] = dict(records[index])
            records[index]["amountMinor"] += 777
        result_json = task_dir / "results.json"
        result_xlsx = task_dir / "invoices.xlsx"
        _json_file(result_json, records)
        write_workbook(records, result_xlsx)
        files = [result_json, result_xlsx]
        artifact_manifest = [
            {"name": path.name, "hash": self._file_hash(path)} for path in files
        ]
        output_artifact_hash = _hash_json(artifact_manifest)
        input_by_id = {item.invoice_id: item for item in invoices}
        invoice_by_id = {item.invoice_id: item for item in invoices}
        values = []
        for item in records:
            invoice = input_by_id[item["invoiceId"]]
            values.append(
                (_hash_unit_id(item["invoiceId"]), invoice.input_hash, _hash_json(item))
            )
        tree = build_tree(values)
        commitment = ExecutionCommitment(
            taskId=task_id,
            contractId=record["contract"]["contractId"],
            merkleRoot=tree.root,
            outputArtifactHash=output_artifact_hash,
            unitCount=len(values),
            leafEncoding=LEAF_ENCODING,
            committedAt=datetime.now(timezone.utc),
            challengeRound=record["challengeRound"],
        )
        miner_key = record["executorPrivateKey"]
        commitment_message = create_message(commitment, miner_key)
        _json_file(
            task_dir / "execution-commitment.json",
            commitment_message.model_dump(mode="json"),
        )
        record["commitment"] = commitment_message.model_dump(mode="json")
        record["merkleTree"] = {
            "root": tree.root,
            "unitCount": len(values),
            "leafEncoding": LEAF_ENCODING,
        }
        self.event_log.append(
            task_id,
            "execution.committed",
            {
                "root": tree.root,
                "artifactHash": output_artifact_hash,
                "unitCount": len(values),
                "signature": commitment_message.signature,
            },
        )
        if (
            not verify_message(commitment_message)
            or commitment_message.signer.lower() != record["executorPeer"].lower()
        ):
            raise DemoError("executor commitment signature did not verify")

        if record["tamperAfterCommit"]:
            changed = list(records)
            changed[0] = dict(changed[0])
            changed[0]["amountMinor"] += 1
            _json_file(result_json, changed)
            actual_manifest = [
                {"name": path.name, "hash": self._file_hash(path)} for path in files
            ]
            if _hash_json(actual_manifest) != commitment.outputArtifactHash:
                self._transition(
                    record,
                    "FAILED",
                    "execution.post_commit_tamper_detected",
                    {"committedRoot": tree.root},
                )
                self._finalize_refund(record, reason="POST_COMMIT_ARTIFACT_MISMATCH")
                return

        self._transition(
            record,
            "VERIFYING",
            "verification.challenge_open",
            {"challengeRound": record["challengeRound"]},
        )
        record["progress"] = (
            "Output root is committed. Selecting validators and samples from post-commit randomness."
        )
        beacon = SimulatedBeacon()
        randomness = beacon.read(record["challengeRound"], task_id, tree.root)
        seed = challenge_seed(task_id, tree.root, record["challengeRound"], randomness)
        validator_peers = record["validatorPeers"]
        selected = select_validators(
            seed, validator_peers, record["executorPeer"], len(validator_peers)
        )
        assignments = select_units(
            seed,
            [item.invoice_id for item in invoices],
            record["quote"]["sampleCount"],
            selected,
        )
        if record["disagree"]:
            if len(selected) < 2:
                raise DemoError(
                    "the disagreement simulation requires at least two validators"
                )
            # Duplicate one unit across two independent validators. The second
            # worker is explicitly fault-injected to disagree below.
            disputed_unit = assignments[selected[0]][0]
            assignments[selected[1]].append(disputed_unit)
        record["randomness"] = {
            "source": "SIMULATED_LOCAL_BEACON",
            "verified": False,
            "challengeRound": record["challengeRound"],
            "value": randomness,
        }
        assignment_messages = []
        for index, validator_peer in enumerate(selected):
            assignment = VerificationAssignment(
                taskId=task_id,
                commitmentRoot=tree.root,
                randomnessRound=record["challengeRound"],
                randomnessValue=randomness,
                randomnessVerified=False,
                validatorPeerId=validator_peer,
                unitIds=assignments[validator_peer],
                assignmentIndex=index,
            )
            key_index = record["validatorPeers"].index(validator_peer)
            signed_assignment = create_message(
                assignment, record["validatorPrivateKeys"][key_index]
            )
            if (
                signed_assignment.signer.lower() != validator_peer.lower()
                or not verify_message(signed_assignment)
            ):
                raise DemoError("validator assignment signature did not verify")
            assignment_messages.append(signed_assignment.model_dump(mode="json"))
        record["assignmentMessages"] = assignment_messages
        _json_file(
            task_dir / "verification-assignments.json",
            {
                "commitmentRoot": tree.root,
                "randomness": record["randomness"],
                "assignments": assignments,
                "signedAssignments": assignment_messages,
            },
        )
        self.event_log.append(
            task_id,
            "verification.assigned",
            {
                "commitmentRoot": tree.root,
                "randomnessSource": "SIMULATED_LOCAL_BEACON",
                "verified": False,
                "assignments": assignments,
                "signatures": [item["signature"] for item in assignment_messages],
            },
        )

        committed_by_id = {item["invoiceId"]: item for item in records}
        unit_index = {item["invoiceId"]: index for index, item in enumerate(records)}
        all_receipts: list[dict] = []
        unavailable = set(record["unavailableValidators"])
        for validator_index, validator_peer in enumerate(selected):
            if validator_index in unavailable:
                self.event_log.append(
                    task_id,
                    "verification.validator_timeout",
                    {
                        "validator": validator_peer,
                        "unitIds": assignments[validator_peer],
                    },
                )
                continue
            assigned_files = [
                invoice_by_id[unit_id] for unit_id in assignments[validator_peer]
            ]
            validator = self.extractor_factory()
            observations = validator.extract_batch(assigned_files)
            observed_by_id = {
                item.invoiceId.strip().upper(): item for item in observations
            }
            if record["disagree"] and assigned_files:
                first_id = assigned_files[0].invoice_id
                original = observed_by_id[first_id]
                observed_by_id[first_id] = original.model_copy(
                    update={"supplier": original.supplier + " (disputed)"}
                )
            account_key = record["validatorPrivateKeys"][
                record["validatorPeers"].index(validator_peer)
            ]
            for invoice in assigned_files:
                checks = verify_invoice_unit(
                    unit_index[invoice.invoice_id],
                    invoice,
                    committed_by_id[invoice.invoice_id],
                    observed_by_id[invoice.invoice_id],
                    tree,
                )
                receipt = VerificationReceipt(
                    receiptId=str(uuid4()),
                    taskId=task_id,
                    commitmentRoot=tree.root,
                    validatorPeerId=validator_peer,
                    unitId=invoice.invoice_id,
                    result=checks["result"],
                    observedOutputHash=checks["observedOutputHash"],
                    evidenceHash=checks["evidenceHash"],
                    merkleProof=checks["proof"],
                    reasonCode=checks["reasonCode"],
                    modelDigest=validator.model_digest,
                    durationMs=0,
                )
                signed = create_message(receipt, account_key)
                signature_ok = verify_message(signed)
                identity_ok = (
                    signed.signer.lower() == validator_peer.lower()
                    and receipt.validatorPeerId.lower() == validator_peer.lower()
                )
                if not signature_ok or not identity_ok:
                    raise DemoError(
                        f"validator receipt signature failed verification for {invoice.invoice_id} (signature={signature_ok}, signer={signed.signer}, assigned={validator_peer}, payload={receipt.validatorPeerId})"
                    )
                receipt_record = signed.model_dump(mode="json")
                receipt_record["evidence"] = checks["evidence"]
                all_receipts.append(receipt_record)
                _json_file(
                    task_dir / "receipts" / f"{receipt.receiptId}.json", receipt_record
                )
        self.event_log.append(
            task_id,
            "verification.receipts",
            {"received": len(all_receipts), "assigned": record["quote"]["sampleCount"]},
        )
        raw_receipts = [
            item["payload"] | {"receiptId": item["payload"]["receiptId"]}
            for item in all_receipts
        ]
        decision_data = decide(raw_receipts, record["quote"]["sampleCount"])
        decision = VerificationDecision(taskId=task_id, **decision_data)
        marketplace_key = load_or_create_private_key(self.root, "marketplace")
        decision_message = create_message(decision, marketplace_key)
        record["receipts"] = all_receipts
        record["decision"] = decision_message.model_dump(mode="json")
        _json_file(
            task_dir / "verification-decision.json",
            decision_message.model_dump(mode="json"),
        )
        self.event_log.append(
            task_id, "verification.decision", decision_message.model_dump(mode="json")
        )

        if decision.outcome == "DISPUTED":
            self._transition(
                record, "DISPUTED", "task.disputed", {"reasonCode": decision.reasonCode}
            )
            record["progress"] = (
                "Verification evidence conflicts; escrow remains unresolved for an additional audit."
            )
            return
        if decision.outcome == "VERIFIED":
            self._transition(
                record,
                "VERIFIED",
                "task.verified",
                {"decisionHash": content_hash(decision.model_dump(mode="json"))},
            )
            settlement_outcome = "VERIFIED"
        elif decision.outcome == "FAILED":
            self._transition(
                record,
                "FAILED",
                "task.verification_failed",
                {"reasonCode": decision.reasonCode},
            )
            settlement_outcome = "FAILED"
        else:
            self._transition(
                record,
                "INCONCLUSIVE",
                "task.inconclusive",
                {"reasonCode": decision.reasonCode},
            )
            settlement_outcome = "INCONCLUSIVE"
        settlement = create_settlement(
            task_id=task_id,
            quote_id=record["quote"]["quoteId"],
            idempotency_key=record["contract"]["idempotencyKey"],
            outcome=settlement_outcome,
            currency=Currency.model_validate(record["quote"]["currency"]),
            execution_reward=record["quote"]["executionRewardAtomic"],
            verification_budget=record["quote"]["verificationBudgetAtomic"],
            miner=record["executorPeer"],
            validator_prices=self._validator_prices(record),
            valid_validator_peers={
                item["payload"]["validatorPeerId"] for item in all_receipts
            },
            decision_hash=content_hash(decision.model_dump(mode="json")),
        )
        validate_settlement_within_quote(settlement, record["quote"])
        stored, _ = settle_once(self.event_log, settlement)
        retried, retry_applied = settle_once(self.event_log, settlement)
        if retried != stored or retry_applied:
            raise DemoError(
                "idempotent settlement retry did not return the original payout"
            )
        record["settlement"] = stored
        record["settlementRetryWasIdempotent"] = not retry_applied
        settlement_message = create_message(
            PaymentSettlement.model_validate(stored), marketplace_key
        )
        record["settlementMessage"] = settlement_message.model_dump(mode="json")
        _json_file(
            task_dir / "payment-settlement.json",
            settlement_message.model_dump(mode="json"),
        )
        self._transition(
            record,
            "SETTLED" if settlement.outcome == "SETTLED" else "REFUNDED",
            "payment.finalized",
            {
                "settlementId": settlement.settlementId,
                "chargedAtomic": settlement.totalChargedAtomic,
            },
        )
        record["progress"] = (
            "Task verification and automatic local settlement completed."
        )

    def _run_document_task_locked(self, record: dict) -> None:
        """Execute and sample-verify customer-uploaded documents, one per unit."""
        task_id = record["task"]["taskId"]
        task_dir = self.jobs_dir / task_id
        if record["executionTimeout"]:
            with self._lock:
                _mark_pending_nodes_failed(record)
            self._transition(
                record, "FAILED", "execution.timeout", {"deadlineExceeded": True}
            )
            self._finalize_refund(record, reason="EXECUTION_TIMEOUT")
            return
        task_payload = record["task"]
        if content_hash(record["prompt"]) != task_payload.get("promptHash"):
            raise DemoError("customer prompt changed after the signed task was created")
        skill_profiles = record.get("skillProfiles") or [
            {
                "name": record["skillName"],
                "contentHash": record["skillHash"],
                "directory": record["skillDirectory"],
                "instructions": record["skillInstructions"],
                "inputSchema": {},
                "outputSchema": record["outputSchema"],
                "allowedTools": [],
                "requirements": {},
            }
        ]
        current_profiles = []
        skills_root = (self.root / "skills").resolve()
        for profile in skill_profiles:
            skill_dir = Path(profile["directory"]).resolve()
            try:
                skill_dir.relative_to(skills_root)
            except ValueError as exc:
                raise DemoError(
                    "selected skill package is outside the published skills directory"
                ) from exc
            skill_now = self.registry.publish(skill_dir)
            if skill_now.get("name") != profile["name"]:
                raise DemoError(
                    f"selected skill identity changed after the quote: {profile['name']}"
                )
            if skill_now["contentHash"] != profile["contentHash"]:
                raise DemoError(
                    f"selected skill content changed after the quote was issued: {profile['name']}"
                )
            skill_file = (skill_dir / skill_now["skillFile"]).resolve()
            try:
                skill_file.relative_to(skill_dir)
            except ValueError as exc:
                raise DemoError(
                    "skill instructions escaped the published package"
                ) from exc
            if not skill_file.is_file() or skill_file.stat().st_size > 1_000_000:
                raise DemoError(
                    "skill instructions are missing or exceed the size limit"
                )
            current_instructions = read_skill_instructions(skill_dir)
            current_input_schema = skill_now.get("inputSchema", {})
            current_schema = skill_now.get("outputSchema", {})
            if current_instructions != profile["instructions"] or content_hash(
                current_schema
            ) != content_hash(profile["outputSchema"]):
                raise DemoError(
                    f"selected skill instructions or schema changed after the quote: {profile['name']}"
                )
            current_profiles.append(
                {
                    **profile,
                    "instructions": current_instructions,
                    "inputSchema": current_input_schema,
                    "outputSchema": current_schema,
                    "allowedTools": skill_now.get("allowedTools", []),
                    "requirements": skill_now.get("requirements", {}),
                }
            )
        current_skill_hash = (
            current_profiles[0]["contentHash"]
            if len(current_profiles) == 1
            else content_hash(
                [
                    {"name": item["name"], "contentHash": item["contentHash"]}
                    for item in current_profiles
                ]
            )
        )
        current_workflow = (
            _normalize_workflow(record["workflow"]) if record.get("workflow") else None
        )
        current_workflow_hash = (
            _workflow_hash(current_workflow, current_profiles)
            if current_workflow
            else None
        )
        current_output_schema = (
            _workflow_output_schema(current_profiles, current_workflow)
            if current_workflow
            else _skill_chain_schema(current_profiles)
        )
        if (
            current_skill_hash != task_payload.get("skillHash")
            or content_hash(record["outputSchema"])
            != task_payload.get("outputSchemaHash")
            or content_hash(current_output_schema)
            != task_payload.get("outputSchemaHash")
        ):
            raise DemoError(
                "selected skill set or output schema changed after the signed task was created"
            )
        if current_workflow_hash != task_payload.get("workflowHash"):
            raise DemoError(
                "workflow connections changed after the signed task was created"
            )
        if (
            task_payload.get("modelName") != record["model"]
            or task_payload.get("modelDigest") != record["modelDigest"]
        ):
            raise DemoError("selected model changed after the signed task was created")
        runner = OllamaDocumentRunner(record["model"], self.ollama_url)
        if runner.model_digest != record["modelDigest"]:
            raise DemoError(
                "selected Ollama model digest changed after the quote was issued"
            )
        documents = [load_document(item) for item in record["documents"]]
        self._transition(
            record,
            "EXECUTED",
            "execution.started",
            {"unitCount": len(documents), "modelDigest": runner.model_digest},
        )
        scope = (
            "the prompt-only request"
            if record.get("promptOnly")
            else f"{len(documents)} uploaded file(s)"
        )
        record["progress"] = (
            f"Running {len(current_profiles)} workflow skill node(s) on {scope} with {record['model']}."
        )
        records = []

        def publish_node_update(
            node_id,
            skill_name,
            status,
            unit_index,
            unit_count,
            input_values,
            output_values,
        ):
            with self._lock:
                execution = record.get("nodeExecution")
                if not isinstance(execution, dict):
                    return
                node_state = next(
                    (
                        item
                        for item in execution.get("nodes", [])
                        if item.get("nodeId") == node_id
                    ),
                    None,
                )
                if node_state is None:
                    return
                node_state["skillName"] = skill_name
                node_state["status"] = status
                node_state["currentUnit"] = unit_index if status == "RUNNING" else None
                node_state["completedUnits"] = max(
                    node_state.get("completedUnits", 0),
                    unit_index if status in {"IN_PROGRESS", "COMPLETED"} else 0,
                )
                if input_values is not None:
                    node_state["inputValues"] = _runtime_value_preview(input_values)
                if output_values is not None:
                    node_state["outputValues"] = _runtime_value_preview(output_values)
                if status == "RUNNING":
                    execution["currentNodeId"] = node_id
                    record["progress"] = (
                        f"Running {skill_name} · document {unit_index}/{unit_count}."
                    )
                elif execution.get("currentNodeId") == node_id:
                    execution["currentNodeId"] = None

        for unit_index, document in enumerate(documents, start=1):
            if (
                time.time()
                > datetime.fromisoformat(
                    record["contract"]["commitDeadline"]
                ).timestamp()
            ):
                with self._lock:
                    _mark_pending_nodes_failed(record)
                self._transition(
                    record, "FAILED", "execution.timeout", {"deadlineExceeded": True}
                )
                self._finalize_refund(record, reason="EXECUTION_TIMEOUT")
                return
            result = (
                _run_skill_workflow(
                    runner,
                    current_profiles,
                    current_workflow,
                    record["prompt"],
                    document,
                    on_node_update=publish_node_update,
                    unit_index=unit_index,
                    unit_count=len(documents),
                )
                if current_workflow
                else _run_skill_chain(
                    runner, current_profiles, record["prompt"], document
                )
            )
            records.append(
                {
                    "unitId": document.unit_id,
                    "fileName": document.file_name,
                    "result": result,
                }
            )
            with self._lock:
                execution = record.get("nodeExecution")
                if isinstance(execution, dict):
                    execution["outputValues"] = _runtime_value_preview(
                        {
                            "completedUnits": len(records),
                            "totalUnits": len(documents),
                            "latestUnit": {
                                "unitId": document.unit_id,
                                "fileName": document.file_name,
                                "value": result,
                            },
                        }
                    )
        _json_file(task_dir / "results.json", records)
        write_result_workbook(records, task_dir / "results.xlsx")
        artifact_paths = [task_dir / "results.json", task_dir / "results.xlsx"]
        artifact_manifest = [
            {"name": path.name, "hash": self._file_hash(path)}
            for path in artifact_paths
        ]
        artifact_hash = _hash_json(artifact_manifest)
        document_by_id = {item.unit_id: item for item in documents}
        leaf_values = [
            (
                _hash_unit_id(item["unitId"]),
                document_by_id[item["unitId"]].input_hash,
                _hash_json(item),
            )
            for item in records
        ]
        tree = build_tree(leaf_values)
        commitment = ExecutionCommitment(
            taskId=task_id,
            contractId=record["contract"]["contractId"],
            merkleRoot=tree.root,
            outputArtifactHash=artifact_hash,
            unitCount=len(leaf_values),
            leafEncoding=LEAF_ENCODING,
            committedAt=datetime.now(timezone.utc),
            challengeRound=record["challengeRound"],
        )
        commitment_message = create_message(commitment, record["executorPrivateKey"])
        if (
            not verify_message(commitment_message)
            or commitment_message.signer.lower() != record["executorPeer"].lower()
        ):
            raise DemoError("executor commitment signature did not verify")
        _json_file(
            task_dir / "execution-commitment.json",
            commitment_message.model_dump(mode="json"),
        )
        record["commitment"] = commitment_message.model_dump(mode="json")
        record["merkleTree"] = {
            "root": tree.root,
            "unitCount": len(leaf_values),
            "leafEncoding": LEAF_ENCODING,
        }
        self.event_log.append(
            task_id,
            "execution.committed",
            {
                "root": tree.root,
                "artifactHash": artifact_hash,
                "unitCount": len(leaf_values),
                "signature": commitment_message.signature,
            },
        )
        current_manifest = [
            {"name": path.name, "hash": self._file_hash(path)}
            for path in artifact_paths
        ]
        if _hash_json(current_manifest) != commitment.outputArtifactHash:
            self._transition(
                record,
                "FAILED",
                "execution.post_commit_tamper_detected",
                {"committedRoot": tree.root},
            )
            self._finalize_refund(record, reason="POST_COMMIT_ARTIFACT_MISMATCH")
            return

        self._transition(
            record,
            "VERIFYING",
            "verification.challenge_open",
            {"challengeRound": record["challengeRound"]},
        )
        record["progress"] = (
            "Results are committed. Deriving sample assignments from the local post-commit randomness adapter."
        )
        beacon = SimulatedBeacon()
        randomness = beacon.read(record["challengeRound"], task_id, tree.root)
        seed = challenge_seed(task_id, tree.root, record["challengeRound"], randomness)
        selected = select_validators(
            seed,
            record["validatorPeers"],
            record["executorPeer"],
            len(record["validatorPeers"]),
        )
        assignments = select_units(
            seed,
            [item.unit_id for item in documents],
            record["quote"]["sampleCount"],
            selected,
        )
        record["randomness"] = {
            "source": "SIMULATED_LOCAL_BEACON",
            "verified": False,
            "challengeRound": record["challengeRound"],
            "value": randomness,
        }
        assignment_messages = []
        for index, peer in enumerate(selected):
            assignment = VerificationAssignment(
                taskId=task_id,
                commitmentRoot=tree.root,
                randomnessRound=record["challengeRound"],
                randomnessValue=randomness,
                randomnessVerified=False,
                validatorPeerId=peer,
                unitIds=assignments[peer],
                assignmentIndex=index,
            )
            key = record["validatorPrivateKeys"][record["validatorPeers"].index(peer)]
            signed_assignment = create_message(assignment, key)
            if (
                not verify_message(signed_assignment)
                or signed_assignment.signer.lower() != peer.lower()
            ):
                raise DemoError("validator assignment signature did not verify")
            assignment_messages.append(signed_assignment.model_dump(mode="json"))
        record["assignmentMessages"] = assignment_messages
        _json_file(
            task_dir / "verification-assignments.json",
            {
                "commitmentRoot": tree.root,
                "randomness": record["randomness"],
                "assignments": assignments,
                "signedAssignments": assignment_messages,
            },
        )
        self.event_log.append(
            task_id,
            "verification.assigned",
            {
                "commitmentRoot": tree.root,
                "randomnessSource": "SIMULATED_LOCAL_BEACON",
                "verified": False,
                "assignments": assignments,
                "signatures": [item["signature"] for item in assignment_messages],
            },
        )

        records_by_id = {item["unitId"]: item for item in records}
        indices_by_id = {item["unitId"]: index for index, item in enumerate(records)}
        unavailable = set(record["unavailableValidators"])
        all_receipts = []
        for validator_index, peer in enumerate(selected):
            if validator_index in unavailable:
                self.event_log.append(
                    task_id,
                    "verification.validator_timeout",
                    {"validator": peer, "unitIds": assignments[peer]},
                )
                continue
            validator_key = record["validatorPrivateKeys"][
                record["validatorPeers"].index(peer)
            ]
            validator_runner = OllamaDocumentRunner(record["model"], self.ollama_url)
            for unit_id in assignments[peer]:
                document = document_by_id[unit_id]
                committed_record = records_by_id[unit_id]
                observed = (
                    _run_skill_workflow(
                        validator_runner,
                        current_profiles,
                        current_workflow,
                        record["prompt"],
                        document,
                    )
                    if current_workflow
                    else _run_skill_chain(
                        validator_runner, current_profiles, record["prompt"], document
                    )
                )
                proof = tree.proof(indices_by_id[unit_id])
                proof_valid = tree.verify(indices_by_id[unit_id], proof)
                observed_record = {
                    "unitId": unit_id,
                    "fileName": document.file_name,
                    "result": observed,
                }
                committed_hash = _hash_json(committed_record)
                observed_hash = _hash_json(observed_record)
                if not proof_valid:
                    result, reason = "FAIL", "INVALID_MERKLE_PROOF"
                elif committed_hash != observed_hash:
                    result, reason = "INCONCLUSIVE", "INDEPENDENT_MODEL_DISAGREEMENT"
                else:
                    result, reason = "PASS", "INDEPENDENT_REPLAY_AND_COMMITMENT_MATCH"
                evidence = {
                    "unitId": unit_id,
                    "inputHash": document.input_hash,
                    "committedOutput": committed_record,
                    "independentOutput": observed_record,
                    "merkleProofValid": proof_valid,
                    "verificationMeaning": "repeatability and commitment membership; not factual correctness proof",
                }
                receipt = VerificationReceipt(
                    receiptId=str(uuid4()),
                    taskId=task_id,
                    commitmentRoot=tree.root,
                    validatorPeerId=peer,
                    unitId=unit_id,
                    result=result,
                    observedOutputHash=observed_hash,
                    evidenceHash=content_hash(evidence),
                    merkleProof=proof,
                    reasonCode=reason,
                    modelDigest=validator_runner.model_digest,
                    durationMs=validator_runner.last_metrics["durationMs"],
                )
                signed = create_message(receipt, validator_key)
                if not verify_message(signed) or signed.signer.lower() != peer.lower():
                    raise DemoError(
                        f"validator receipt signature failed verification for {unit_id}"
                    )
                receipt_record = signed.model_dump(mode="json")
                receipt_record["evidence"] = evidence
                all_receipts.append(receipt_record)
                _json_file(
                    task_dir / "receipts" / f"{receipt.receiptId}.json", receipt_record
                )

        self.event_log.append(
            task_id,
            "verification.receipts",
            {"received": len(all_receipts), "assigned": record["quote"]["sampleCount"]},
        )
        raw_receipts = [item["payload"] for item in all_receipts]
        decision_data = decide(raw_receipts, record["quote"]["sampleCount"])
        if decision_data["outcome"] == "VERIFIED":
            decision_data["explanation"] = (
                "Every assigned sample's seeded independent replay matched the committed structured output and Merkle membership proof. "
                "This is evidence of repeatability for sampled units only; it does not prove factual correctness, inference execution, or unsampled results."
            )
        elif decision_data["outcome"] == "FAILED":
            decision_data["explanation"] = (
                "A sampled unit failed its Merkle commitment-membership check. The signed receipt identifies the proof evidence."
            )
        decision = VerificationDecision(taskId=task_id, **decision_data)
        marketplace_key = load_or_create_private_key(self.root, "marketplace")
        decision_message = create_message(decision, marketplace_key)
        record["receipts"] = all_receipts
        record["decision"] = decision_message.model_dump(mode="json")
        _json_file(task_dir / "verification-decision.json", record["decision"])
        self.event_log.append(task_id, "verification.decision", record["decision"])
        if decision.outcome == "DISPUTED":
            self._transition(
                record, "DISPUTED", "task.disputed", {"reasonCode": decision.reasonCode}
            )
            record["progress"] = (
                "Validator evidence conflicts; escrow remains unresolved for additional audit."
            )
            return
        if decision.outcome == "VERIFIED":
            self._transition(
                record,
                "VERIFIED",
                "task.verified",
                {"decisionHash": content_hash(decision.model_dump(mode="json"))},
            )
            settlement_outcome = "VERIFIED"
        elif decision.outcome == "FAILED":
            self._transition(
                record,
                "FAILED",
                "task.verification_failed",
                {"reasonCode": decision.reasonCode},
            )
            settlement_outcome = "FAILED"
        else:
            self._transition(
                record,
                "INCONCLUSIVE",
                "task.inconclusive",
                {"reasonCode": decision.reasonCode},
            )
            settlement_outcome = "INCONCLUSIVE"
        settlement = create_settlement(
            task_id=task_id,
            quote_id=record["quote"]["quoteId"],
            idempotency_key=record["contract"]["idempotencyKey"],
            outcome=settlement_outcome,
            currency=Currency.model_validate(record["quote"]["currency"]),
            execution_reward=record["quote"]["executionRewardAtomic"],
            verification_budget=record["quote"]["verificationBudgetAtomic"],
            miner=record["executorPeer"],
            validator_prices=self._validator_prices(record),
            valid_validator_peers={
                item["payload"]["validatorPeerId"] for item in all_receipts
            },
            decision_hash=content_hash(decision.model_dump(mode="json")),
        )
        validate_settlement_within_quote(settlement, record["quote"])
        stored, _ = settle_once(self.event_log, settlement)
        retried, retry_applied = settle_once(self.event_log, settlement)
        if retried != stored or retry_applied:
            raise DemoError(
                "idempotent settlement retry did not return the original payout"
            )
        record["settlement"] = stored
        record["settlementRetryWasIdempotent"] = not retry_applied
        settlement_message = create_message(
            PaymentSettlement.model_validate(stored), marketplace_key
        )
        record["settlementMessage"] = settlement_message.model_dump(mode="json")
        _json_file(task_dir / "payment-settlement.json", record["settlementMessage"])
        self._transition(
            record,
            "SETTLED" if settlement.outcome == "SETTLED" else "REFUNDED",
            "payment.finalized",
            {
                "settlementId": settlement.settlementId,
                "chargedAtomic": settlement.totalChargedAtomic,
            },
        )
        record["progress"] = (
            "Document task verification and idempotent local settlement completed."
        )

    def _finalize_refund(self, record: dict, reason: str) -> None:
        if record["state"] not in {"FAILED", "INCONCLUSIVE", "EXPIRED", "CANCELLED"}:
            return
        quote = record["quote"]
        task_id = record["task"]["taskId"]
        settlement = create_settlement(
            task_id=task_id,
            quote_id=quote["quoteId"],
            idempotency_key=record["contract"]["idempotencyKey"],
            outcome="FAILED",
            currency=Currency.model_validate(quote["currency"]),
            execution_reward=quote["executionRewardAtomic"],
            verification_budget=quote["verificationBudgetAtomic"],
            miner=record["executorPeer"],
            validator_prices=self._validator_prices(record),
            valid_validator_peers=set(),
            decision_hash=content_hash({"taskId": task_id, "reason": reason}),
        )
        validate_settlement_within_quote(settlement, quote)
        stored, _ = settle_once(self.event_log, settlement)
        record["settlement"] = stored
        record["settlementRetryWasIdempotent"] = not settle_once(
            self.event_log, settlement
        )[1]
        signed_settlement = create_message(
            PaymentSettlement.model_validate(stored),
            load_or_create_private_key(self.root, "marketplace"),
        )
        record["settlementMessage"] = signed_settlement.model_dump(mode="json")
        _json_file(
            self.jobs_dir / task_id / "payment-settlement.json",
            signed_settlement.model_dump(mode="json"),
        )
        if record["state"] != "REFUNDED":
            self._transition(record, "REFUNDED", "payment.refunded", {"reason": reason})

    def _transition(
        self, record: dict, next_state: str, event_type: str, payload: dict
    ) -> None:
        previous = record["state"]
        validate_transition(previous, next_state)
        record["state"] = next_state
        self.event_log.append(
            record["task"]["taskId"],
            event_type,
            {"from": previous, "to": next_state, **payload},
        )

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    def _load_record(self, task_id: str) -> dict:
        path = self.jobs_dir / task_id / "quote.json"
        if not path.is_file():
            raise DemoError(f"task not found: {task_id}")
        record = json.loads(path.read_text(encoding="utf-8"))
        self._jobs[task_id] = record
        return record

    def _persist_record(self, task_id: str, record: dict) -> None:
        safe_record = dict(record)
        safe_record.pop("customerPrivateKey", None)
        safe_record.pop("executorPrivateKey", None)
        safe_record.pop("validatorPrivateKeys", None)
        # These previews can contain customer prompts and uploaded document text.
        # Keep them transient for the local inspector; never write them into quote.json.
        safe_record.pop("nodeExecution", None)
        _json_file(self.jobs_dir / task_id / "quote.json", safe_record)

    @staticmethod
    def _validator_prices(record: dict) -> dict[str, int]:
        prices = {}
        for index, peer in enumerate(record["validatorPeers"]):
            prices[peer] = int(record["quote"]["verificationBudgetAtomic"]) // max(
                1, len(record["validatorPeers"])
            )
        return prices

    def status(self, task_id: str) -> dict:
        with self._lock:
            return self._public_job(
                self._jobs.get(task_id) or self._load_record(task_id)
            )

    def add_rating(self, task_id: str, stars: int, comment: str = "") -> dict:
        with self._lock:
            record = self._jobs.get(task_id) or self._load_record(task_id)
            if record["state"] not in {"SETTLED", "REFUNDED"}:
                raise DemoError(
                    "ratings are accepted after a task reaches a terminal payment state"
                )
            if not record.get("customerPrivateKey"):
                raise DemoError(
                    "the customer signing key is not available in this process"
                )
            rating = SkillRating(
                ratingId=str(uuid4()),
                taskId=task_id,
                skillHash=record["task"]["skillHash"],
                stars=stars,
                comment=comment,
            )
            signed = create_message(rating, record["customerPrivateKey"])
            if signed.signer.lower() != record["taskMessage"][
                "signer"
            ].lower() or not verify_message(signed):
                raise DemoError("rating signer must match the task customer")
            self.event_log.append(
                task_id,
                "skill.rating",
                {"rating": signed.model_dump(mode="json"), "paymentUnaffected": True},
            )
            _json_file(
                self.jobs_dir / task_id / "rating.json", signed.model_dump(mode="json")
            )
            return {"rating": rating.model_dump(mode="json"), "paymentUnaffected": True}

    def artifacts(self, task_id: str) -> list[dict]:
        directory = self.jobs_dir / task_id
        if not directory.is_dir():
            raise DemoError(f"task not found: {task_id}")
        return [
            {
                "name": file.relative_to(directory).as_posix(),
                "url": f"/api/tasks/{task_id}/artifacts/{file.relative_to(directory).as_posix()}",
            }
            for file in sorted(directory.rglob("*"))
            if file.is_file()
            and "private-input" not in file.relative_to(directory).parts
            and file.relative_to(directory).as_posix() != "quote.json"
        ]

    def artifact_path(self, task_id: str, name: str) -> Path:
        relative = Path(name)
        root = (self.jobs_dir / task_id).resolve()
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise DemoError("artifact not found") from exc
        if (
            "private-input" in path.relative_to(root).parts
            or path.name == "quote.json"
            or not path.is_file()
        ):
            raise DemoError("artifact not found")
        return path

    def audit_events(self, task_id: str) -> list[dict]:
        self.status(task_id)
        return self.event_log.events(task_id)

    @staticmethod
    def _public_job(record: dict) -> dict:
        quote = record["quote"]
        task = dict(record["task"])
        if record.get("taskMode") == "document":
            task["skillName"] = record["skillName"]
            task["skillNames"] = record.get("skillNames", [record["skillName"]])
            task["model"] = record["model"]
        return {
            "task": task,
            "quote": quote,
            "state": record["state"],
            "progress": record.get("progress", ""),
            "error": record.get("error"),
            "benchmark": record["benchmarkReport"],
            "pricing": record["benchmarkReport"]
            if record.get("taskMode") == "document"
            else None,
            "commitment": record.get("merkleTree"),
            "decision": record.get("decision", {}).get("payload"),
            "settlement": record.get("settlement"),
            "settlementRetryWasIdempotent": record.get("settlementRetryWasIdempotent"),
            "randomness": record.get("randomness"),
            "receiptCount": len(record.get("receipts", [])),
            "validatorCount": quote["validatorCount"],
            "sampleCount": quote["sampleCount"],
            "ratingPaymentIndependent": True,
            "nodeExecution": record.get("nodeExecution"),
        }
