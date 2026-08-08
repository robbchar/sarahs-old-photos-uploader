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
    correcting a header would decide which field a value lands in. This rule is
    applied consistently so that column collisions (two headers normalizing to
    the same field name) can be detected before they silently destroy data."""
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
    """Construct a ColumnMap from a header row. The map records which columns
    are held back from upload and which normalized field name each header maps
    to. This is called before validation, so colliding headers or empty field
    names will not raise here; use check_column_map() to detect and report
    those defects."""
    return ColumnMap(
        headers=list(headers),
        field_names={header: normalize_header(header) for header in headers},
        held_back=[header for header in headers if is_held_back(header)],
    )


def grid_to_rows(grid: list[list[str]]) -> tuple[ColumnMap, list[dict[str, str]]]:
    """Convert a Sheet grid (headers + data rows) into a ColumnMap and row
    dictionaries. Short rows are padded with empty strings because the Sheets
    API omits trailing empty cells - a row genuinely can be shorter than the
    header without being corrupt data. Long rows silently drop excess cells
    without raising; use check_grid_shape() to detect those defects before
    upload. This mirrors the approach check_row_shape() takes for CSV rows: no
    validation in the converter, errors reported separately so users see every
    problem at once instead of one per run."""
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


def check_column_map(column_map: ColumnMap) -> list[str]:
    """Detect defects in a ColumnMap that would silently destroy data. Two
    headers normalizing to the same field name collide, silently overwriting
    one value with the other in the output dict. A header normalizing to the
    empty string is a similar defect: multiple decorative/divider columns would
    all collide on an empty key. This function reports each collision and each
    empty-string field name so all defects are visible to the user at once."""
    errors: list[str] = []

    # Check for collisions: two different headers mapping to the same field name
    seen: dict[str, str] = {}
    for header in column_map.headers:
        field_name = column_map.field_names[header]
        if field_name in seen:
            errors.append(
                f"columns '{seen[field_name]}' and '{header}' both normalize to field name "
                f"'{field_name}' - one value will silently overwrite the other"
            )
        else:
            seen[field_name] = header

    # Check for empty field names
    for header in column_map.headers:
        if column_map.field_names[header] == "":
            errors.append(
                f"column '{header}' normalizes to an empty field name (no alphanumeric "
                "characters remain after removing punctuation) - it will collide with "
                "other decorative columns and destroy data"
            )

    return errors


def check_grid_shape(grid: list[list[str]]) -> list[str]:
    """Detect data rows longer than the header row. When a data row has more
    cells than the header has columns, the excess cells silently vanish from
    the row dict output without error, making it impossible to tell which values
    are missing data and which are present but attributed to the wrong field.
    This mirrors check_row_shape() for CSV rows: short rows (handled by the
    Sheets API omitting trailing empty cells) are not an error and produce no
    message, but long rows are flagged here with row number and count of excess
    cells."""
    if not grid:
        return []

    errors: list[str] = []
    headers = grid[0]
    header_count = len(headers)

    for offset, row in enumerate(grid[1:]):
        row_number = offset + 2  # header is row 1, first data row is row 2
        if len(row) > header_count:
            extra_count = len(row) - header_count
            errors.append(
                f"row {row_number} has {extra_count} more field(s) than the header "
                f"({len(row)} vs {header_count}) - every value after column {header_count} "
                "will be silently dropped"
            )

    return errors
