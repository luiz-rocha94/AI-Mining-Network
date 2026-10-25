"""Generate a direct and installed dependency license inventory."""

from __future__ import annotations

import importlib.metadata as metadata
import json
from collections import Counter, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_DIRECT = [
    "pydantic",
    "pypdf",
    "openpyxl",
    "reportlab",
    "eth-account",
    "eth-abi",
    "eth-utils",
    "jsonschema",
    "pyyaml",
    "psutil",
    "regex",
    "pytest",
    "ruff",
    "mcp",
]
NODE_DIRECT = [
    "@ai-mining-network/golem",
    "@ipld/dag-cbor",
    "@orbitdb/core",
    "@helia/bitswap",
    "@helia/libp2p",
    "@helia/unixfs",
    "@libp2p/bootstrap",
    "@libp2p/kad-dht",
    "@libp2p/ping",
    "@libp2p/crypto",
    "@libp2p/gossipsub",
    "@libp2p/identify",
    "@libp2p/mdns",
    "@libp2p/memory",
    "@libp2p/noise",
    "@libp2p/tcp",
    "@libp2p/yamux",
    "@multiformats/multiaddr",
    "blockstore-fs",
    "datastore-core",
    "datastore-level",
    "helia",
    "multiformats",
    "@openzeppelin/contracts",
    "@openzeppelin/merkle-tree",
    "@nomicfoundation/hardhat-ethers",
    "ethers",
    "hardhat",
    "solc",
    "@vitejs/plugin-react",
    "vite",
    "typescript",
    "react",
    "react-dom",
    "ajv",
    "ajv-formats",
    "libp2p",
    "@golem-sdk/golem-js",
]


def _license_from_metadata(distribution) -> str:
    value = distribution.metadata.get(
        "License-Expression"
    ) or distribution.metadata.get("License")
    if value and value.strip() not in {"UNKNOWN", ""}:
        return value.replace("\n", " ").strip()
    classifiers = [
        item.partition("::")[2].strip()
        for item in distribution.metadata.get_all("Classifier", [])
        if item.startswith("License ::")
    ]
    return "; ".join(classifiers) if classifiers else "UNKNOWN — review required"


def python_inventory() -> dict[str, dict]:
    installed = {
        dist.metadata["Name"].lower().replace("_", "-"): dist
        for dist in metadata.distributions()
        if dist.metadata.get("Name")
    }
    queue = deque(name.lower().replace("_", "-") for name in PYTHON_DIRECT)
    result = {}
    while queue:
        name = queue.popleft()
        if name in result or name not in installed:
            continue
        dist = installed[name]
        result[name] = {
            "version": dist.version,
            "license": _license_from_metadata(dist),
        }
        for requirement in dist.requires or []:
            try:
                from packaging.requirements import Requirement

                parsed = Requirement(requirement)
                if parsed.marker is None or parsed.marker.evaluate():
                    queue.append(parsed.name.lower().replace("_", "-"))
            except Exception:
                continue
    return result


def node_inventory() -> dict[str, dict]:
    result = {}
    store = ROOT / "node_modules" / ".pnpm"
    if store.exists():
        for path in store.rglob("package.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            name, version = item.get("name"), item.get("version")
            if not name or not version:
                continue
            key = f"{name}@{version}"
            result[key] = {
                "name": name,
                "version": version,
                "license": item.get("license", "UNKNOWN — review required"),
            }
    return result


def main() -> None:
    py = python_inventory()
    node = node_inventory()
    direct_node = set(NODE_DIRECT)
    direct_python = {item.lower().replace("_", "-") for item in PYTHON_DIRECT}
    counts = Counter(item["license"] for item in node.values())
    lines = [
        "# Dependency license report",
        "",
        "Generated from Python distribution metadata and installed pnpm package manifests by `scripts/license_report.py`. Python inventory is the installed closure reachable from runtime/dev/optional direct dependencies; Node inventory is every resolved package version in `node_modules/.pnpm`. Regenerate after installing/updating dependencies. License metadata is not legal advice; review packages whose manifest says unknown or offers a custom expression.",
        "",
        "## Python dependencies",
        "",
        "| Package | Version | License metadata | Direct/optional |",
        "|---|---:|---|---|",
    ]
    for name, item in sorted(py.items()):
        lines.append(
            f"| `{name}` | {item['version']} | {item['license']} | {'yes' if name in direct_python else 'transitive'} |"
        )
    lines += [
        "",
        "## Node dependencies",
        "",
        f"Resolved packages: **{len(node)}**. License metadata counts: "
        + ", ".join(f"`{name}` {count}" for name, count in sorted(counts.items()))
        + ".",
        "",
        "| Package | Version | License metadata | Direct dependency |",
        "|---|---:|---|---|",
    ]
    for key, item in sorted(node.items()):
        lines.append(
            f"| `{item['name']}` | {item['version']} | {item['license']} | {'yes' if item['name'] in direct_node else 'transitive'} |"
        )
    target = ROOT / "docs" / "reports" / "dependency-licenses.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {target} ({len(py)} Python distributions, {len(node)} Node packages)")


if __name__ == "__main__":
    main()
