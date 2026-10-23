"""Local registry adapter for public skills; OrbitDB is a production adapter seam."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .skill_packages import SkillPackageError, compute_skill_hash, inspect_skill

__all__ = [
    "LocalSkillRegistry",
    "SkillRegistryError",
    "compute_skill_hash",
    "read_skill",
    "read_skill_instructions",
]


class SkillRegistryError(SkillPackageError):
    pass


def read_skill(directory: Path) -> dict:
    try:
        manifest, _body, _text = inspect_skill(directory)
    except SkillPackageError as exc:
        raise SkillRegistryError(str(exc)) from exc
    return manifest


def read_skill_instructions(directory: Path) -> str:
    try:
        _manifest, body, _text = inspect_skill(directory)
    except SkillPackageError as exc:
        raise SkillRegistryError(str(exc)) from exc
    return body


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
