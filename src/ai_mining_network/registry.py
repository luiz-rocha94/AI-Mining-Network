"""Local registry adapter for public skills; OrbitDB is a production adapter seam."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


class SkillRegistryError(ValueError):
    pass


def compute_skill_hash(directory: Path) -> str:
    """Hash sorted relative paths and file bytes; manifest is metadata, not payload."""
    root = directory.resolve()
    files = sorted(path for path in root.rglob("*") if path.is_file() and path.name != "manifest.json")
    if not files:
        raise SkillRegistryError("skill package contains no payload files")
    digest = hashlib.sha256(b"AMN-SKILL-PACKAGE-V1\0")
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def read_skill(directory: Path) -> dict:
    manifest_path = directory / "manifest.json"
    skill_path = directory / "SKILL.md"
    if not skill_path.is_file() or not manifest_path.is_file():
        raise SkillRegistryError("skill package must include SKILL.md and manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    content_hash = compute_skill_hash(directory)
    expected = manifest.get("contentHash")
    if expected not in (None, "", content_hash):
        raise SkillRegistryError(f"skill content hash mismatch: manifest={expected}, actual={content_hash}")
    frontmatter = skill_path.read_text(encoding="utf-8").split("---", 2)
    if len(frontmatter) != 3 or frontmatter[0].strip():
        raise SkillRegistryError("SKILL.md must start with YAML frontmatter delimited by ---")
    fields = {}
    for line in frontmatter[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip().strip("\"'")
    if not fields.get("name") or not fields.get("description"):
        raise SkillRegistryError("SKILL.md frontmatter requires name and description")
    if fields["name"] != manifest.get("name"):
        raise SkillRegistryError("skill name must match between SKILL.md and manifest")
    manifest["contentHash"] = content_hash
    manifest["description"] = fields["description"]
    return manifest


class LocalSkillRegistry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(sqlite3.connect(path)) as db:
            with db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS skills (content_hash TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL, manifest_json TEXT NOT NULL, package_path TEXT NOT NULL)"
                )

    def publish(self, directory: Path) -> dict:
        manifest = read_skill(directory)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO skills(content_hash,name,version,manifest_json,package_path) VALUES(?,?,?,?,?)",
                    (
                        manifest["contentHash"],
                        manifest["name"],
                        manifest["version"],
                        json.dumps(manifest, sort_keys=True),
                        str(directory.resolve()),
                    ),
                )
        return manifest

    def discover(self, query: str = "") -> list[dict]:
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT manifest_json FROM skills WHERE name LIKE ? ORDER BY name,version",
                (f"%{query}%",),
            ).fetchall()
        return [json.loads(row["manifest_json"]) for row in rows]

