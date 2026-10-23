"""Run one private prompt through the isolated local Ollama skill worker."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_mining_network.document_tasks import OllamaDocumentRunner
from ai_mining_network.registry import read_skill, read_skill_instructions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--skill", default="document-assistant")
    parser.add_argument("--model", default="qwen3:4b-instruct")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    args = parser.parse_args()

    skill_dir = args.root / "skills" / args.skill
    manifest = read_skill(skill_dir)
    values = {
        "customerPrompt": "Summarize this text in one concise sentence.",
        "documentName": "worker-smoke-test.txt",
        "documentText": "The local worker sends bounded, schema-constrained requests to Ollama.",
    }
    runner = OllamaDocumentRunner(args.model, args.ollama_url)
    output = runner.run(
        skill_name=manifest["name"],
        skill_instructions=read_skill_instructions(skill_dir),
        prompt=values["customerPrompt"],
        document_name=values["documentName"],
        document_text=values["documentText"],
        input_schema=manifest["inputSchema"],
        input_values=values,
        output_schema=manifest["outputSchema"],
        allowed_tools=manifest["allowedTools"],
        requirements=manifest["requirements"],
        timeout_seconds=manifest["requirements"]["timeoutSeconds"],
    )
    print(
        json.dumps(
            {
                "skill": manifest["name"],
                "skillHash": manifest["contentHash"],
                "model": args.model,
                "modelDigest": runner.model_digest,
                "output": output,
                "metrics": runner.last_metrics,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
