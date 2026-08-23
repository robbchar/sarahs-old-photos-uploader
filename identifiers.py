"""Minting the NUMBER half of an identifier, and reading a row's lifecycle
state from the Sheet's two tool-owned columns.

Identifiers are permanent once uploaded, so this module never reuses a number:
gaps left by a crashed run stay gaps. Identifiers are bounded by NUMBER_WIDTH (5 digits, max 99999);
exceeding this raises ValueError to prevent silent wrapping into duplicate identifiers.
See docs/DECISIONS.md, "Identifiers are minted by upload and written back to the Sheet"."""
from __future__ import annotations

import re
from collections.abc import Iterable
from enum import Enum

NUMBER_WIDTH = 5
_IDENTIFIER_RE = re.compile(r"^(?P<collection>[a-z0-9]+)-(?P<project>[a-z0-9]+)-(?P<number>\d{5})$")


def format_identifier(collection_key: str, project_id: str, number: int) -> str:
    max_number = 10 ** NUMBER_WIDTH - 1
    if number > max_number:
        raise ValueError(
            f"Identifier number {number} exceeds {NUMBER_WIDTH}-digit maximum ({max_number}). "
            f"Project has exhausted its identifier space; scheme requires widening."
        )
    return f"{collection_key}-{project_id}-{number:0{NUMBER_WIDTH}d}"


def parse_identifier(identifier: str) -> tuple[str, str, int] | None:
    """(collection_key, project_id, number), or None if `identifier` does not
    match the scheme.

    The one place the scheme is decoded. ia_bulk.py used to carry its own
    copy of the pattern for the same job, including a literal `\\d{5}` that
    NUMBER_WIDTH here parameterizes - so widening the scheme (which
    format_identifier tells you to do once a project exhausts 99999) would
    have left that copy rejecting every new identifier as malformed, failing
    a whole Sheet for a reason that is not real."""
    match = _IDENTIFIER_RE.match(identifier.strip())
    if match is None:
        return None
    return match.group("collection"), match.group("project"), int(match.group("number"))


def parse_number(identifier: str) -> int | None:
    parsed = parse_identifier(identifier)
    return parsed[2] if parsed else None


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
    EXISTING identifier, never re-minted.

    Reads `ia_identifier`, not `identifier`: the real Sheet's own
    `Identifier` column holds the donor's original archival reference
    (e.g. "CD 1 01 53 58 1 Central SS"), not a minted IA identifier. Reading
    that column here was the exact defect that made all 234 real rows
    report as "reserved but invalid" - see docs/DECISIONS.md, "Tool-owned
    Sheet columns are all `ia_`-prefixed"."""
    if not (row.get("ia_identifier") or "").strip():
        return RowState.UNASSIGNED
    if not (row.get("ia_uploaded") or "").strip():
        return RowState.RESERVED
    return RowState.DONE
