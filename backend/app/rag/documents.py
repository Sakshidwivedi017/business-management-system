"""Retrieval documents built from genuine catalogue data. One source record is one document.

The longest source text (a classification guide entry) is a few thousand
characters, far below the embedding model's input limit, so nothing is chunked.
Text is built deterministically, so an unchanged record always hashes the same.
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection

from app.db.repositories import knowledge as repo

ITEM = "item"
ITEM_CLASSIFICATION = "item_classification"
SOURCES = (ITEM, ITEM_CLASSIFICATION)


@dataclass(frozen=True)
class Document:
    source: str
    source_id: str
    title: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        payload = json.dumps([self.title, self.content, self.metadata], sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()


def build_documents(conn: Connection) -> list[Document]:
    items = (item_document(row) for row in repo.list_item_sources(conn))
    guides = (classification_document(row) for row in repo.list_classification_sources(conn))
    return [doc for doc in (*items, *guides) if doc is not None]


def item_document(row: dict[str, Any]) -> Document | None:
    name = _clean(row["name"])
    if name is None:
        return None
    path = _path(row["category_name"], row["sub_category_name"], row["sub_category_2_name"])
    lines = [
        f"Item: {name}",
        f"Item code: {row['item_code']}",
        _line("Category", path),
        _line("Unit", row["unit"]),
        _line("HSN code", row["hsn_code"]),
        "Spare part: yes" if row["is_spare"] else None,
        _line("Notes", row["notes"]),
    ]
    return Document(
        source=ITEM,
        source_id=row["id"],
        title=f"{row['item_code']} – {name}",
        content="\n".join(line for line in lines if line),
        metadata={
            "item_id": row["id"],
            "item_code": row["item_code"],
            "name": name,
            "status": row["status"],
            "category": path,
        },
    )


def classification_document(row: dict[str, Any]) -> Document | None:
    name = _clean(row["name"])
    if name is None:
        return None
    path = _path(row["category_name"], row["sub_category_name"], name)
    lines = [
        f"Item classification: {name} ({row['short_code']})",
        _line("Category", path),
        _line("Standard", row["standard"]),
        _line("Description", row["description"]),
        _line("Naming convention", row["naming_convention"]),
        _bullets("Convention notes", row["convention_notes"]),
        _bullets("Examples", row["examples"]),
        _line("HSN code", row["hsn_code"]),
    ]
    return Document(
        source=ITEM_CLASSIFICATION,
        source_id=row["id"],
        title=f"{name} ({path})",
        content="\n".join(line for line in lines if line),
        metadata={"sub_category_2_id": row["id"], "name": name, "short_code": row["short_code"], "category": path},
    )


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _line(label: str, value: Any) -> str | None:
    value = _clean(value)
    return f"{label}: {value}" if value else None


def _path(*parts: Any) -> str | None:
    return " > ".join(p for p in (_clean(part) for part in parts) if p) or None


def _bullets(label: str, value: Any) -> str | None:
    """Convention notes and examples are stored as JSON lists; anything else is kept as plain text."""
    value = _clean(value)
    if value is None:
        return None
    try:
        entries = json.loads(value)
    except ValueError:
        return f"{label}: {value}"
    if not isinstance(entries, list):
        return f"{label}: {value}"
    entries = [str(entry).strip() for entry in entries if str(entry).strip()]
    return f"{label}:\n" + "\n".join(f"- {entry}" for entry in entries) if entries else None
