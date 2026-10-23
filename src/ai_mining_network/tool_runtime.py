"""Versioned, explicitly authorized tool contracts for declarative skills."""

from __future__ import annotations

import json

from .skill_packages import SkillPackageError, validate_json_value

OLLAMA_CHAT_TOOL_ID = "ollama.chat@1.0.0"
MAX_TOOL_ARGUMENT_BYTES = 2 * 1024 * 1024
OLLAMA_CHAT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "model": {"type": "string", "minLength": 1, "maxLength": 256},
        "baseUrl": {"type": "string", "minLength": 1, "maxLength": 2048},
        "messages": {
            "type": "array",
            "minItems": 1,
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "role": {"enum": ["system", "user"]},
                    "content": {"type": "string", "maxLength": 1_000_000},
                },
                "required": ["role", "content"],
                "additionalProperties": False,
            },
        },
        "outputSchema": {"type": "object"},
        "timeoutSeconds": {"type": "integer", "minimum": 1, "maximum": 600},
        "maxResponseBytes": {"type": "integer", "minimum": 1, "maximum": 2_097_152},
    },
    "required": [
        "model",
        "baseUrl",
        "messages",
        "outputSchema",
        "timeoutSeconds",
        "maxResponseBytes",
    ],
    "additionalProperties": False,
}


class ToolInvocationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def validate_tool_request(
    tool_id: str, arguments: dict, declared_tools: list[str]
) -> None:
    """Reject undeclared, unsupported, oversized, or malformed calls before I/O."""
    if not isinstance(declared_tools, list) or tool_id not in declared_tools:
        raise ToolInvocationError(
            "TOOL_DENIED", f"skill did not declare permission for {tool_id}"
        )
    if tool_id != OLLAMA_CHAT_TOOL_ID:
        raise ToolInvocationError(
            "TOOL_UNAVAILABLE", f"no runtime implementation is registered for {tool_id}"
        )
    if not isinstance(arguments, dict):
        raise ToolInvocationError(
            "MALFORMED_ARGUMENTS", "tool arguments must be a JSON object"
        )
    try:
        encoded = json.dumps(
            arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ToolInvocationError(
            "MALFORMED_ARGUMENTS", "tool arguments must be finite JSON values"
        ) from exc
    if len(encoded) > MAX_TOOL_ARGUMENT_BYTES:
        raise ToolInvocationError(
            "INPUT_TOO_LARGE", "tool arguments exceed the 2 MiB limit"
        )
    try:
        validate_json_value(arguments, OLLAMA_CHAT_INPUT_SCHEMA, "tool arguments")
    except SkillPackageError as exc:
        raise ToolInvocationError("MALFORMED_ARGUMENTS", str(exc)) from exc
