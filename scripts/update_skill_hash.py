"""Refresh an AMN skill package's manifest-bound content hash."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_mining_network.skill_packages import compute_skill_hash


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "skill_dir", type=Path, help="directory containing SKILL.md and manifest.json"
    )
    args = parser.parse_args()
    manifest_path = args.skill_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["contentHash"] = compute_skill_hash(args.skill_dir)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(manifest["contentHash"])


if __name__ == "__main__":
    main()
