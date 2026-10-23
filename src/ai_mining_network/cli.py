"""Command-line tools for publishing a skill and running the invoice MVP."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .benchmark import fraud_detection_probability
from .benchmark_service import run_invoice_benchmark
from .identity import load_or_create_private_key
from .invoice import OllamaExtractor
from .models import SkillManifest
from .orchestrator import DemoError, DemoService
from .registry import LocalSkillRegistry, read_skill
from .signing import create_message


def _json(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _run_demo(args) -> int:
    service = DemoService(Path(args.root), args.model, args.ollama_url)
    job = service.create_quote(
        invoice_count=args.count,
        sample_count=args.samples,
        validator_count=args.validators,
        corrupt_count=args.corrupt,
        unavailable_validators=args.unavailable_validator,
        disagree=args.disagree,
        tamper_after_commit=args.tamper_after_commit,
        execution_timeout=args.timeout,
    )
    quote = job["quote"]
    print("Fixed quote — no charge is made until you accept it:")
    print(f"  Task: {job['task']['taskId']}")
    print(f"  Total: {quote['totalAtomic']} {quote['currency']['symbol']} atomic units")
    print(f"  Execution: {quote['executionRewardAtomic']} | verification: {quote['verificationBudgetAtomic']}")
    print(f"  Expires: {quote['expiresAt']}")
    print(f"  Samples: {quote['sampleCount']} | validators: {quote['validatorCount']}")
    if not args.accept:
        try:
            answer = input("Accept this fixed quote and start the local task? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            print("Quote was not accepted.")
            return 0
    service.accept_quote(job["task"]["taskId"])
    deadline = time.monotonic() + args.wait_seconds
    while time.monotonic() < deadline:
        current = service.status(job["task"]["taskId"])
        print(f"[{current['state']}] {current['progress']}")
        if current["state"] in {"SETTLED", "REFUNDED", "DISPUTED"}:
            _json(current)
            return 0 if current["state"] in {"SETTLED", "REFUNDED"} else 2
        time.sleep(1)
    print("Still running; query this task later:")
    print(f"  amn task {job['task']['taskId']}")
    return 3


def main() -> None:
    parser = argparse.ArgumentParser(prog="amn", description="AI Mining Network local MVP tools")
    commands = parser.add_subparsers(dest="command", required=True)

    publish = commands.add_parser("publish-skill", help="validate and publish a local skill package")
    publish.add_argument("path", type=Path, nargs="?", default=Path("skills/invoice-extractor"))

    discover = commands.add_parser("discover-skills", help="list locally registered skills")
    discover.add_argument("query", nargs="?", default="")
    discover.add_argument("--registry", type=Path, default=Path(".amn/registry.sqlite3"))

    benchmark = commands.add_parser("benchmark", help="run and sign a reproducible local skill benchmark")
    benchmark.add_argument("--root", type=Path, default=Path.cwd())
    benchmark.add_argument("--skill", type=Path, default=Path("skills/invoice-extractor"))
    benchmark.add_argument("--model", default="qwen2.5:1.5b")
    benchmark.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    benchmark.add_argument("--samples", type=int, default=20)
    benchmark.add_argument("--output", type=Path, default=Path(".amn/benchmark-contribution"))

    demo = commands.add_parser("demo", help="create and optionally accept a fixed-price invoice task")
    demo.add_argument("--root", type=Path, default=Path.cwd())
    demo.add_argument("--model", default="qwen2.5:1.5b")
    demo.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    demo.add_argument("--count", type=int, default=100)
    demo.add_argument("--samples", type=int, default=30)
    demo.add_argument("--validators", type=int, default=3)
    demo.add_argument("--corrupt", type=int, default=0, help="deliberately corrupt this many outputs in the simulation")
    demo.add_argument("--unavailable-validator", type=int, action="append", default=[], help="zero-based validator index to simulate as unavailable")
    demo.add_argument("--disagree", action="store_true", help="simulate an independent model disagreement")
    demo.add_argument("--tamper-after-commit", action="store_true", help="simulate artifact mutation after the output commitment")
    demo.add_argument("--timeout", action="store_true", help="simulate an execution timeout")
    demo.add_argument("--accept", action="store_true", help="accept without an interactive prompt")
    demo.add_argument("--wait-seconds", type=int, default=7200)

    task = commands.add_parser("task", help="inspect a persisted local task")
    task.add_argument("task_id")
    task.add_argument("--root", type=Path, default=Path.cwd())

    fraud = commands.add_parser("fraud-report", help="print exact sampling detection probabilities")
    fraud.add_argument("--units", type=int, default=100)
    fraud.add_argument("--corrupt", type=int, default=5)
    fraud.add_argument("--samples", type=int, nargs="+", default=[5, 10, 20, 30, 50])

    web = commands.add_parser("web", help="run the local customer web API")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    web.add_argument("--root", type=Path, default=Path.cwd())

    args = parser.parse_args()
    if args.command == "publish-skill":
        skill = read_skill(args.path)
        manifest = SkillManifest.model_validate({key: value for key, value in skill.items() if key in SkillManifest.model_fields})
        private_key = load_or_create_private_key(Path.cwd(), "skill-author")
        signed = create_message(manifest, private_key)
        target = Path(".amn/skills") / (skill["contentHash"].split(":", 1)[1] + ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(signed.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")
        _json(LocalSkillRegistry(Path(".amn/registry.sqlite3")).publish(args.path))
        print(f"Published {skill['name']}@{skill['version']} {skill['contentHash']}")
        print(f"Signed author manifest: {target}")
        return
    if args.command == "discover-skills":
        _json(LocalSkillRegistry(args.registry).discover(args.query))
        return
    if args.command == "benchmark":
        root = args.root.resolve()
        skill_dir = args.skill if args.skill.is_absolute() else root / args.skill
        output_dir = args.output if args.output.is_absolute() else root / args.output
        try:
            receipt, report = run_invoice_benchmark(
                skill_dir,
                output_dir,
                OllamaExtractor(args.model, args.ollama_url),
                sample_count=args.samples,
                private_key=load_or_create_private_key(root, "benchmark-contributor"),
            )
        except (ValueError, RuntimeError) as exc:
            parser.exit(2, f"amn benchmark: {exc}\n")
        _json({"benchmark": receipt.model_dump(mode="json"), "report": report, "receiptFile": str(output_dir / "benchmark-receipt.json")})
        return
    if args.command == "demo":
        try:
            raise SystemExit(_run_demo(args))
        except (DemoError, ValueError) as exc:
            parser.exit(2, f"amn demo: {exc}\n")
    if args.command == "task":
        _json(DemoService(args.root).status(args.task_id))
        return
    if args.command == "fraud-report":
        if args.units < 1 or args.corrupt < 0 or args.corrupt > args.units:
            parser.error("units must be positive and corrupt must be between zero and units")
        _json([
            {"units": args.units, "corrupt": args.corrupt, "samples": sample,
             "detectionProbability": fraud_detection_probability(args.units, args.corrupt, sample)}
            for sample in args.samples
        ])
        return
    if args.command == "web":
        from .web import make_server

        server = make_server(args.host, args.port, args.root)
        print(f"Customer UI API: http://{args.host}:{args.port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
