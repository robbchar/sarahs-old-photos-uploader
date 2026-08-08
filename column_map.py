"""Turns a Google Sheet grid into the same row shape read_csv() produces, so
everything downstream cannot tell where its rows came from. See
docs/DECISIONS.md, "The Sheet is read live"."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Hyphens and underscores are preserved because Internet Archive field names use them
# (e.g., "identifier-bib"). Other punctuation is removed.
_PUNCTUATION = re.compile(r"[^a-z0-9\s_-]")
_WHITESPACE = re.compile(r"\s+")
_REPEATED_UNDERSCORE = re.compile(r"_+")


def normalize_header(header: str) -> str:
    """A Sheet header becomes an IA metadata field name by this rule and no
    other. Typos survive on purpose: the Sheet is authoritative, and silently
    correcting a header would decide which field a value lands in."""
    text = _PUNCTUATION.sub("", header.strip().lower())
    text = _WHITESPACE.sub("_", text)
    text = _REPEATED_UNDERSCORE.sub("_", text)
    return text.strip("_")


HELD_BACK_MARKER = "(lcps internal)"
RESERVED_FIELDS = frozenset({"identifier", "file", "ia_uploaded", "ia_url"})


def is_held_back(header: str) -> bool:
    """A header marked (LCPS Internal) is never uploaded. The rule lives in the
    header text so the people writing the data can see it, rather than in a
    config file only the maintainer can read."""
    return HELD_BACK_MARKER in header.lower()


@dataclass(frozen=True)
class ColumnMap:
    headers: list[str]
    field_names: dict[str, str]
    held_back: list[str]

    def uploadable_fields(self) -> list[str]:
        return [
            self.field_names[header]
            for header in self.headers
            if header not in self.held_back
            and self.field_names[header] not in RESERVED_FIELDS
        ]


def build_column_map(headers: list[str]) -> ColumnMap:
    return ColumnMap(
        headers=list(headers),
        field_names={header: normalize_header(header) for header in headers},
        held_back=[header for header in headers if is_held_back(header)],
    )


def grid_to_rows(grid: list[list[str]]) -> tuple[ColumnMap, list[dict[str, str]]]:
    if not grid:
        return build_column_map([]), []

    headers, *data_rows = grid
    column_map = build_column_map(headers)
    rows = [
        {
            column_map.field_names[header]: (row[index] if index < len(row) else "")
            for index, header in enumerate(headers)
        }
        for row in data_rows
    ]
    return column_map, rows
