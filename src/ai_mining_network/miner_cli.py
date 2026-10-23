"""Local miner capability advertisement CLI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from eth_account import Account

from .identity import load_or_create_private_key
from .invoice import OllamaExtractor
from .models import MinerCapabilities
from .registry import LocalSkillRegistry, read_skill
from .signing import create_message


def main() -> None:
    parser = argparse.ArgumentParser(prog="amn-miner", description="Local AI Mining Network miner tools")
    parser.add_argument("command", choices=["capabilities"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--model", default=os.environ.get("AMN_OLLAMA_MODEL", "qwen2.5:1.5b"))
    args = parser.parse_args()
    root = args.root.resolve()
    skill = read_skill(root / "skills" / "invoice-extractor")
    LocalSkillRegistry(root / ".amn" / "registry.sqlite3").publish(root / "skills" / "invoice-extractor")
    private_key = load_or_create_private_key(root, "miner")
    account = Account.from_key(private_key)
    model_digests = []
    ollama_ready = False
    try:
        model_digests = [OllamaExtractor(args.model).model_digest]
        ollama_ready = True
    except Exception as exc:
        print(f"Ollama is not ready; advertisement will be marked unavailable: {exc}")
    try:
        import psutil

        ram = int(psutil.virtual_memory().total)
    except ImportError:
        ram = 1_073_741_824
    capabilities = MinerCapabilities(
        peerId=account.address,
        paymentAddress=account.address,
        cpuCores=max(1, os.cpu_count() or 1),
        ramBytes=ram,
        gpuModels=[],
        modelDigests=model_digests,
        skillHashes=[skill["contentHash"]],
        supportedRuntimes=["python-local", "ollama-local"],
        available=ollama_ready,
    )
    signed = create_message(capabilities, private_key)
    target = root / ".amn" / "capabilities" / "miner.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(signed.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")
    print(json.dumps({"advertisement": str(target), "capabilities": capabilities.model_dump(mode="json"), "signature": signed.signature}, indent=2))
    print("This command writes a local development advertisement; it does not connect to a peer-to-peer marketplace.")


if __name__ == "__main__":
    main()
