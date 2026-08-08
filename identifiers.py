"""Minting the NUMBER half of an identifier, and reading a row's lifecycle
state from the Sheet's two tool-owned columns.

Identifiers are permanent once uploaded, so this module never reuses a number:
gaps left by a crashed run stay gaps. See docs/DECISIONS.md, "Identifiers are
minted by upload and written back to the Sheet"."""
from __future__ import annotations

import re
from collections.abc import Iterable
from enum import Enum

NUMBER_WIDTH = 5
_IDENTIFIER_RE = re.compile(r"^(?P<collection>[a-z0-9]+)-(?P<project>[a-z0-9]+)-(?P<number>\d{5})$")


def format_identifier(collection_key: str, project_id: str, number: int) -> str:
    return f"{collection_key}-{project_id}-{number:0{NUMBER_WIDTH}d}"


def parse_number(identifier: str) -> int | None:
    match = _IDENTIFIER_RE.match(identifier.strip())
    return int(match.group("number")) if match else None


def next_identifiers(
    existing: Iterable[str], collection_key: str, project_id: str, count: int
) -> list[str]:
    highest = 0
    for identifier in existing:
        match = _IDENTIFIER_RE.match(identifier.strip())
        if not match:
            continue
        if match.group("collection") != collection_key or match.group("project") != project_id:
            continue
        highest = max(highest, int(match.group("number")))

    return [
        format_identifier(collection_key, project_id, highest + offset)
        for offset in range(1, count + 1)
    ]


class RowState(Enum):
    UNASSIGNED = "unassigned"
    RESERVED = "reserved"
    DONE = "done"


def classify_row(row: dict[str, str]) -> RowState:
    """RESERVED is the crash-recovery case: a number was written to the Sheet
    but the upload never confirmed. Such a row must be retried under its
    EXISTING identifier, never re-minted."""
    if not (row.get("identifier") or "").strip():
        return RowState.UNASSIGNED
    if not (row.get("ia_uploaded") or "").strip():
        return RowState.RESERVED
    return RowState.DONE
