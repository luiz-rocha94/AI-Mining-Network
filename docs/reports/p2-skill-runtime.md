# P2 skill runtime implementation report

**Status:** implementation and local automated validation passed on 2026-10-23;
maintainer review is the gate before P3. This report describes the supported
declarative local runtime. It does not claim production sandbox isolation.

## Implemented

- `skill_packages.py` safely parses Agent Skills YAML frontmatter, rejects
  duplicate YAML/JSON keys, checks required name/description fields and parent
  directory identity, validates inline JSON Schema 2020-12, disables external
  schema references, bounds regular-expression evaluation, resolves packaged
  resources without traversal, and rejects symlinks, secret-like files, and
  oversized packages.
- Skill packages use `AMN-SKILL-PACKAGE-V2\0`. The digest binds normalized
  canonical manifest bytes (except the `contentHash` field; defaults and
  SKILL.md description are materialized) and all sorted package files except
  root `manifest.json`. Both publication and task execution revalidate
  the package. `scripts/update_skill_hash.py <skill-directory>` refreshes the
  embedded hash.
- Skills are declarative. The runtime rejects a non-declarative execution mode
  and never launches scripts included in a package. The only implemented
  versioned tool is `ollama.chat@1.0.0`; there is no shell or filesystem tool.
- `OllamaDocumentRunner` starts a separate fixed Python worker with bounded JSON
  stdin/stdout, schema-checked model output, a temporary working/home
  directory, and a filtered environment without inherited credential
  variables. The process tree is monitored for CPU time, resident memory, and
  deadlines; timeout and cancellation terminate it; temporary files are
  removed after success or failure.
- Local Ollama is the default. Remote inference requires HTTPS and
  `AMN_ALLOW_REMOTE_OLLAMA=1`. The MCP v2 adapter exposes generic published
  skill execution and preserves the invoice compatibility tool. MCP is an
  optional extra and is imported only when the server is started.
- Skills that require per-task GPU/VRAM or arbitrary network access are rejected
  by the local Ollama adapter.

## Protocol and compatibility decisions

The skill package validator follows the published Agent Skills metadata
requirements (`name`, `description`, optional `license`, `compatibility`,
`metadata`, and `allowed-tools` text) and applies additional AMN package and
execution rules. AMN permissions come from its versioned manifest allow-list;
Agent Skills `allowed-tools` text does not grant runtime authority. The
`skills-ref` repository describes itself as a demo validator, so it was not
added as a production validation dependency.

The MCP server was installed and exercised with Python 3.12 and MCP SDK 2.3.0.
The optional dependency is restricted to Python 3.11+, while importing the core
Python modules does not require it. Ollama's documented `/api/chat` structured
output interface is used by sending the skill output JSON Schema in `format`.

Protocol v2 canonical JSON accepts safe integers but not floating-point values.
AMN skill manifests and committed outputs follow that subset: use integer minor
units or exact decimal strings for amounts. A Merkle root and a successful
schema check still do not prove that AI inference occurred or that an answer is
factually correct.

## Validation performed

`tests/test_skill_runtime.py` contains 20 cases covering:

- publication/discovery, package hash binding, Agent Skills frontmatter, and
  packaged references;
- duplicate keys, malformed schemas, disabled external references, traversal,
  missing resources, secret-like files, and per-file size limits;
- fail-closed GPU/network requirements and versioned-tool input validation;
- timeout, CPU-time, and RSS process-tree termination, sanitized environment,
  and scratch cleanup;
- a local mock Ollama exchange that checks structured output, no model tools,
  prompt-injection containment at the tool boundary, and inert package scripts;
- MCP tool listing, invalid/oversized inputs, denied skills, worker failures,
  cancellation, and optional-extra import behavior.
- structured `INVALID_MODEL_OUTPUT` errors when model JSON fails the declared
  output schema.

The isolated worker was also run against the real local `qwen3:4b-instruct`
Ollama model with a non-sensitive prompt. It returned schema-valid
`{"response":"The local worker sends bounded, schema-constrained requests to Ollama."}`
for model digest `0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`
in 1,799 ms (286 prompt tokens, 20 output tokens). This is a local smoke test,
not a benchmark or cryptographic proof of inference.

Reproduce with:

```powershell
python -m pip install -e ".[dev,mcp]"
python -m pytest tests/test_skill_runtime.py -vv
python scripts/smoke_ollama_worker.py --model qwen3:4b-instruct
```

Repository-wide validation passed with `pnpm test:all`: 4 customer-web tests,
6 protocol tests, TypeScript protocol typecheck, 4 Hardhat contract tests, and
69 Python tests. Changed P2 Python files also pass Ruff import/syntax checks
(`ruff check --select E4,E7,E9,F,I ...`).
Dependency metadata is regenerated in
[`dependency-licenses.md`](dependency-licenses.md).

## Tested boundary and remaining limits

- CPU/RSS are process-tree monitor-and-kill thresholds, not hard kernel quotas;
  polling allows overshoot. Ollama runs as a separate daemon, so its memory,
  GPU/VRAM, and lifetime are outside the worker limit.
- A scratch `cwd`, `HOME`, and `TMPDIR` do not create a filesystem jail. The
  worker runs with the miner's OS identity and could access files available to
  that account if fixed worker code were compromised.
- Network access is constrained by the implemented application tool surface
  and configured Ollama origin, not by an OS firewall or network namespace.
- The worker does not receive uploaded file paths or inherited secret
  environment variables. This does not protect against other processes running
  under the same OS account.
- Prompt instructions alone are not a security control. The local defense is
  that the model receives no shell, filesystem, or arbitrary network tool.
- A separate container/VM or equivalent OS boundary, hard resource controls,
  model-process accounting, secure secrets, and independent red-team review are
  required before production use. The unfinished items remain in
  [`production-readiness.md`](../production-readiness.md).

## Official references

- [Agent Skills specification](https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) and
  [tool documentation](https://py.sdk.modelcontextprotocol.io/servers/tools/)
- [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs)
