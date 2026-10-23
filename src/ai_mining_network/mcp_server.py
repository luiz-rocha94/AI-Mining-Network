"""Optional MCP v2 server exposing only versioned, bounded skill execution."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

from .document_tasks import OllamaDocumentRunner, SkillExecutionError
from .registry import read_skill, read_skill_instructions
from .skill_packages import SkillPackageError, validate_json_value
from .tool_runtime import ToolInvocationError

MAX_MCP_INPUT_BYTES = 1 * 1024 * 1024
MCP_SKILL_TOOL_ID = "amn.skill.execute@1.0.0"
MCP_SKILL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_name": {"type": "string", "minLength": 1, "maxLength": 64},
        "input_values": {"type": "object"},
        "customer_prompt": {"type": "string", "maxLength": 20_000},
    },
    "required": ["skill_name", "input_values"],
    "additionalProperties": False,
}


def build_server(*, root: Path | None = None, runner_factory=None):
    try:
        from mcp.server import MCPServer
    except ImportError as exc:
        raise RuntimeError(
            'Install the optional MCP dependency with: pip install -e ".[mcp]"'
        ) from exc

    server = MCPServer("AI Mining Network skills")
    project_root = (root or Path(os.environ.get("AMN_ROOT", Path.cwd()))).resolve()
    skills_root = (project_root / "skills").resolve()
    model = os.environ.get("AMN_OLLAMA_MODEL", "qwen2.5:1.5b")
    ollama_url = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    make_runner = runner_factory or OllamaDocumentRunner

    def execute(
        skill_name: str,
        input_values: dict,
        customer_prompt: str,
        cancel_event: threading.Event,
    ):
        try:
            validate_json_value(
                {
                    "skill_name": skill_name,
                    "input_values": input_values,
                    "customer_prompt": customer_prompt,
                },
                MCP_SKILL_INPUT_SCHEMA,
                "MCP tool arguments",
            )
        except SkillPackageError as exc:
            raise ToolInvocationError("MALFORMED_ARGUMENTS", str(exc)) from exc
        try:
            arguments_bytes = json.dumps(
                input_values, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ToolInvocationError(
                "MALFORMED_ARGUMENTS", "input_values must be finite JSON values"
            ) from exc
        if len(arguments_bytes) > MAX_MCP_INPUT_BYTES:
            raise ToolInvocationError(
                "INPUT_TOO_LARGE", "MCP skill inputs exceed the 1 MiB limit"
            )
        if (
            not skill_name.isascii()
            or skill_name in {".", ".."}
            or "/" in skill_name
            or "\\" in skill_name
        ):
            raise ToolInvocationError(
                "SKILL_DENIED", "skill_name must be a local published skill identifier"
            )
        package = (skills_root / skill_name).resolve()
        try:
            package.relative_to(skills_root)
        except ValueError as exc:
            raise ToolInvocationError(
                "SKILL_DENIED", "skill package escaped the local skills directory"
            ) from exc
        manifest = read_skill(package)
        if manifest["name"] != skill_name:
            raise ToolInvocationError(
                "SKILL_DENIED", "skill name does not match the published package"
            )
        if "ollama.chat@1.0.0" not in manifest["allowedTools"]:
            raise ToolInvocationError(
                "TOOL_DENIED", "skill did not declare the versioned Ollama chat tool"
            )
        validate_json_value(input_values, manifest["inputSchema"], "skill input")
        runner = make_runner(model, ollama_url)
        document_name = str(
            input_values.get("documentName")
            or input_values.get("invoiceId")
            or "mcp-input"
        )
        document_text = str(
            input_values.get(
                "documentText", json.dumps(input_values, ensure_ascii=False)
            )
        )
        result = runner.run(
            skill_name=skill_name,
            skill_instructions=read_skill_instructions(package),
            prompt=customer_prompt,
            document_name=document_name,
            document_text=document_text,
            input_schema=manifest["inputSchema"],
            input_values=input_values,
            output_schema=manifest["outputSchema"],
            allowed_tools=manifest["allowedTools"],
            requirements=manifest["requirements"],
            timeout_seconds=manifest["requirements"].get("timeoutSeconds", 600),
            cancel_event=cancel_event,
        )
        return {
            "skillName": skill_name,
            "skillHash": manifest["contentHash"],
            "result": result,
        }

    async def cancellable_execute(
        skill_name: str, input_values: dict, customer_prompt: str = ""
    ) -> dict:
        cancel_event = threading.Event()
        try:
            result = await asyncio.to_thread(
                execute, skill_name, input_values, customer_prompt, cancel_event
            )
            return {"ok": True, "result": result}
        except asyncio.CancelledError:
            cancel_event.set()
            raise
        except ToolInvocationError as exc:
            return {"ok": False, "error": exc.as_dict()}
        except SkillExecutionError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        except (SkillPackageError, ValueError) as exc:
            return {
                "ok": False,
                "error": {"code": "INVALID_SKILL_INPUT", "message": str(exc)},
            }
        except Exception as exc:  # MCP boundary converts unexpected failures to a bounded error receipt
            return {
                "ok": False,
                "error": {
                    "code": "WORKER_FAILED",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            }
        finally:
            cancel_event.set()

    @server.tool()
    async def amn_skill_execute_v1(
        skill_name: str, input_values: dict, customer_prompt: str = ""
    ) -> dict:
        """Run one published declarative skill. Only its declared versioned tools are available."""
        return await cancellable_execute(skill_name, input_values, customer_prompt)

    @server.tool()
    async def extract_invoice_v1(invoice_id: str, document_text: str) -> dict:
        """Compatibility tool for one invoice, routed through the same isolated worker."""
        return await cancellable_execute(
            "invoice-extractor",
            {"invoiceId": invoice_id, "documentText": document_text},
            "Extract the invoice fields requested by this skill.",
        )

    return server


def main() -> None:
    server = build_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
