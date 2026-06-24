"""Approved read-only SQL templates."""

import json
from pathlib import Path
from typing import TypedDict


class SqlTemplateMetadata(TypedDict):
    description: str
    risk: str
    source: str


_TEMPLATE_DIR = Path(__file__).with_name("sql_templates")
_MANIFEST_FILE = _TEMPLATE_DIR / "manifest.json"


def _load_templates() -> dict[str, str]:
    templates: dict[str, str] = {}
    for path in sorted(_TEMPLATE_DIR.glob("*.sql")):
        templates[path.stem] = path.read_text(encoding="utf-8").strip()
    return templates


def _load_manifest() -> dict[str, SqlTemplateMetadata]:
    raw = json.loads(_MANIFEST_FILE.read_text(encoding="utf-8"))
    return {
        name: {
            "description": str(meta["description"]),
            "risk": str(meta["risk"]),
            "source": str(meta["source"]),
        }
        for name, meta in raw.items()
    }


SQL_TEMPLATES: dict[str, str] = _load_templates()
SQL_TEMPLATE_METADATA: dict[str, SqlTemplateMetadata] = _load_manifest()


def list_sql_templates() -> str:
    return "\n".join(
        f"- {name}: {SQL_TEMPLATE_METADATA[name]['description']}" for name in sorted(SQL_TEMPLATES)
    )
