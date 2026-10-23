"""Strict, offline validation for Agent Skills packages and AMN manifests."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import regex as bounded_regex
import yaml
from jsonschema import Draft202012Validator, FormatChecker, validators
from jsonschema.exceptions import SchemaError, ValidationError

from .canonical import canonical_json_bytes
from .models import ResourceRequirements

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
TOOL_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]*@[0-9]+\.[0-9]+\.[0-9]+$")
SUPPORTED_TOOL_IDS = frozenset({"ollama.chat@1.0.0"})
MAX_PACKAGE_BYTES = 10 * 1024 * 1024
MAX_PACKAGE_FILES = 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_SCHEMA_BYTES = 64 * 1024
MAX_SCHEMA_DEPTH = 32
MAX_PATTERN_CHARS = 1024
MAX_SKILL_FILE_BYTES = 1 * 1024 * 1024
MAX_SKILL_LINES = 500
PACKAGE_HASH_DOMAIN = b"AMN-SKILL-PACKAGE-V2\0"
_FORBIDDEN_DIRS = {".git", ".amn", "node_modules", "__pycache__", ".venv"}
_SECRET_FILE_RE = re.compile(
    r"^(?:\.env(?:\..*)?|id_rsa|id_ed25519|.*\.(?:key|pem|p12|pfx))$", re.IGNORECASE
)


class SkillPackageError(ValueError):
    """A package is malformed, unsafe, or outside the supported AMN subset."""


class _UniqueYamlLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueYamlLoader, node, deep: bool = False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise SkillPackageError(
                "SKILL.md YAML metadata keys must be scalar values"
            ) from exc
        if duplicate:
            raise SkillPackageError(f"duplicate SKILL.md metadata key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueYamlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SkillPackageError(f"duplicate JSON property: {key}")
        result[key] = value
    return result


def _read_json(path: Path, *, limit: int) -> dict:
    try:
        if path.stat().st_size > limit:
            raise SkillPackageError(f"{path.name} exceeds the {limit:,}-byte limit")
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkillPackageError(
            f"could not read valid UTF-8 JSON from {path.name}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise SkillPackageError(f"{path.name} must contain a JSON object")
    return value


def _parse_skill_markdown(path: Path) -> tuple[dict, str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SkillPackageError(f"SKILL.md must be readable UTF-8: {exc}") from exc
    if len(text.encode("utf-8")) > MAX_SKILL_FILE_BYTES:
        raise SkillPackageError("SKILL.md exceeds the 1 MiB limit")
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        raise SkillPackageError(
            "SKILL.md must start with YAML frontmatter delimited by ---"
        )
    lines = text.splitlines()
    if len(lines) > MAX_SKILL_LINES:
        raise SkillPackageError(f"SKILL.md exceeds the {MAX_SKILL_LINES}-line limit")
    match = re.match(
        r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, flags=re.DOTALL
    )
    if not match:
        raise SkillPackageError(
            "SKILL.md must contain closed YAML frontmatter followed by its body"
        )
    try:
        metadata = yaml.load(match.group(1), Loader=_UniqueYamlLoader)
    except SkillPackageError:
        raise
    except yaml.YAMLError as exc:
        raise SkillPackageError(f"SKILL.md frontmatter is invalid YAML: {exc}") from exc
    if not isinstance(metadata, dict):
        raise SkillPackageError("SKILL.md frontmatter must be a YAML mapping")

    name = metadata.get("name")
    description = metadata.get("description")
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 64
        or not SKILL_NAME_RE.fullmatch(name)
        or "--" in name
    ):
        raise SkillPackageError(
            "SKILL.md name must be 1–64 lowercase letters, digits, or single hyphens"
        )
    if not isinstance(description, str) or not 1 <= len(description) <= 1024:
        raise SkillPackageError(
            "SKILL.md description must be a non-empty string of at most 1024 characters"
        )
    for key, maximum in (("license", 256), ("compatibility", 500)):
        if key in metadata and (
            not isinstance(metadata[key], str) or not 1 <= len(metadata[key]) <= maximum
        ):
            raise SkillPackageError(
                f"SKILL.md {key} must be a string of at most {maximum} characters"
            )
    if "metadata" in metadata:
        extra = metadata["metadata"]
        if not isinstance(extra, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in extra.items()
        ):
            raise SkillPackageError(
                "SKILL.md metadata must be a mapping of string keys to string values"
            )
    if "allowed-tools" in metadata and not isinstance(metadata["allowed-tools"], str):
        raise SkillPackageError(
            "SKILL.md allowed-tools must be a space-separated string"
        )
    body = text[match.end() :]
    return metadata, body, text


def _schema_depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_SCHEMA_DEPTH:
        raise SkillPackageError(
            f"input/output schema exceeds the {MAX_SCHEMA_DEPTH}-level nesting limit"
        )
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                raise SkillPackageError(
                    "JSON Schema references are disabled; inline schema definitions instead"
                )
            _schema_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _schema_depth(item, depth + 1)


def _bounded_pattern(validator, pattern: str, instance: Any, schema: dict):
    if not isinstance(instance, str):
        return
    try:
        matched = bounded_regex.search(pattern, instance, timeout=0.05)
    except TimeoutError:
        yield ValidationError("pattern matching exceeded the 50 ms limit")
        return
    except bounded_regex.error as exc:
        yield ValidationError(
            f"pattern is not supported by the bounded regex engine: {exc}"
        )
        return
    if matched is None:
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


_BoundedValidator = validators.extend(
    Draft202012Validator, {"pattern": _bounded_pattern}
)


def validate_schema_definition(schema: Any, label: str) -> dict:
    if not isinstance(schema, dict):
        raise SkillPackageError(f"{label} must be a JSON Schema object")
    try:
        encoded = canonical_json_bytes(schema)
    except (TypeError, ValueError) as exc:
        raise SkillPackageError(
            f"{label} must use canonical JSON-compatible values: {exc}"
        ) from exc
    if len(encoded) > MAX_SCHEMA_BYTES:
        raise SkillPackageError(
            f"{label} exceeds the {MAX_SCHEMA_BYTES:,}-byte schema limit"
        )
    _schema_depth(schema)
    for pattern in _patterns(schema):
        if len(pattern) > MAX_PATTERN_CHARS:
            raise SkillPackageError(
                f"{label} contains a pattern longer than {MAX_PATTERN_CHARS} characters"
            )
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise SkillPackageError(
            f"{label} is not a valid JSON Schema 2020-12 document: {exc.message}"
        ) from exc
    return schema


def validate_json_value(value: Any, schema: dict, label: str) -> None:
    validate_schema_definition(schema, f"{label} schema")
    try:
        encoded = canonical_json_bytes(value)
        if len(encoded) > MAX_PACKAGE_BYTES:
            raise SkillPackageError(
                f"{label} exceeds the {MAX_PACKAGE_BYTES:,}-byte JSON limit"
            )
        _BoundedValidator(schema, format_checker=FormatChecker()).validate(value)
    except SkillPackageError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise SkillPackageError(
            f"{label} is not valid protocol-safe JSON: {exc}"
        ) from exc
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "$"
        raise SkillPackageError(f"{label} at {location}: {exc.message}") from exc


def _patterns(schema: Any):
    if isinstance(schema, dict):
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            yield pattern
        for value in schema.values():
            yield from _patterns(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _patterns(value)


def _validate_relative_path(value: str, *, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise SkillPackageError(f"{label} must be a non-empty relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or ":" in path.parts[0]
    ):
        raise SkillPackageError(
            f"{label} must not be absolute or traverse outside the package"
        )
    return path


@dataclass(frozen=True)
class _PackageFile:
    path: Path
    relative: PurePosixPath
    size: int


def _collect_files(root: Path) -> list[_PackageFile]:
    files: list[_PackageFile] = []
    total = 0
    try:
        entries = sorted(
            root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
        )
    except OSError as exc:
        raise SkillPackageError(f"could not enumerate skill package: {exc}") from exc
    for path in entries:
        relative_text = path.relative_to(root).as_posix()
        relative = _validate_relative_path(relative_text, label="package path")
        if path.is_symlink():
            raise SkillPackageError(
                f"symbolic links are not allowed in skill packages: {relative_text}"
            )
        if any(part in _FORBIDDEN_DIRS for part in relative.parts):
            raise SkillPackageError(
                f"skill package contains forbidden directory: {relative.parts[0]}"
            )
        if _SECRET_FILE_RE.fullmatch(path.name):
            raise SkillPackageError(
                f"skill package contains a secret-like file: {relative_text}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise SkillPackageError(
                f"unsupported filesystem entry in skill package: {relative_text}"
            )
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise SkillPackageError(
                f"{relative_text} exceeds the {MAX_FILE_BYTES:,}-byte per-file limit"
            )
        total += size
        if total > MAX_PACKAGE_BYTES:
            raise SkillPackageError(
                f"skill package exceeds the {MAX_PACKAGE_BYTES:,}-byte total limit"
            )
        files.append(_PackageFile(path, relative, size))
        if len(files) > MAX_PACKAGE_FILES:
            raise SkillPackageError(
                f"skill package exceeds the {MAX_PACKAGE_FILES}-file limit"
            )
    return files


def _referenced_paths(skill_body: str) -> set[str]:
    result: set[str] = set()
    for match in re.finditer(r"!?\[[^\]]*\]\(([^)]+)\)", skill_body):
        raw = match.group(1).strip().split(maxsplit=1)[0].strip("<>")
        if (
            raw
            and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw)
            and not raw.startswith("#")
        ):
            result.add(raw.split("#", 1)[0])
    for match in re.finditer(
        r"(?<![\w:])(?:\./)?(?:references|assets|scripts)/[A-Za-z0-9_./-]+", skill_body
    ):
        result.add(match.group(0).removeprefix("./").rstrip(".,:;)]}"))
    return result


def _validate_references(root: Path, skill_body: str, known: set[str]) -> None:
    for raw in sorted(_referenced_paths(skill_body)):
        path = _validate_relative_path(raw, label="SKILL.md resource reference")
        normalized = path.as_posix()
        target = root.joinpath(*path.parts)
        try:
            resolved = target.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise SkillPackageError(
                f"SKILL.md reference does not resolve inside the package: {raw}"
            ) from exc
        if resolved.is_symlink() or normalized not in known or not resolved.is_file():
            raise SkillPackageError(
                f"SKILL.md reference is not a packaged regular file: {raw}"
            )


def _validate_manifest(manifest: dict, root: Path, frontmatter: dict) -> dict:
    name = manifest.get("name")
    if (
        not isinstance(name, str)
        or not SKILL_NAME_RE.fullmatch(name)
        or "--" in name
        or len(name) > 64
    ):
        raise SkillPackageError(
            "manifest name must follow the Agent Skills lowercase name convention"
        )
    if name != root.name or frontmatter.get("name") != name:
        raise SkillPackageError(
            "manifest name, SKILL.md name, and parent directory name must match"
        )
    version = manifest.get("version")
    if not isinstance(version, str) or not SEMVER_RE.fullmatch(version):
        raise SkillPackageError(
            "manifest version must be a semantic version such as 1.0.0"
        )
    if manifest.get("skillFile", "SKILL.md") != "SKILL.md":
        raise SkillPackageError(
            "the Agent Skills entry point must be the root SKILL.md file"
        )
    if (
        "description" in manifest
        and manifest["description"] != frontmatter["description"]
    ):
        raise SkillPackageError("manifest description must match SKILL.md frontmatter")
    input_schema = validate_schema_definition(
        manifest.get("inputSchema"), "manifest inputSchema"
    )
    output_schema = validate_schema_definition(
        manifest.get("outputSchema"), "manifest outputSchema"
    )
    if input_schema.get("type") != "object":
        raise SkillPackageError("manifest inputSchema must describe an object")
    allowed_tools = manifest.get("allowedTools", [])
    if not isinstance(allowed_tools, list) or any(
        not isinstance(item, str) for item in allowed_tools
    ):
        raise SkillPackageError(
            "manifest allowedTools must be a list of versioned tool identifiers"
        )
    if len(set(allowed_tools)) != len(allowed_tools):
        raise SkillPackageError("manifest allowedTools must not contain duplicates")
    for tool_id in allowed_tools:
        if not TOOL_ID_RE.fullmatch(tool_id):
            raise SkillPackageError(f"tool identifier is not versioned: {tool_id!r}")
        if tool_id not in SUPPORTED_TOOL_IDS:
            raise SkillPackageError(f"tool is not supported by this runtime: {tool_id}")
    execution_mode = manifest.get("executionMode", "declarative")
    if execution_mode != "declarative":
        raise SkillPackageError(
            "script execution is disabled; only declarative skills are supported"
        )
    for key in ("requiredModels", "publicBenchmarkIds"):
        value = manifest.get(key, [])
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise SkillPackageError(
                f"manifest {key} must be a list of non-empty strings"
            )
    try:
        requirements = ResourceRequirements.model_validate(
            manifest.get("requirements", {})
        )
    except Exception as exc:
        raise SkillPackageError(f"manifest requirements are invalid: {exc}") from exc
    if manifest.get("verificationStrategy") not in {
        "per-record",
        "per-document",
        "per-image",
        "deterministic-batch",
    }:
        raise SkillPackageError("manifest verificationStrategy is not supported")
    manifest["inputSchema"] = input_schema
    manifest["outputSchema"] = output_schema
    manifest["allowedTools"] = allowed_tools
    manifest["requiredModels"] = manifest.get("requiredModels", [])
    manifest["publicBenchmarkIds"] = manifest.get("publicBenchmarkIds", [])
    manifest["skillFile"] = "SKILL.md"
    manifest["requirements"] = requirements.model_dump(mode="json")
    manifest["executionMode"] = "declarative"
    manifest["description"] = frontmatter["description"]
    return manifest


def _compute_package_hash(root: Path, manifest: dict, files: list[_PackageFile]) -> str:
    digest = hashlib.sha256(PACKAGE_HASH_DOMAIN)
    normalized_manifest = {
        key: value for key, value in manifest.items() if key != "contentHash"
    }
    try:
        manifest_bytes = canonical_json_bytes(normalized_manifest)
    except (TypeError, ValueError) as exc:
        raise SkillPackageError(
            f"manifest must use canonical JSON-compatible values: {exc}"
        ) from exc
    digest.update(len(manifest_bytes).to_bytes(8, "big"))
    digest.update(manifest_bytes)
    for item in files:
        if item.relative.as_posix() == "manifest.json":
            continue
        relative_bytes = item.relative.as_posix().encode("utf-8")
        payload = item.path.read_bytes()
        digest.update(len(relative_bytes).to_bytes(4, "big"))
        digest.update(relative_bytes)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def inspect_skill(directory: Path) -> tuple[dict, str, str]:
    if directory.is_symlink() or not directory.is_dir():
        raise SkillPackageError(
            "skill package must be a real directory, not a symbolic link"
        )
    root = directory.resolve(strict=True)
    files = _collect_files(root)
    known_paths = {item.relative.as_posix() for item in files}
    if "SKILL.md" not in known_paths or "manifest.json" not in known_paths:
        raise SkillPackageError(
            "skill package must include root SKILL.md and manifest.json files"
        )
    skill_meta, skill_body, full_text = _parse_skill_markdown(root / "SKILL.md")
    manifest = _read_json(root / "manifest.json", limit=MAX_MANIFEST_BYTES)
    manifest = _validate_manifest(manifest, root, skill_meta)
    _validate_references(root, skill_body, known_paths)
    content_hash = _compute_package_hash(root, manifest, files)
    expected = manifest.get("contentHash")
    if expected not in (None, "", content_hash):
        raise SkillPackageError(
            f"skill content hash mismatch: manifest={expected}, actual={content_hash}"
        )
    manifest["contentHash"] = content_hash
    return manifest, skill_body, full_text


def compute_skill_hash(directory: Path) -> str:
    """Return the V2 package digest, binding the manifest and every package file."""
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink():
        raise SkillPackageError("manifest.json may not be a symbolic link")
    manifest = _read_json(manifest_path, limit=MAX_MANIFEST_BYTES)
    if directory.is_symlink() or not directory.is_dir():
        raise SkillPackageError("skill package must be a real directory")
    root = directory.resolve(strict=True)
    files = _collect_files(root)
    if "SKILL.md" not in {item.relative.as_posix() for item in files}:
        raise SkillPackageError("skill package must include root SKILL.md")
    skill_meta, _, _ = _parse_skill_markdown(root / "SKILL.md")
    normalized = _validate_manifest(manifest, root, skill_meta)
    return _compute_package_hash(root, normalized, files)
