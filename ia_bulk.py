"""Bulk validate/upload/sync-metadata CLI for Internet Archive, driven by a
project's Google Sheet (read live) or, for offline/dry-run work, a CSV
exported from it. See docs/ARCHITECTURE.md for the CSV schema and identifier
scheme this script assumes, and docs/DECISIONS.md ("The Sheet is read live")
for why both sources exist."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterator, TypeVar

import googleapiclient.discovery
import internetarchive
import requests
from googleapiclient.errors import HttpError

import google_auth
from column_map import (
    ColumnMap,
    FileResolutionError,
    TemplateError,
    candidate_path,
    check_column_map,
    check_file_template,
    check_grid_shape,
    grid_to_rows,
    resolve_file,
    template_fields,
)
from ia_fields import PIPELINE_OWNED_FIELDS, suggest_standard_fields
from identifiers import RowState, classify_row, next_identifiers
from project_config import ProjectConfig, load_project_config
from sheet_client import CellUpdate, SheetClient, column_letter

IDENTIFIER_RE = re.compile(r"^[a-z0-9]+-[a-z0-9]+-\d{5}$")
REQUIRED_UPLOAD_COLUMNS = ("identifier", "file", "mediatype", "title")
# Deliberately excludes "identifier" only - do not "fix" this back to
# REQUIRED_UPLOAD_COLUMNS, and do not add "identifier" back either.
#
# "identifier": on the Sheet path this is ordinary donor metadata (the
# real Sheet's own `Identifier` column holds an archival reference like
# "CD 1 01 53 58 1 Central SS"), not the tool's minted identifier - that
# lives in `ia_identifier`, which is never required here either: a blank
# ia_identifier is the normal starting state for every new row
# (RowState.UNASSIGNED), not an error. See docs/DECISIONS.md, "Tool-owned
# Sheet columns are all `ia_`-prefixed".
#
# "file" is left out entirely, not merely reclassified. By the time
# validate_rows sees a Sheet row, cmd_validate has already turned
# `file_template` plus the row's own columns into a candidate path and
# resolved it against `files_dir` (see resolve_file() in column_map.py),
# recording the outcome in a FileOutcomes: resolved (a real, disk-verified
# `file` value), blank (nobody named a file yet - a readiness fact), or
# broken (a name was given and didn't resolve - an error, via
# outcomes.errors). A required-columns check for `file` here would just
# be a second, cruder way of asking the same question FileOutcomes already
# answered precisely, producing a duplicate "missing required column
# 'file'" line behind the resolver's own, more actionable message. See
# docs/DECISIONS.md, "A file is found by resolution, not by constructing a
# path".
#
# "title" is also gone, but reclassified rather than dropped: it moved to
# the registry's required_for_upload (see ProjectConfig), because it is
# ordinary human-filled metadata - a blank title means "nobody has
# catalogued this row yet" (readiness), not "this row is broken"
# (validity). `required_columns` here is now reserved for what is
# structurally guaranteed to exist independent of any human filling
# anything in.
#
# NOT to be confused with validate_rows' check_file_exists / is_file()
# check below, a completely different mechanism that stays untouched: it
# is a disk-level safety net on the resolved `file` value, not a
# required-columns check, and removing it is a different (and wrong)
# change from the one this constant's shrink makes.
SHEET_REQUIRED_COLUMNS = ("mediatype",)
# Columns this script reads by exact lowercase name. A case variant of one of
# these (a "Date" column from the raw Sheet export, say) is silently treated as
# unrelated pass-through metadata, so check_header rejects it.
KNOWN_LOWERCASE_COLUMNS = frozenset(REQUIRED_UPLOAD_COLUMNS) | {"date"}
CHUNK_SIZE = 500
TEST_COLLECTION = "test_collection"
TEST_IDENTIFIER_PREFIX = "zztest-"
UNDATED_PLACEHOLDER = "[n.d.]"
# projects_registry.json ships sheet_id/test_sheet_id as REPLACE_WITH_* until
# someone edits in the real Google Sheet ID. Checked before ever asking
# Google about it, so an unreplaced placeholder fails with a message naming
# the fix (edit the registry) instead of an opaque 404/permission error.
PLACEHOLDER_SHEET_ID_PREFIX = "REPLACE_WITH"

# The four columns this tool writes. All `ia_`-prefixed so they cannot collide
# with a header a Sheet author already uses - the real LCPS Sheet's own
# `Identifier` column holds the donor's archival reference, and an unprefixed
# `identifier` column would have been overwritten by the first upload. See
# docs/DECISIONS.md, "Tool-owned Sheet columns are all `ia_`-prefixed".
IA_IDENTIFIER_COLUMN = "ia_identifier"
IA_UPLOADED_COLUMN = "ia_uploaded"
IA_URL_COLUMN = "ia_url"
IA_IDENTIFIER_BIB_COLUMN = "ia_identifier_bib"
WRITE_BACK_COLUMNS = (
    IA_IDENTIFIER_COLUMN,
    IA_UPLOADED_COLUMN,
    IA_URL_COLUMN,
    IA_IDENTIFIER_BIB_COLUMN,
)
ITEM_URL_PREFIX = "https://archive.org/details/"
# upload_row() strips these two keys from the metadata it sends, so a column
# normalizing to one of them never reaches Internet Archive whatever the
# receipt might otherwise imply. `file` is the local path, not metadata.
# `identifier` IS Internet Archive's own item identifier, so a Sheet column of
# that name - on the real Sheet, the donor's archival reference - cannot be
# uploaded under it. Named here so the receipt can say so out loud instead of
# listing a field that silently never ships.
DROPPED_BY_UPLOAD_ROW = frozenset({"identifier", "file"})

_ChunkItem = TypeVar("_ChunkItem")


def chunk_rows(
    rows: list[_ChunkItem], chunk_size: int = CHUNK_SIZE
) -> "Iterator[list[_ChunkItem]]":
    for start in range(0, len(rows), chunk_size):
        yield rows[start : start + chunk_size]


@dataclass
class CsvData:
    """Header and rows travel together because validating one without the
    other is what let a malformed header slip through: check_row_shape can
    only see a row/header field-count mismatch, and check_header can only see
    the header text."""

    fieldnames: list[str]
    rows: list[dict[str, str]]


def read_csv(csv_path: str | Path) -> CsvData:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return CsvData(fieldnames=list(reader.fieldnames or []), rows=rows)


def load_registry(registry_path: str | Path) -> dict:
    with open(registry_path, encoding="utf-8") as f:
        return json.load(f)


def check_identifier(
    identifier: str,
    row_number: int,
    registry: dict,
    seen_identifiers: dict[str, int],
    column_name: str = "identifier",
) -> list[str]:
    """column_name defaults to "identifier" (the CSV path, unchanged) and
    names the column being checked in every message. The Sheet path passes
    "ia_identifier" - on a Sheet that has BOTH its own `Identifier` column
    (donor metadata, untouched by this tool) and `ia_identifier` (the
    tool's minted one), a message that just says "identifier" leaves a
    volunteer unable to tell which column to go fix. Naming the actual
    column is exactly what Part A's `ia_` prefix exists to make possible."""
    identifier = identifier.strip()
    if not identifier:
        return [f"missing required column '{column_name}'"]

    errors: list[str] = []
    if not IDENTIFIER_RE.match(identifier):
        errors.append(
            f"{column_name} '{identifier}' does not match scheme COLLECTIONKEY-PROJECTID-NUMBER"
        )
    else:
        collection_key, project_id, _number = identifier.split("-")
        known_prefix = collection_key == registry.get("collection_key") and project_id in registry.get(
            "projects", {}
        )
        if not known_prefix:
            errors.append(
                f"{column_name} prefix '{collection_key}-{project_id}' not found in project registry"
            )

    if identifier in seen_identifiers:
        errors.append(f"{column_name} '{identifier}' duplicates row {seen_identifiers[identifier]}")
    else:
        seen_identifiers[identifier] = row_number

    return errors


def check_header(fieldnames: list[str] | None) -> list[str]:
    """Header text becomes the IA metadata field name verbatim, so a sloppy
    header ships sloppy field names across the whole batch - and metadata on
    an uploaded item is permanent enough to be worth failing loudly over.

    These are rejected rather than silently cleaned up: stripping or
    lowercasing a header on the user's behalf would quietly change which
    field a value lands in, which is the very failure this check exists to
    catch."""
    if not fieldnames:
        return ["CSV has no header row"]

    errors: list[str] = []

    for fieldname in fieldnames:
        if fieldname != fieldname.strip():
            errors.append(
                f"column '{fieldname}' has leading/trailing whitespace - it would upload "
                "as a metadata field name with that whitespace in it"
            )

    seen: set[str] = set()
    for fieldname in fieldnames:
        if fieldname in seen:
            errors.append(f"duplicate column '{fieldname}'")
        seen.add(fieldname)

    for fieldname in fieldnames:
        canonical = fieldname.strip().lower()
        if canonical in KNOWN_LOWERCASE_COLUMNS and fieldname != canonical:
            errors.append(
                f"column '{fieldname}' must be lowercase '{canonical}' - as spelled it is "
                "passed through as an unrelated metadata field"
            )

    return errors


def check_row_shape(row: dict) -> list[str]:
    """A CSV row must have exactly as many fields as the header. csv.DictReader
    tolerates both mismatches silently, and both corrupt an upload:

    - Surplus fields land in a list under the None restkey, which later blows
      up upload_row's metadata comprehension with "'list' object has no
      attribute 'strip'".
    - Missing fields become None, which means the header and the data disagree
      about column positions - so every value past the gap is uploaded under
      the wrong field name. A header cell containing an unquoted comma
      produces exactly this.

    Note that an empty cell is "" and is perfectly fine; only None means the
    field was absent from the row."""
    errors: list[str] = []

    surplus = row.get(None)
    if surplus:
        errors.append(
            f"row has more fields than the header ({len(surplus)} extra: {surplus!r}) - "
            "a header cell probably contains an unquoted comma"
        )

    missing = [key for key, value in row.items() if key is not None and value is None]
    if missing:
        errors.append(
            f"row has fewer fields than the header (missing: {', '.join(missing)}) - "
            "every value after the gap is attributed to the wrong column"
        )

    return errors


class Readiness(Enum):
    """Whether a human has filled in the fields a row needs before it can be
    uploaded. Deliberately NOT a member of RowState: RowState answers "has
    this been minted and uploaded", readiness answers "has a person filled
    it in", and a not-ready row IS RowState.UNASSIGNED. They are different
    questions, not alternatives - see docs/DECISIONS.md."""

    READY = "ready"
    NOT_READY = "not_ready"


@dataclass
class RowValidation:
    row_number: int
    identifier: str
    errors: list[str] = field(default_factory=list)
    # Two sources, in a fixed order: the blank required_for_upload columns
    # validate_rows finds first, then the blank file_template cells
    # validate_sheet_grid extends on afterward (see FileOutcomes.blank).
    # Order matters to callers that group or count by name (Tasks 7-8), not
    # just to this list's own contents.
    missing_fields: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def readiness(self) -> Readiness:
        """Derived, never stored: `missing_fields` being non-empty IS what
        not-ready means, so a second stored field could only drift from it."""
        return Readiness.NOT_READY if self.missing_fields else Readiness.READY


def validate_rows(
    rows: list[dict[str, str]],
    files_dir: str | Path,
    registry: dict,
    skip_identifiers: frozenset[str] = frozenset(),
    required_columns: tuple[str, ...] = REQUIRED_UPLOAD_COLUMNS,
    check_file_exists: bool = True,
    identifier_column: str = "identifier",
    required_for_upload: tuple[str, ...] = (),
) -> list[RowValidation]:
    """skip_identifiers lets a --resume-from run skip re-validating rows a
    prior run already validated and uploaded successfully - the identifier
    is still tracked for duplicate detection, just without redoing the
    regex/registry/disk-stat checks.

    required_columns defaults to REQUIRED_UPLOAD_COLUMNS (the CSV path,
    unchanged) but the Sheet path passes SHEET_REQUIRED_COLUMNS, which
    excludes 'identifier' - see that constant's comment for why.

    check_file_exists defaults to True (the CSV path, unchanged: a CSV row's
    'file' is a real, already-resolvable path, so checking it on disk is
    correct there). The Sheet path now also passes True (Phase 2 - see
    SHEET_REQUIRED_COLUMNS' comment): by the time this runs, cmd_validate
    has already resolved each row's 'file' against disk via resolve_file(),
    so this check is a redundant safety net there rather than the primary
    signal, which is fine - it costs one cheap is_file() stat per row.

    identifier_column defaults to "identifier" (the CSV path, unchanged:
    that column is pre-assigned, permanent, and never generated by this
    tool). The Sheet path passes "ia_identifier" instead - after Task 9 the
    Sheet's own 'identifier' column holds the donor's original archival
    reference (e.g. "CD 1 01 53 58 1 Central SS"), not a minted IA
    identifier, and running check_identifier's COLLECTIONKEY-PROJECTID-
    NUMBER regex against a donor reference fails every row for the wrong
    reason - which is exactly what happened against the real Sheet before
    this fix. See docs/DECISIONS.md, "Tool-owned Sheet columns are all
    `ia_`-prefixed".

    required_for_upload defaults to () (the CSV path, unchanged: a CSV's
    'title' etc. are already covered by required_columns, and a CSV has no
    ProjectConfig to source a second list from). The Sheet path passes
    config.required_for_upload - a blank one of these is a READINESS fact
    (nobody has catalogued this row yet), not a validation error, which is
    the whole reason it is recorded on missing_fields rather than folded
    into `errors` alongside required_columns."""
    seen_identifiers: dict[str, int] = {}
    results: list[RowValidation] = []

    for offset, row in enumerate(rows):
        row_number = offset + 2  # header is row 1
        identifier = (row.get(identifier_column) or "").strip()

        if identifier in skip_identifiers:
            seen_identifiers.setdefault(identifier, row_number)
            results.append(RowValidation(row_number=row_number, identifier=identifier))
            continue

        errors: list[str] = check_row_shape(row)

        for column in required_columns:
            if not (row.get(column) or "").strip():
                errors.append(f"missing required column '{column}'")

        if identifier:
            errors.extend(
                check_identifier(
                    identifier, row_number, registry, seen_identifiers, identifier_column
                )
            )

        if check_file_exists:
            file_value = (row.get("file") or "").strip()
            if file_value:
                file_path = Path(files_dir) / file_value
                if not file_path.is_file():
                    errors.append(f"file not found: {file_path}")

        missing_fields = [
            column for column in required_for_upload if not (row.get(column) or "").strip()
        ]

        results.append(
            RowValidation(
                row_number=row_number,
                identifier=identifier,
                errors=errors,
                missing_fields=missing_fields,
            )
        )

    return results


def validate_csv_rows(
    rows: list[dict[str, str]],
    files_dir: str | Path,
    registry: dict,
    skip_identifiers: frozenset[str] = frozenset(),
) -> list[RowValidation]:
    """The CSV path's answer, named. `identifier`, `file`, `mediatype` and
    `title` are all required: a CSV is a small file somebody prepared by hand
    for one batch, its identifiers are pre-assigned and permanent, and a blank
    one is a defect rather than a starting state."""
    return validate_rows(
        rows,
        files_dir,
        registry,
        skip_identifiers=skip_identifiers,
        required_columns=REQUIRED_UPLOAD_COLUMNS,
        check_file_exists=True,
        identifier_column="identifier",
    )


def validate_sheet_rows(
    rows: list[dict[str, str]],
    files_dir: str | Path,
    registry: dict,
    skip_identifiers: frozenset[str] = frozenset(),
    required_for_upload: tuple[str, ...] = (),
) -> list[RowValidation]:
    """The Sheet path's answer, named. It differs from the CSV path's in
    exactly three ways, all of which used to travel as loose parameters at
    every call site:

    - the tool's minted identifier lives in `ia_identifier`, never
      `identifier` (which on the real Sheet is the donor's own archival
      reference and would fail the COLLECTIONKEY-PROJECTID-NUMBER regex on
      every row);
    - `ia_identifier` is not required, because blank is the normal starting
      state of a new row - RowState.UNASSIGNED, not an error;
    - a blank required_for_upload column (title, say) is a readiness fact
      recorded on missing_fields, not a validation error - see
      SHEET_REQUIRED_COLUMNS' comment for why that split exists."""
    return validate_rows(
        rows,
        files_dir,
        registry,
        skip_identifiers=skip_identifiers,
        required_columns=SHEET_REQUIRED_COLUMNS,
        check_file_exists=True,
        identifier_column=IA_IDENTIFIER_COLUMN,
        required_for_upload=required_for_upload,
    )


_GRID_SHAPE_ROW_NUMBER_RE = re.compile(r"^row (\d+) ")


def sheet_structure_validation(column_map: ColumnMap, grid: list[list[str]]) -> list[RowValidation]:
    """check_column_map catches two headers that normalize to the same IA
    field name - which would silently overwrite one column's data across
    every row - and headers that normalize to an empty field name. Those are
    genuinely header-level problems, so they're filed under row 1, mirroring
    header_validation() for the CSV path.

    check_grid_shape catches a data row longer than the header, whose excess
    cells otherwise vanish without a trace - but that is a problem with a
    SPECIFIC data row, not with the header. check_grid_shape's own message
    already names the real row number (e.g. "row 3 has 1 more field(s)...");
    filing it under row 1 anyway - as an earlier version of this function
    did - puts a row-3 problem under the heading a volunteer reads as "the
    header row", which is actively confusing. So each shape-error message is
    parsed for the row number it already names and filed there instead,
    producing one RowValidation per affected row.

    check_grid_shape's message format is a private contract between these
    two functions, not a public interface - if that wording ever changes
    such that the leading "row N " prefix disappears, _GRID_SHAPE_ROW_NUMBER_RE
    simply fails to match and the message is filed under row 1 as a safe
    fallback rather than raising."""
    results: list[RowValidation] = []

    header_errors = check_column_map(column_map)
    if header_errors:
        results.append(RowValidation(row_number=1, identifier="", errors=header_errors))

    for message in check_grid_shape(grid):
        match = _GRID_SHAPE_ROW_NUMBER_RE.match(message)
        row_number = int(match.group(1)) if match else 1
        results.append(RowValidation(row_number=row_number, identifier="", errors=[message]))

    return results


def format_field_receipt(column_map: ColumnMap) -> str:
    """Printed before anything permanent happens, so the transformation from
    Sheet header to IA field name is reviewable by a human.

    The "not uploaded" sections are not decoration. A Sheet column named
    `Identifier` (the real one has one, holding the donor's archival reference)
    normalizes to `identifier`, which upload_row strips because that name is
    Internet Archive's own item identifier. The receipt used to list it among
    the fields that would upload, which was simply untrue - and a receipt an
    operator learns to disbelieve is worse than no receipt.

    There are two different reasons a column does not ship, and collapsing
    them into one list would recreate that same untruth in a quieter form:

    - `identifier` and `file` are dropped outright (DROPPED_BY_UPLOAD_ROW):
      one is Internet Archive's own item identifier, the other a local path.
    - `mediatype` and `collection` ARE sent, but with a value this tool
      generates - upload_from_sheet overwrites row['mediatype'] from the
      registry and upload_row sets metadata['collection'] unconditionally, so
      a Sheet column of either name has its own value silently discarded.
      ia_fields.PIPELINE_OWNED_FIELDS is the existing definition of that set,
      reused here rather than restated, so the receipt and the
      rename-suggestion logic cannot disagree about which names are the
      tool's."""
    all_fields = column_map.uploadable_fields()
    fields = [
        name
        for name in all_fields
        if name not in DROPPED_BY_UPLOAD_ROW and name not in PIPELINE_OWNED_FIELDS
    ]
    dropped = [name for name in all_fields if name in DROPPED_BY_UPLOAD_ROW]
    # `identifier` is in both sets; it is listed under "reserves these names"
    # only, which is the more precise reason of the two.
    generated = [
        name
        for name in all_fields
        if name in PIPELINE_OWNED_FIELDS and name not in DROPPED_BY_UPLOAD_ROW
    ]

    lines = ["will upload these metadata fields:"]
    lines.append("  " + ", ".join(fields) if fields else "  (none)")
    if dropped:
        lines.append("NOT uploaded - Internet Archive reserves these names:")
        lines.append(f"  {', '.join(dropped)}")
    if generated:
        lines.append(
            "uploaded with a value this tool generates - the column's own value is IGNORED:"
        )
        lines.append(f"  {', '.join(generated)}")
    if column_map.held_back:
        lines.append("held back (LCPS Internal):")
        lines.append("  " + ", ".join(column_map.held_back))
    return "\n".join(lines)


def _pluralize(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def format_lifecycle_summary(rows: list[dict[str, str]], row_results: list[RowValidation]) -> str:
    """row_results must be validate_rows()'s own output for these exact
    rows, in the same order (one result per row) - NOT the combined report
    that also includes sheet_structure_validation()'s row-1/shape entries,
    which are not aligned with `rows` at all. Passing a mismatched list
    would make zip() silently truncate to the shorter one rather than
    raising, so a caller-side wiring mistake would produce plausible-looking
    but wrong counts instead of an obvious failure - which is exactly the
    kind of silent wrongness this function exists to prevent, so the length
    is checked explicitly instead.

    Counts are cross-referenced against row_results rather than
    classify_row() alone, for EVERY bucket, not just "ready": a row that
    classify_row() would call DONE or RESERVED but that actually fails
    validation (a duplicate identifier, an unregistered project prefix, a
    now-missing title) is not "already uploaded" or "will retry" just
    because it has the shape of one - RESERVED in particular makes a
    forward-looking promise ("will retry under existing identifier") that a
    row failing identifier validation cannot keep.

    Validity itself splits three ways, not two: a row is either READY
    (catalogued and passes validation), invalid (catalogued but fails
    validation), or NOT_READY (missing one or more required_for_upload/
    file_template fields - see RowValidation.readiness). Crossed with
    classify_row()'s three states that makes nine buckets, not six. A row
    that is BOTH not-ready and carrying validation errors (e.g. an
    uncatalogued row whose filename is also a typo) is counted ONCE, under
    not-ready: not-ready takes precedence over invalid, because "nobody has
    filled this in yet" is the more useful thing to tell an operator than a
    validation error that will most likely resolve itself the moment the
    row is catalogued. Every row falls into exactly one of UNASSIGNED/DONE/
    RESERVED and then exactly one of ready/invalid/not_ready, so the nine
    counts below always sum to len(rows).

    Only non-zero buckets render, except the three per-state headline lines
    ("ready to upload"/"already uploaded"/"reserved but unconfirmed"), which
    always print - even as "0" - so the three lifecycle states themselves
    are never silently absent from the report. When both a state's
    not_ready and invalid counts are non-zero, not_ready prints first: it is
    normally the far larger of the two (an unfilled-in row rather than a
    broken one) and is not an error, so it reads before the more alarming
    invalid line rather than after it."""
    if len(rows) != len(row_results):
        raise ValueError(
            f"format_lifecycle_summary: got {len(rows)} row(s) but {len(row_results)} "
            "row_results - they must be the same length, in the same order. Pass "
            "validate_rows()'s own return value here, not the combined report (which "
            "also carries sheet_structure_validation()'s row-1/shape entries)."
        )

    counts: dict[tuple[RowState, str], int] = {
        (state, bucket): 0
        for state in (RowState.UNASSIGNED, RowState.DONE, RowState.RESERVED)
        for bucket in ("ready", "invalid", "not_ready")
    }

    for row, result in zip(rows, row_results):
        state = classify_row(row)
        if result.readiness is Readiness.NOT_READY:
            bucket = "not_ready"  # precedence: counted once, here - never also "invalid"
        elif result.is_valid:
            bucket = "ready"
        else:
            bucket = "invalid"
        counts[(state, bucket)] += 1

    lines = [
        f"{_pluralize(counts[(RowState.UNASSIGNED, 'ready')], 'row')} ready to upload "
        "(no identifier yet)"
    ]
    if counts[(RowState.UNASSIGNED, "not_ready")]:
        lines.append(
            f"{_pluralize(counts[(RowState.UNASSIGNED, 'not_ready')], 'row')} not yet "
            "assigned an identifier and not yet catalogued (missing required fields) - "
            "waiting on data entry, not blocked by an error"
        )
    if counts[(RowState.UNASSIGNED, "invalid")]:
        lines.append(
            f"{_pluralize(counts[(RowState.UNASSIGNED, 'invalid')], 'row')} not yet "
            "assigned an identifier but failed validation - see the errors above; will "
            "not be uploaded until fixed"
        )

    lines.append(f"{counts[(RowState.DONE, 'ready')]} already uploaded")
    if counts[(RowState.DONE, "not_ready")]:
        lines.append(
            f"{_pluralize(counts[(RowState.DONE, 'not_ready')], 'row')} already uploaded "
            "but missing required fields - a required column was cleared after upload; "
            "needs a human to look, not an automatic retry"
        )
    if counts[(RowState.DONE, "invalid")]:
        lines.append(
            f"{_pluralize(counts[(RowState.DONE, 'invalid')], 'row')} already uploaded but "
            "now fail validation - see the errors above; this needs a human to look, not "
            "an automatic retry"
        )

    lines.append(
        f"{counts[(RowState.RESERVED, 'ready')]} reserved but unconfirmed - will retry "
        "under existing identifier"
    )
    if counts[(RowState.RESERVED, "not_ready")]:
        lines.append(
            f"{_pluralize(counts[(RowState.RESERVED, 'not_ready')], 'row')} reserved but "
            "not yet catalogued (missing required fields) - waiting on data entry before "
            "it can retry"
        )
    if counts[(RowState.RESERVED, "invalid")]:
        lines.append(
            f"{_pluralize(counts[(RowState.RESERVED, 'invalid')], 'row')} reserved but "
            "invalid - see the errors above; will NOT retry automatically until fixed"
        )

    return "\n".join(lines)


def _format_result_lines(results: list[RowValidation]) -> list[str]:
    """The [STATUS]/error-line half of a report, without the trailing
    "N/M rows passed" count - factored out so a caller that needs to show
    structural errors WITHOUT a misleading pass/fail count next to them
    (see cmd_validate's no-data-rows branch: "0/1 rows passed" reads as
    nonsense when the "1" is a synthetic entry standing in for zero real
    rows) can reuse the exact same formatting `format_report` uses."""
    lines: list[str] = []
    for result in results:
        status = "PASS" if result.is_valid else "FAIL"
        # A blank identifier is the normal state of an unassigned Sheet row
        # (see RowState.UNASSIGNED), not a special case worth restating the
        # row number for - "[PASS] row 2 (row 2)" said nothing "[PASS] row 2"
        # didn't already say.
        label = f" {result.identifier}" if result.identifier else ""
        # A not-ready row (missing_fields non-empty) gets a marker appended
        # after the label, distinct from the [PASS]/[FAIL] status: readiness
        # and validity are different questions (see Readiness's docstring),
        # so a row can be "[FAIL] ... (not yet catalogued)" - broken AND
        # uncatalogued - without the marker implying the errors below it are
        # what "not yet catalogued" means.
        marker = "  (not yet catalogued)" if result.missing_fields else ""
        lines.append(f"[{status}] row {result.row_number}{label}{marker}")
        for error in result.errors:
            lines.append(f"    - {error}")
    return lines


def format_readiness_breakdown(row_results: list[RowValidation]) -> str:
    """Counts not-ready rows by which field is missing.

    This is the measurement that sizes a planned follow-up tool: a script
    that fills filenames in from disk. If most not-ready rows are missing
    only a filename, that script closes most of the gap; if most are
    missing a title, it barely helps. A single flat "N not yet catalogued"
    total cannot answer that question - this can.

    The field names come from whatever is actually in each result's
    missing_fields, not a hardcoded list - so this stays correct when a
    project's required_for_upload changes, without this function needing to
    change alongside it.

    The counts OVERLAP - a row missing two fields appears in both of their
    counts - so the closing parenthetical noting that is load-bearing, not
    decoration: adjacent numbers are read as a partition (as if they summed
    to the total above them) unless something says otherwise, and here they
    don't sum to it."""
    not_ready = [result for result in row_results if result.missing_fields]
    if not not_ready:
        return ""

    counts: dict[str, int] = {}
    for result in not_ready:
        for name in result.missing_fields:
            counts[name] = counts.get(name, 0) + 1

    lines = [f"{_pluralize(len(not_ready), 'row')} not yet catalogued"]
    for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"    {count} missing {name}")
    if any(len(result.missing_fields) > 1 for result in not_ready):
        lines.append(
            "    (a row missing more than one field appears in more than one count "
            f"above, so these do not sum to {len(not_ready)})"
        )
    return "\n".join(lines)


def format_report(results: list[RowValidation]) -> str:
    lines = _format_result_lines(results)
    passed = sum(1 for r in results if r.is_valid)
    lines.append("")
    lines.append(f"{passed}/{len(results)} rows passed")
    return "\n".join(lines)


def utc_timestamp() -> str:
    """Every timestamp this tool records, in ISO-8601 UTC with an explicit Z.

    UTC for the same reason run_stamp() uses it, applied to the values that
    outlive the run: local time repeats an hour during the DST fall-back
    transition, so a run spanning it stamps a later chunk with an earlier
    wall-clock time than an earlier one. `ia_uploaded` is the permanent record
    of when an archival item was published, and the log is what --resume-from
    and any later audit read - both were naive local time, with no offset to
    reconstruct the real instant from afterwards.

    The trailing Z is not decoration: without it the string is ambiguous, and
    the ambiguity is only discoverable by knowing which machine wrote it and
    what its clock was set to that day."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def open_log(log_dir: str | Path, command_name: str) -> Path:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    # UTC here too, so a directory listing sorts in the order the runs
    # actually happened - see utc_timestamp().
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return log_dir / f"{command_name}-{timestamp}.jsonl"


def log_run_header(
    log_path: str | Path,
    config: ProjectConfig,
    column_map: ColumnMap,
    live: bool,
    dry_run: bool,
    limit: int | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> None:
    """The first line written to a Sheet-path run's log. `head -1 <log>` then
    answers "what did this run send, under what field names, and what did it
    require before a row could go out" - the exact question that gets asked
    months later, once an identifier is already permanent and the Sheet has
    moved on and no longer shows what it looked like at upload time.

    `columns` and `held_back` come straight from the ColumnMap: `columns`
    maps EVERY header the Sheet had that run (not only the uploaded ones) to
    its normalized field name, and `held_back` names which of those were
    excluded as `(LCPS Internal)` - a receipt has to show what was left out,
    not only what went through. `required_for_upload` is the project's
    readiness rule at the time of the run: which normalized columns had to be
    non-blank for a row to be in scope at all. All three can change between
    runs (the Sheet gaining a column, a registry edit) even though none of
    them changes per row within one run, which is why this is written once
    per log rather than being documented once somewhere else.

    `limit` and `chunk_size` (Task 12) round out the same reconstructability
    goal: a run that stopped after --limit rows, or that used a non-default
    --chunk-size, cannot be explained later by the rest of this record alone.
    Both default to "the run's own default" (None / CHUNK_SIZE) so the three
    tests that call this directly without passing them still get a header
    that says so explicitly, rather than omitting the fields.

    Deliberately excludes anything that isn't safe to keep around in a log
    file indefinitely: no credentials, no tokens, no filesystem paths outside
    the project. `sheet_id` is the one Google identifier here, and it already
    appears in this command's ordinary console output."""
    entry = {
        "record": "run_header",
        "timestamp": utc_timestamp(),
        "project": config.project_id,
        "live": live,
        "dry_run": dry_run,
        "sheet_id": config.sheet_id_for(live),
        "collection": config.ia_collection,
        "files_dir": config.files_dir,
        "file_template": config.file_template,
        "columns": dict(column_map.field_names),
        "held_back": list(column_map.held_back),
        "required_for_upload": list(config.required_for_upload),
        "limit": limit,
        "chunk_size": chunk_size,
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def log_result(
    log_path: str | Path,
    identifier: str,
    file_value: str,
    status: str,
    live: bool,
    error: str | None = None,
    uploaded_as: str | None = None,
) -> None:
    entry = {
        "identifier": identifier,
        "file": file_value,
        "status": status,
        "error": error,
        "uploaded_as": uploaded_as,
        "live": live,
        "timestamp": utc_timestamp(),
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def load_prior_successes(log_path: str | Path, live: bool) -> set[str]:
    """Identifiers logged as success/unchanged in the SAME mode (test vs
    --live) as this run. A test-mode log entry only ever confirms that the
    zztest-prefixed item landed in test_collection, never the real one, so
    it must not be allowed to skip a real --live upload (and vice versa).
    Logs from before the "live" field existed have no mode recorded and are
    treated conservatively as not matching either mode. A run_header record
    (see log_run_header) is skipped explicitly rather than relying on it
    merely lacking a "status" key - the header schema is free to grow a
    "status"-named field of its own later without silently turning this into
    a bug.

    A line that will not parse is SKIPPED, not raised on. log_result appends
    one line per row with no atomic write, so a run killed mid-write - Ctrl-C,
    a full disk, a closed laptop - leaves a truncated final line. Raising
    there made the log permanently unusable as a resume source, which disables
    the only recovery mechanism the CSV path has using the exact crash it
    exists to recover from. Every intact line before the damaged one is still
    a real record and is still honored.

    Damaged lines are counted and reported on stderr rather than skipped in
    silence: a lost line means an identifier that DID upload is no longer
    known to have, so the resumed run will attempt it again. That is safe -
    Internet Archive matches on MD5 and will not duplicate the file - but the
    operator should know why the run is longer than they expected."""
    successes: set[str] = set()
    damaged = 0
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("record") == "run_header":
                    continue
                if entry.get("status") in ("success", "unchanged") and entry.get("live") == live:
                    # A record with no identifier cannot skip anything, and is
                    # damage of the same kind as a line that will not parse.
                    identifier = entry.get("identifier")
                    if not identifier:
                        damaged += 1
                        continue
                    successes.add(identifier)
            except (json.JSONDecodeError, AttributeError):
                # AttributeError covers a line that parses as valid JSON of the
                # wrong shape entirely - a bare list or string has no .get().
                damaged += 1

    if damaged:
        print(
            f"{_pluralize(damaged, 'line')} in {log_path} could not be read and were skipped - "
            "the log is damaged, most likely truncated by a run that was killed mid-write. Any "
            "row recorded on those lines will be attempted again; Internet Archive matches on "
            "MD5, so an already-uploaded file is not duplicated.",
            file=sys.stderr,
        )
    return successes


def run_stamp() -> str:
    """A lowercase, IA-identifier-safe stamp unique to this invocation of the
    script - e.g. "20260819t144907". Computed ONCE per run and threaded
    through every effective_identifier() call that run makes, never
    recomputed per row: a run's test items must group together under one
    stamp, not scatter across however many rows it processed. (A *resumed*
    run is a separate invocation with its own stamp, so its items land under
    a second stamp rather than the original run's - that's correct, not a
    bug: --resume-from still recognizes them as done because it matches on
    the real `identifier`, never on the stamped `uploaded_as`.)

    Uses UTC (time.gmtime), not local time: local time repeats an hour's
    worth of timestamps during the DST fall-back transition, which would
    make two rehearsals started an hour apart during that transition mint
    the same stamp - defeating the reason this function exists.

    See docs/DECISIONS.md, "Test identifiers carry a per-run stamp" - without
    this, a test run's identifiers were a pure function of the real ones, so a
    fresh Sheet (which always mints from 00001) reproduced the exact same test
    identifiers every time. Internet Archive never releases an identifier and
    test_collection darkens items after ~30 days, so every rehearsal after the
    first collided with a darkened item and failed outright."""
    return time.strftime("%Y%m%dt%H%M%S", time.gmtime())


def effective_identifier(identifier: str, live: bool, stamp: str) -> str:
    """`stamp` is required, not defaulted - a default would leave the
    collision described in run_stamp()'s docstring reachable again, and every
    caller of this function lives in this same file.

    Live is untouched: a live identifier is the permanent, public address of
    an archival item and must stay a pure function of the Sheet/CSV, so the
    live branch deliberately never looks at `stamp`. A stamp reaching a live
    identifier would be the worst outcome this function could produce."""
    if live:
        return identifier
    return f"{TEST_IDENTIFIER_PREFIX}{stamp}-{identifier}"


# is_rate_limit_error() looks ONLY at parsed status-code INTEGERS, never at
# str(exc) or any server-supplied text. An earlier version of this function
# scanned str(exc) for "status 429"/"status 503" substrings; that is unsafe
# in both directions and was fixed after review found the gap:
#
# - a 404 (or anything else) whose body happens to mention "status 503" -
#   a mirrored error, a proxied message, an echoed request - would
#   misclassify as a rate limit and wrongly stop the whole run.
# - a plain substring test also matches "status 5031" or "status 42900":
#   digits that merely CONTAIN 503/429 as a substring, not equal to them.
#
# A false positive here is worse than a miss: it halts a batch mid-flight,
# on a command that creates permanent items, for a reason that is not real.
#
# Two structured sources are checked instead, both verified by reading
# source rather than guessed:
#
# 1. UploadFailed.status_code - set by upload_row() below, in this file,
#    from the real, parsed `response.status_code` whenever
#    internetarchive.upload() returns a not-ok Response. See UploadFailed's
#    own docstring.
#
# 2. exc.response.status_code - requests.exceptions.RequestException (the
#    base of HTTPError) stores whatever Response object it is given as
#    `.response` in its own __init__ (`self.response = kwargs.pop
#    ("response", None)` - verified by reading requests' source directly,
#    not assumed). Tracing the installed internetarchive 5.10.1's
#    Item.upload_file() - the method upload_row() actually reaches via
#    internetarchive.upload() -> Item.upload() - shows that on a real S3
#    failure it catches the resulting HTTPError and re-raises via
#    `raise type(exc)(error_msg, response=exc.response, request=exc.request)`.
#    The MESSAGE there is rebuilt from the S3 XML body's <Message>/
#    <Resource> text (see get_s3_xml_text() in internetarchive/utils.py) and
#    loses the numeric status and the S3 <Code> (e.g. "SlowDown") entirely -
#    but `response=exc.response` is passed through UNCHANGED, so
#    `.response.status_code` still holds the real, original status even
#    though the text does not. This is what lets the check below catch a
#    live rate limit surfacing through the real library's own exception,
#    not only upload_row()'s own not-ok-Response branch.
#
# 503 is Internet Archive's documented S3 overload signal (the installed
# internetarchive 5.10.1's own `ia upload --retries` help text: "Number of
# times to retry request if S3 returns a 503 SlowDown error"). 429 is not
# IA-upload-specific documentation, but session.py's default urllib3 Retry
# status_forcelist ([429, 500, 501, 502, 503, 504]) shows the library's own
# authors also treat it as rate-limit-adjacent.
#
# No --live run has ever happened, so no real rate-limit response has ever
# been captured - both sources above are verified against the installed
# library's SOURCE, not against actual IA behavior. Neither reachable
# exception in this codebase's own upload path lacks a structured status
# (see UploadFailed and the HTTPError tracing above), so there is no
# text-based fallback: the rate-limit decision never depends on
# server-supplied text, only on a parsed integer. A miss (an exception with
# neither attribute, or a genuinely different status) just logs one more
# ordinary failure and the run continues - --limit remains the
# operator-controlled backstop either way. See docs/DECISIONS.md,
# "Rate-limit detection uses a parsed status code, never message text".
RATE_LIMIT_STATUS_CODES = (429, 503)


def is_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    return status_code in RATE_LIMIT_STATUS_CODES


class RateLimited(Exception):
    """Marks an upload failure as Internet Archive's rate limit rather than
    an ordinary per-row error. Nothing ever raises or catches this - despite
    the name, it is never thrown. SheetUploadRun.execute() CONSTRUCTS one
    (reassigning it to the local `exc`) once is_rate_limit_error() has
    already matched the original exception, then checks `isinstance(exc,
    RateLimited)` to decide whether to stop the whole run or log this row
    and continue - a plain marker object checked by type, not a control-flow
    signal raised up the stack."""


class UploadFailed(RuntimeError):
    """Raised by upload_row() below when internetarchive.upload() returns a
    not-ok Response. Subclasses RuntimeError (rather than plain Exception)
    so the pre-existing `pytest.raises(RuntimeError, match="503")` caller
    keeps working unchanged.

    Carries `status_code` as the PARSED INTEGER `response.status_code` -
    never reconstructed from the message text later - specifically so
    is_rate_limit_error() can look at the real, structured value Internet
    Archive returned instead of scanning `response.text`, which is
    arbitrary server-supplied prose that might itself contain a string like
    "status 503" for an unrelated reason (a mirrored error, a proxy
    message) even when the real status was something else entirely. See
    is_rate_limit_error()'s own comment and docs/DECISIONS.md, "Rate-limit
    detection uses a parsed status code, never message text"."""

    def __init__(self, message: str, *, status_code: int | None):
        super().__init__(message)
        self.status_code = status_code


def upload_row(row: dict, target_identifier: str, collection: str, files_dir: str | Path) -> None:
    file_name = (row.get("file") or "").strip()
    if not file_name:
        # Defence in depth against the single most damaging outcome in this
        # system. `Path(files_dir) / ""` is files_dir ITSELF, and
        # internetarchive's Item.upload() iterates a directory argument, so a
        # blank name would send the whole data tree recursively into one
        # permanent, unrenameable item. plan_upload_targets already refuses to
        # plan such a row (it is NOT_READY); this raise makes the path
        # unreachable by construction rather than by an upstream caller's
        # discipline. Raising is safe for a run in flight: SheetUploadRun
        # catches it per row, logs a failure and moves on.
        raise ValueError(
            f"upload of '{target_identifier}' refused: the row has no 'file' value, and "
            "uploading a blank filename would send the entire files_dir recursively into "
            "one permanent item"
        )
    file_path = Path(files_dir) / file_name
    metadata = {
        key: (value or "").strip()
        for key, value in row.items()
        if key not in ("identifier", "file") and (value or "").strip()
    }
    metadata["date"] = (row.get("date") or "").strip() or UNDATED_PLACEHOLDER
    metadata["collection"] = collection

    responses = internetarchive.upload(
        target_identifier,
        files=[str(file_path)],
        metadata=metadata,
        verbose=True,
        checksum=True,
    )
    for response in responses:
        # internetarchive.upload() is typed to return Request | Response;
        # a Request is only ever returned when debug=True, which we never
        # pass, so this always holds at runtime. Narrowing it explicitly
        # keeps response.ok/.status_code/.text type-checker-clean.
        if isinstance(response, requests.Request):
            raise RuntimeError(
                f"upload of '{target_identifier}' returned an unprepared Request instead of "
                "a Response - this should be unreachable since debug is never passed"
            )
        if not response.ok:
            raise UploadFailed(
                f"upload of '{target_identifier}' failed with status {response.status_code}: {response.text}",
                status_code=response.status_code,
            )


class MetadataUnchanged(Exception):
    pass


def update_metadata_row(row: dict, target_identifier: str) -> None:
    """Blank cells are dropped entirely, not sent as empty strings - a
    sync-metadata CSV only needs to list the columns that changed, so a
    blank cell must mean "leave this field alone", not "clear it". To
    actually delete an existing field on the IA item, put the literal
    value REMOVE_TAG in that cell; the internetarchive library (and the
    official `ia` CLI's `--modify field:REMOVE_TAG`) treats that string as
    a delete sentinel and issues a metadata "remove" op for the field."""
    metadata = {
        key: (value or "").strip()
        for key, value in row.items()
        if key != "identifier" and (value or "").strip()
    }

    response = internetarchive.modify_metadata(target_identifier, metadata=metadata)
    # See the matching narrowing comment in upload_row(): modify_metadata()
    # is typed to return Request | Response, but a Request is only ever
    # returned when debug=True, which we never pass.
    if isinstance(response, requests.Request):
        raise RuntimeError(
            f"metadata update of '{target_identifier}' returned an unprepared Request instead of "
            "a Response - this should be unreachable since debug is never passed"
        )
    if not response.ok:
        try:
            error_message = json.loads(response.text).get("error", "")
        except (ValueError, AttributeError):
            error_message = ""
        if error_message == "no changes to _meta.xml":
            raise MetadataUnchanged(target_identifier)
        raise RuntimeError(
            f"metadata update of '{target_identifier}' failed with status {response.status_code}: {response.text}"
        )


def validate_identifiers(
    rows: list[dict[str, str]],
    registry: dict,
    skip_identifiers: frozenset[str] = frozenset(),
) -> list[RowValidation]:
    seen_identifiers: dict[str, int] = {}
    results: list[RowValidation] = []

    for offset, row in enumerate(rows):
        row_number = offset + 2
        identifier = (row.get("identifier") or "").strip()

        if identifier in skip_identifiers:
            seen_identifiers.setdefault(identifier, row_number)
            results.append(RowValidation(row_number=row_number, identifier=identifier))
            continue

        errors = check_identifier(identifier, row_number, registry, seen_identifiers)
        results.append(RowValidation(row_number=row_number, identifier=identifier, errors=errors))

    return results


def header_validation(fieldnames: list[str]) -> list[RowValidation]:
    """Header problems are reported as row 1 - which is literally what the
    header row is - so they flow through the same report and exit-code path
    as row problems instead of needing a parallel channel."""
    errors = check_header(fieldnames)
    return [RowValidation(row_number=1, identifier="", errors=errors)] if errors else []


def build_sheet_client(config: ProjectConfig, live: bool) -> SheetClient:
    """The only place `google_auth.load_credentials` and
    `googleapiclient.discovery.build` are called. Every Sheet-touching code
    path (cmd_validate now, upload/sync-metadata in later tasks) goes through
    this one function, so tests can monkeypatch this single seam and run with
    no credentials and no network.

    interactive=True whenever a human is at a terminal to see the OAuth
    consent-flow browser tab; sys.stdin.isatty() is False for anything
    unattended (cron, CI), where load_credentials fails fast with
    AuthUnavailable instead of hanging waiting for a browser nobody sees."""
    credentials = google_auth.load_credentials(
        google_auth.DEFAULT_TOKEN_PATH,
        google_auth.DEFAULT_CLIENT_SECRETS_PATH,
        interactive=sys.stdin.isatty(),
    )
    service = googleapiclient.discovery.build("sheets", "v4", credentials=credentials)
    return SheetClient(service, config.sheet_id_for(live), config.sheet_tab)


@dataclass(frozen=True)
class FileOutcomes:
    """Two distinct failures that used to be one. `errors` are rows that
    asserted a file and were wrong; `blank` are rows that asserted nothing.
    Kept apart HERE because resolve_sheet_files blanks row['file'] on
    failure, so nothing downstream can tell them apart afterwards."""

    errors: dict[int, str]
    blank: dict[int, list[str]]


def resolve_sheet_files(rows: list[dict[str, str]], config: ProjectConfig) -> FileOutcomes:
    """Resolves each row's file against disk BEFORE validation runs, so a row
    either carries a real, disk-verified 'file' value (and the resolved name,
    which may differ from what the Sheet cell says - see resolve_file() - also
    becomes 'ia_identifier_bib') or is recorded here as one of two DIFFERENT
    outcomes: a row that named a file and was wrong (`errors`, carrying the
    resolver's own message), or a row that named nothing at all (`blank`,
    carrying which of file_template's cells are empty).

    Splitting the two here is the whole point. Below, on failure, the Sheet
    cell's raw, UNVERIFIED candidate must not survive as row['file'] - left in
    place it would coincidentally resolve as a literal path for the later disk
    check (or for an upload), silently masking the fact that resolution never
    actually confirmed this file exists. The cost of that blanking is that
    afterwards a row nobody filled in and a row with a typo'd filename are
    indistinguishable, both being row['file'] == "". This function is the last
    place the candidate still exists, so it is the only place the distinction
    can be drawn - and drawing it matters in one direction especially: a
    broken row misfiled as blank is downgraded from "fix me" to "nobody has
    got to it yet", which is how it stops ever being fixed.

    Shared by `validate` and `upload` deliberately: the value `upload` records
    in `ia_identifier_bib` has to be the same resolved name `validate` showed
    the operator, and two copies of this loop would eventually disagree."""
    listing_cache: dict[Path, list[str]] = {}
    errors: dict[int, str] = {}
    blank: dict[int, list[str]] = {}
    fields = template_fields(config.file_template)

    for offset, row in enumerate(rows):
        row_number = offset + 2  # header is row 1

        blank_cells = [name for name in fields if not (row.get(name) or "").strip()]
        if blank_cells:
            # At least one cell the template needs is empty, so no candidate
            # path can be built and there is nothing to resolve - which is
            # what makes the old "matching ''" message unreachable rather
            # than merely rare. Never an error even when the OTHER cells are
            # filled: a blank cell is a not-yet-answered question, not a
            # wrong answer. len(blank_cells) < len(fields) is what tells a
            # partially-filled row from an untouched one, so the report can
            # name the specific missing cell instead of lumping the two
            # together.
            blank[row_number] = blank_cells
            row["file"] = ""
            continue

        candidate = candidate_path(config.file_template, row)
        try:
            resolved = resolve_file(config.files_dir, candidate, listing_cache)
        except FileResolutionError as exc:
            errors[row_number] = str(exc)
            row["file"] = ""
            continue
        row["file"] = resolved
        row[IA_IDENTIFIER_BIB_COLUMN] = resolved

    return FileOutcomes(errors=errors, blank=blank)


def validate_sheet_grid(
    rows: list[dict[str, str]],
    registry: dict,
    config: ProjectConfig,
    structure_results: list[RowValidation],
    outcomes: FileOutcomes,
) -> tuple[list[RowValidation], list[RowValidation]]:
    """Returns (header_results, row_results). row_results holds exactly one
    entry per row in `rows`, in the same order - which is what
    format_lifecycle_summary requires and what lets a caller pair a row with
    its verdict by index.

    A structural problem with one specific data row (a long row) belongs IN
    that row's own result, not in a second entry printed beside it. Filing it
    separately made the row appear twice with opposite verdicts ("[FAIL] row
    9" from check_grid_shape, "[PASS] row 9" from validate_rows), inflated
    format_report's denominator past the number of data rows the Sheet
    actually has, and - worst - left the row counted as "ready to upload" by
    the lifecycle summary, which reads row_results alone. check_grid_shape and
    validate_rows both number rows `offset + 2`, so row_number - 2 indexes
    row_results exactly. Anything outside that range is header-level and keeps
    its own row-1 entry (as does _GRID_SHAPE_ROW_NUMBER_RE's row-1 fallback) -
    the bounds check is load-bearing, not defensive: a bare row_number - 2
    would quietly fold row 1 into the LAST data row via negative indexing."""
    row_results = validate_sheet_rows(
        rows, config.files_dir, registry, required_for_upload=config.required_for_upload
    )

    for row_number, message in outcomes.errors.items():
        # The resolver's own message: it names the folder and the name that
        # was looked for, which is the actionable part. There is no longer a
        # generic "missing required column 'file'" line behind it - `file`
        # left required_columns entirely once FileOutcomes became the sole
        # source of truth for whether a row's file resolved (see
        # SHEET_REQUIRED_COLUMNS' comment), so this is the whole story for a
        # broken-filename row now, not merely the first line of it.
        row_result = row_results[row_number - 2]
        row_result.errors = [message] + row_result.errors

    for row_number, blank_cells in outcomes.blank.items():
        # Blank template cells are a readiness fact, not an error: nobody
        # asserted a file, so there is nothing to be wrong. Extended onto
        # missing_fields alongside any blank required_for_upload columns
        # validate_sheet_rows already put there, required_for_upload names
        # first - see missing_fields' own ordering note on RowValidation.
        #
        # De-duplicated (dict.fromkeys preserves that order) because the two
        # sources can legitimately name the same column: an operator may list
        # a file_template column in required_for_upload - e.g.
        # ["title", "file_name"] - and check_required_for_upload accepts it,
        # because it IS a real column in the Sheet. Left duplicated, one
        # not-ready row reported "2 missing file_name" and dragged in the
        # overlap parenthetical that says the counts do not sum.
        row_result = row_results[row_number - 2]
        row_result.missing_fields = list(
            dict.fromkeys(row_result.missing_fields + blank_cells)
        )

    header_results: list[RowValidation] = []
    for entry in structure_results:
        index = entry.row_number - 2
        if 0 <= index < len(row_results):
            row_result = row_results[index]
            # structural errors first: a long row's mis-attributed values are
            # the likely cause of whatever content errors follow.
            row_result.errors = entry.errors + row_result.errors
        else:
            header_results.append(entry)

    return header_results, row_results


def check_required_for_upload(config: ProjectConfig, column_map: ColumnMap) -> list[str]:
    """A required_for_upload name that matches no column makes EVERY row
    not-ready, so nothing ever uploads and the report reads "3,000 rows not
    yet catalogued" - which looks plausible. Silent, permanent, and
    self-consistent, which is why this is a hard error rather than a
    warning."""
    known = sorted(set(column_map.field_names.values()))
    return [
        f"required_for_upload names {name!r}, which is not a column in this Sheet. "
        f"Known columns: {', '.join(known)}"
        for name in config.required_for_upload
        if name not in known
    ]


def cmd_validate(args) -> int:
    # `is not None`, not truthiness: --csv "" must be an explicit (if
    # useless) request to read a CSV named "", and fail as such, rather than
    # silently falling through to the Sheet path because an empty string is
    # falsy.
    csv_path = getattr(args, "csv", None)
    if csv_path is not None:
        data = read_csv(csv_path)
        registry = load_registry(args.registry)
        results = header_validation(data.fieldnames) + validate_rows(
            data.rows, args.files_dir, registry
        )
        print(format_report(results))
        return 0 if all(r.is_valid for r in results) else 1

    registry = load_registry(args.registry)
    config = load_project_config(registry, args.project)
    sheet_id = config.sheet_id_for(args.live)
    mode = "live" if args.live else "test"

    # The run mode is this project's core safety design (a rehearsal must
    # never touch the real Sheet), so it - and exactly which spreadsheet and
    # tab back it - is printed before anything else, unconditionally, not
    # just on success. A human staring at a report has to be able to
    # confirm at a glance they're pointed where they think they are.
    print(f"project '{config.project_id}': {mode} mode, spreadsheet '{sheet_id}', tab '{config.sheet_tab}'")
    print()

    if sheet_id.startswith(PLACEHOLDER_SHEET_ID_PREFIX):
        print(
            f"the {mode}-mode spreadsheet ID for project '{config.project_id}' is still "
            f"the placeholder '{sheet_id}' - edit it in {args.registry} to the real Google "
            "Sheet ID before running validate.",
            file=sys.stderr,
        )
        return 1

    client = build_sheet_client(config, args.live)
    try:
        grid = client.read_grid()
    except HttpError as exc:
        print(
            f"could not read spreadsheet '{sheet_id}' tab '{config.sheet_tab}': {exc}. Check "
            f"that 'sheet_tab' in {args.registry} names the tab exactly (case-sensitive) as it "
            "appears in the Sheet, that the spreadsheet ID is correct, and that the Sheet has "
            "been shared with the Google account you authorized as.",
            file=sys.stderr,
        )
        return 1

    column_map, rows = grid_to_rows(grid)

    # mediatype is a per-project constant, never a Sheet column - inject it
    # before validating so every row satisfies the required-column check
    # instead of failing on a column that was never meant to exist.
    for row in rows:
        row["mediatype"] = config.mediatype

    structure_results = sheet_structure_validation(column_map, grid)

    if not rows:
        # A dedicated branch, not just another row-1 structural error: an
        # empty read is far more likely to mean a wrong tab name, an
        # unpopulated copy of the Sheet, or a Sheet never actually shared
        # with the account you authorized as than a real project with zero rows, and
        # reporting that as success would defeat the entire purpose of
        # running `validate`. Handled separately from the normal report
        # (rather than folded into sheet_structure_validation's row-1
        # entry) specifically so the summary line never has to say
        # "0/1 rows passed" - that "1" would be a synthetic entry standing
        # in for zero real rows, which reads as nonsense arithmetic to
        # whoever is staring at it.
        if structure_results:
            print("\n".join(_format_result_lines(structure_results)))
            print()
        print(
            "the Sheet has no data rows (only a header, or nothing at all) - check that "
            "'sheet_tab' in the project's registry entry names the right tab, and that "
            "the Sheet has actually been populated and shared"
        )
        return 1

    # A file_template naming a column the Sheet's header row doesn't have
    # (a registry typo, or a Sheet whose columns changed) is checked once,
    # here, rather than surfacing as the same resolution failure repeated
    # on every one of the Sheet's rows. Deliberately after the no-rows
    # branch above: an empty or header-only Sheet already gets a more
    # useful diagnostic ("no data rows") than a template complaint would be.
    try:
        check_file_template(config.file_template, column_map)
    except TemplateError as exc:
        print(
            f"project '{config.project_id}': {exc} - fix 'file_template' in {args.registry}",
            file=sys.stderr,
        )
        return 1

    config_errors = check_required_for_upload(config, column_map)
    if config_errors:
        print("\n".join(config_errors), file=sys.stderr)
        print(
            f"fix required_for_upload in {args.registry} - as written, every row would "
            "be reported as not yet catalogued and nothing would ever upload",
            file=sys.stderr,
        )
        return 1

    # See docs/DECISIONS.md, "A file is found by resolution, not by
    # constructing a path". `upload` runs the identical two steps, so the
    # value it records in ia_identifier_bib is the one shown here.
    file_outcomes = resolve_sheet_files(rows, config)
    header_results, row_results = validate_sheet_grid(
        rows, registry, config, structure_results, file_outcomes
    )

    results = header_results + row_results
    print(format_report(results))
    print()
    print(format_field_receipt(column_map))
    print()
    print(format_lifecycle_summary(rows, row_results))
    breakdown = format_readiness_breakdown(row_results)
    if breakdown:
        print()
        print(breakdown)
    print()
    print("suggestions (advisory - nothing is changed automatically):")
    suggestions = suggest_standard_fields(column_map.uploadable_fields())
    if suggestions:
        for suggestion in suggestions:
            print(f"  '{suggestion.field_name}' -> '{suggestion.standard}': {suggestion.reason}")
    else:
        print("  (none)")

    return 0 if all(r.is_valid for r in results) else 1


def run_rows(
    rows: list[dict],
    log_path: str | Path,
    live: bool,
    stamp: str,
    action: str,
    process_row,
    describe,
    file_value_for,
) -> dict[str, int]:
    """Shared chunk/progress/log-and-count loop for cmd_upload and
    cmd_sync_metadata - they differ only in how a row is processed, how its
    progress line reads, and what (if anything) goes in the log's file
    field. process_row(row, target_identifier) may raise MetadataUnchanged
    to count as "unchanged" rather than "failure".

    `stamp` is computed once by the caller (run_stamp(), called once per
    command invocation) and passed in rather than computed here, so every row
    this run touches - across every chunk - shares one stamp. See
    run_stamp()'s docstring for why that matters."""
    total = len(rows)
    counts = {"success": 0, "unchanged": 0, "failure": 0}
    position = 0
    for chunk in chunk_rows(rows):
        for row in chunk:
            position += 1
            identifier = row["identifier"].strip()
            target_identifier = effective_identifier(identifier, live, stamp)
            file_value = file_value_for(row)
            print(f"[{position}/{total}] {action} {describe(row, target_identifier)}")
            try:
                process_row(row, target_identifier)
                counts["success"] += 1
                log_result(log_path, identifier, file_value, "success", live, uploaded_as=target_identifier)
            except MetadataUnchanged:
                counts["unchanged"] += 1
                log_result(log_path, identifier, file_value, "unchanged", live, uploaded_as=target_identifier)
            except Exception as exc:
                counts["failure"] += 1
                log_result(
                    log_path, identifier, file_value, "failure", live, error=str(exc), uploaded_as=target_identifier
                )
    return counts


class MissingWriteBackColumns(Exception):
    """The Sheet has no column for something `upload` must record. Checked
    once, before anything is uploaded, because a run that uploaded first and
    then discovered it had nowhere to record the identifier would leave items
    on Internet Archive the Sheet has no record of - the exact outcome the
    reserve-first ordering exists to prevent."""


@dataclass(frozen=True)
class SheetColumns:
    """Zero-based grid indexes of the four columns this tool writes."""

    ia_identifier: int
    ia_uploaded: int
    ia_url: int
    ia_identifier_bib: int

    def cell(self, column_index: int, row_number: int) -> str:
        return f"{column_letter(column_index)}{row_number}"


def locate_write_back_columns(column_map: ColumnMap) -> SheetColumns:
    """Every write-back column is required in ALL modes, including the default
    read-only one. A rehearsal that succeeds against a Sheet the real run would
    refuse is not a rehearsal, so the check does not vary with --live or
    --write-identifier."""
    indexes: dict[str, int] = {}
    for index, header in enumerate(column_map.headers):
        field_name = column_map.field_names[header]
        if field_name in WRITE_BACK_COLUMNS and field_name not in indexes:
            indexes[field_name] = index

    missing = [name for name in WRITE_BACK_COLUMNS if name not in indexes]
    if missing:
        raise MissingWriteBackColumns(
            f"the Sheet has no column(s) named {', '.join(missing)}. `upload` records what it "
            f"did in {', '.join(WRITE_BACK_COLUMNS)}; add them as header cells (any position, "
            "spelling exactly as shown) before uploading."
        )

    return SheetColumns(
        ia_identifier=indexes[IA_IDENTIFIER_COLUMN],
        ia_uploaded=indexes[IA_UPLOADED_COLUMN],
        ia_url=indexes[IA_URL_COLUMN],
        ia_identifier_bib=indexes[IA_IDENTIFIER_BIB_COLUMN],
    )


@dataclass(frozen=True)
class UploadTarget:
    """One row this run intends to upload.

    `identifier` is always the real, permanent one; `uploaded_as` is what
    actually goes over the wire, which is the same string only under --live.
    Both are kept because they answer different questions - the Sheet records
    the permanent identifier, while the URL has to point at the item that
    really exists."""

    row: dict[str, str]
    row_number: int
    identifier: str
    uploaded_as: str
    identifier_bib: str
    newly_minted: bool
    # What this row's file_template columns said when the run read the Sheet.
    # Re-checked against a fresh read before every write - see
    # split_moved_targets().
    source_fingerprint: str


def item_url(uploaded_as: str) -> str:
    return f"{ITEM_URL_PREFIX}{uploaded_as}"


def upload_timestamp() -> str:
    """Its own function so a test can pin it and assert a confirm batch as an
    exact ordered sequence rather than "a cell holding some string".

    Delegates to utc_timestamp() rather than formatting its own: this value
    lands in the Sheet's `ia_uploaded` column, which is the permanent record
    of when the item was published."""
    return utc_timestamp()


def reserve_updates(targets: list[UploadTarget], columns: SheetColumns) -> list[CellUpdate]:
    """Step 1 of the protocol: claim the minted numbers in the Sheet BEFORE
    anything is uploaded. A crash after this point leaves an unused gap in the
    sequence, which is harmless; a crash after uploading but before reserving
    would leave an item on Internet Archive the Sheet has no record of, and the
    next run's max+1 would mint that same number onto a different photograph -
    permanently.

    RESERVED rows are skipped: their identifier is already in the Sheet and
    rewriting it would be a no-op at best."""
    return [
        CellUpdate(columns.cell(columns.ia_identifier, target.row_number), target.identifier)
        for target in targets
        if target.newly_minted
    ]


def confirm_updates(
    targets: list[UploadTarget], columns: SheetColumns, uploaded_at: str
) -> list[CellUpdate]:
    """Step 3: record what actually happened. `ia_uploaded` is what turns a
    row DONE for every later run, so it is written only for rows whose upload
    genuinely succeeded.

    ia_identifier_bib carries the RESOLVED path, which routinely differs from
    the filename the Sheet holds (225 of 234 real rows carry no extension) -
    it records what was uploaded, not what someone typed."""
    updates: list[CellUpdate] = []
    for target in targets:
        updates.append(CellUpdate(columns.cell(columns.ia_uploaded, target.row_number), uploaded_at))
        updates.append(
            CellUpdate(columns.cell(columns.ia_url, target.row_number), item_url(target.uploaded_as))
        )
        updates.append(
            CellUpdate(
                columns.cell(columns.ia_identifier_bib, target.row_number), target.identifier_bib
            )
        )
    return updates


def cell_value(grid: list[list[str]], row_number: int, column_index: int) -> str:
    """A missing row or a short row reads as "" rather than raising - the
    Sheets API omits trailing empty cells, so a genuinely blank cell and a cell
    past the end of a row are the same thing."""
    index = row_number - 1
    if index < 0 or index >= len(grid):
        return ""
    row = grid[index]
    return (row[column_index] if column_index < len(row) else "").strip()


def sheet_row_fingerprints(
    rows: list[dict[str, str]], file_template: str
) -> dict[int, str]:
    """row_number -> the row's `file_template` candidate, as a fingerprint for
    "is this still the same row?".

    The template's columns are the right fingerprint for one specific reason:
    this tool never writes them. Checking `ia_identifier` instead would be
    tautological on the reserve->confirm leg, because reserve is what put that
    value there - the check would be verifying its own write.

    Must be computed from the RAW cells, before resolve_sheet_files() rewrites
    row['file'] to the resolved name: the comparison is against a fresh read of
    the Sheet, which has raw cells in it, and comparing a resolved name to a
    raw one would report every row as moved.

    A row whose template columns have vanished fingerprints as "" and can
    therefore never match, which is the safe direction."""
    fingerprints: dict[int, str] = {}
    for offset, row in enumerate(rows):
        try:
            fingerprints[offset + 2] = candidate_path(file_template, row)
        except (KeyError, IndexError):
            fingerprints[offset + 2] = ""
    return fingerprints


@dataclass(frozen=True)
class SheetSnapshot:
    """A fresh read of the Sheet, reduced to what the mid-run-edit guard needs:
    where the write-back columns are now, what each row's fingerprint is now,
    the grid itself for reading `ia_identifier` back, and every identifier the
    Sheet currently holds ANYWHERE - see claimed_identifiers."""

    columns: SheetColumns
    grid: list[list[str]]
    fingerprints: dict[int, str]
    # Every non-blank `ia_identifier` in the Sheet right now, whatever row it
    # is on. split_moved_targets only inspects a target's OWN row, which
    # cannot see a number claimed on a DIFFERENT row since this run read the
    # Sheet - and that is the case that mints a duplicate. See
    # check_claimed_identifiers().
    claimed_identifiers: frozenset[str]


def read_sheet_snapshot(client: SheetClient, file_template: str) -> SheetSnapshot:
    grid = client.read_grid()
    column_map, rows = grid_to_rows(grid)
    return SheetSnapshot(
        columns=locate_write_back_columns(column_map),
        grid=grid,
        fingerprints=sheet_row_fingerprints(rows, file_template),
        claimed_identifiers=frozenset(
            identifier
            for row in rows
            if (identifier := (row.get(IA_IDENTIFIER_COLUMN) or "").strip())
        ),
    )


def check_claimed_identifiers(
    targets: list[UploadTarget], snapshot: SheetSnapshot
) -> str | None:
    """Returns a stop reason if any number this run minted has been claimed in
    the Sheet since the run read it, or None.

    plan_upload_targets mints the whole run's numbers up front from a single
    read, as max+1, max+2, ... That read can be hours old by the time the last
    chunk reserves. split_moved_targets checks each target's own row, so it
    catches "someone else took THIS row" - but a number written to a row this
    run is not targeting is invisible to it, and that is precisely the case
    that mints a duplicate: two Sheet rows carrying one permanent identifier,
    with internetarchive.upload() APPENDING files to the existing item rather
    than refusing, so two photographs end up in one unrenameable item.

    Stops the whole run rather than dropping the offending target. Every
    number this run holds came out of the same max+1 arithmetic over the same
    stale read, so one collision means the max was wrong and the rest are
    suspect too - dropping one and proceeding with its neighbours would be
    reserving numbers that are wrong for the same reason. Nothing has been
    reserved or uploaded at that point, so a rerun re-reads, re-mints from the
    real maximum, and proceeds.

    Only newly-minted targets are checked. A RESERVED row's identifier is
    already in the Sheet by definition - that is what RESERVED means - so
    including it here would stop every retry run on its own reservation."""
    collisions = sorted(
        target.identifier
        for target in targets
        if target.newly_minted and target.identifier in snapshot.claimed_identifiers
    )
    if not collisions:
        return None
    return (
        f"identifier(s) {', '.join(collisions)} were claimed in the Sheet after this run read "
        "it, so the numbers this run minted are no longer free. Nothing has been reserved or "
        "uploaded. Rerun to mint from the Sheet's current state"
    )


def split_moved_targets(
    targets: list[UploadTarget], snapshot: SheetSnapshot, reserved_already: bool
) -> tuple[list[UploadTarget], list[UploadTarget]]:
    """Returns (still_at_their_row, moved).

    Row numbers are positional. A human inserting or deleting a row shifts
    every row below it, and the run holds row numbers from a read that may be
    hours old on a full-collection run - the initial read fixes them, and the
    last chunk's reserve write uses them. So this check runs before BOTH
    writes, not just before the confirm.

    Two things must agree. The fingerprint (the file_template columns, which
    this tool never writes) says the row still describes the same photograph.
    `ia_identifier` says nobody else has claimed it: blank for a row about to
    be reserved, and equal to ours once reserved. Checking the identifier alone
    would be tautological after reserve; checking the fingerprint alone would
    miss a row someone else assigned a number to in the meantime."""
    still_there: list[UploadTarget] = []
    moved: list[UploadTarget] = []
    for target in targets:
        expected_identifier = (
            target.identifier if reserved_already or not target.newly_minted else ""
        )
        fingerprint_now = snapshot.fingerprints.get(target.row_number, "")
        matches = (
            bool(fingerprint_now)
            and fingerprint_now == target.source_fingerprint
            and cell_value(snapshot.grid, target.row_number, snapshot.columns.ia_identifier)
            == expected_identifier
        )
        if matches:
            still_there.append(target)
        else:
            moved.append(target)
    return still_there, moved


def sheet_upload_metadata(
    target: UploadTarget, column_map: ColumnMap, mediatype: str
) -> dict[str, str]:
    """The row dict handed to upload_row on the Sheet path.

    upload_row turns every key it is given (bar `identifier` and `file`) into
    an Internet Archive metadata field, and IA metadata is permanent - so the
    tool's own bookkeeping columns and anything a Sheet author marked (LCPS
    Internal) have to be filtered out HERE, before upload_row ever sees them.
    ColumnMap.uploadable_fields() is the single definition of what may be
    uploaded and already excludes both.

    `identifier-bib` and `mediatype` are generated rather than read from a
    column - see docs/DECISIONS.md, "`identifier-bib` and `mediatype` are
    generated, not columns". The surviving test item
    zztest-lcps-sarahsoldphotos-00005 carries a permanently misspelled
    `indentifier-bib` because a header typo shipped once; a generated field
    name cannot do that."""
    uploadable = set(column_map.uploadable_fields()) - DROPPED_BY_UPLOAD_ROW
    metadata_row = {key: value for key, value in target.row.items() if key in uploadable}
    metadata_row["mediatype"] = mediatype
    metadata_row["identifier-bib"] = target.identifier_bib
    metadata_row["file"] = target.row["file"]
    return metadata_row


def plan_upload_targets(
    rows: list[dict[str, str]],
    row_results: list[RowValidation],
    config: ProjectConfig,
    live: bool,
    fingerprints: dict[int, str],
    stamp: str,
) -> list[UploadTarget]:
    """Decides what this run will upload and under which identifier.

    Numbers are minted for the whole run up front, before any chunk is
    reserved: minting is pure arithmetic with no side effect, and doing it once
    means a later chunk cannot re-mint an earlier chunk's numbers by reading a
    Sheet that has not been written yet.

    `existing` deliberately spans EVERY row, including rows that failed
    validation and rows already DONE - a number that appears anywhere in the
    Sheet is spent, whatever the state of the row holding it.

    `stamp` is computed once by the caller (run_stamp(), called once per
    upload_from_sheet() invocation) so every target this run plans - across
    every chunk SheetUploadRun.execute() later processes - shares one stamp."""
    if len(rows) != len(row_results):
        raise ValueError(
            f"plan_upload_targets: got {len(rows)} row(s) but {len(row_results)} row_results - "
            "they must be the same length, in the same order."
        )

    existing = [row.get(IA_IDENTIFIER_COLUMN) or "" for row in rows]

    pending: list[tuple[int, dict[str, str], RowState]] = []
    for offset, (row, result) in enumerate(zip(rows, row_results)):
        # Scope is valid AND ready - readiness is load-bearing here, not a
        # nicety. SHEET_REQUIRED_COLUMNS no longer requires `title` or `file`,
        # so a row nobody has catalogued yet is now perfectly VALID; it is
        # merely NOT_READY. Filtering on is_valid alone uploaded it under a
        # permanent, unrenameable identifier with no title - and, because its
        # `file` is blank, with files_dir itself as the file argument (see the
        # guard at the top of upload_row). This is also what makes `upload`'s
        # scope agree with what `validate`'s lifecycle summary calls "ready to
        # upload"; the two commands must not define that phrase differently.
        if not result.is_valid or result.readiness is Readiness.NOT_READY:
            continue
        state = classify_row(row)
        if state is RowState.DONE:
            continue
        pending.append((offset + 2, row, state))

    unassigned_count = sum(1 for _, _, state in pending if state is RowState.UNASSIGNED)
    minted = iter(
        next_identifiers(existing, config.collection_key, config.project_id, unassigned_count)
    )

    targets: list[UploadTarget] = []
    for row_number, row, state in pending:
        newly_minted = state is RowState.UNASSIGNED
        identifier = next(minted) if newly_minted else (row.get(IA_IDENTIFIER_COLUMN) or "").strip()
        targets.append(
            UploadTarget(
                row=row,
                row_number=row_number,
                identifier=identifier,
                uploaded_as=effective_identifier(identifier, live, stamp),
                identifier_bib=(row.get(IA_IDENTIFIER_BIB_COLUMN) or "").strip(),
                newly_minted=newly_minted,
                source_fingerprint=fingerprints.get(row_number, ""),
            )
        )
    return targets


def write_cells_if_any(client: SheetClient, updates: list[CellUpdate]) -> None:
    """An empty batch is not sent at all. SheetClient.write_cells already
    returns early on an empty list, but the call still shows up in any record
    of what this run did to the Sheet - and "this run issued exactly these
    writes, in this order" is the property the protocol is asserted on."""
    if updates:
        client.write_cells(updates)


@dataclass(frozen=True)
class VerifyOutcome:
    """The result of re-reading the Sheet before a write.

    `stop_reason` distinguishes "these particular rows moved" (None - carry on
    with the rest) from "something happened to the whole Sheet" (a message -
    stop the run). The second case cannot be expressed as a per-row verdict: a
    failed read or a shifted column is equally true of every remaining chunk,
    so continuing would re-read and re-report the entire Sheet on the way to
    the same conclusion."""

    ok: list[UploadTarget]
    moved: list[UploadTarget]
    stop_reason: str | None


@dataclass(frozen=True)
class SheetUploadRun:
    """Everything the reserve -> upload -> confirm loop needs that does not
    change from row to row."""

    client: SheetClient
    columns: SheetColumns
    column_map: ColumnMap
    mediatype: str
    file_template: str
    files_dir: str
    collection: str
    live: bool
    write_back: bool
    log_path: Path
    # Task 12: overridable per run via --chunk-size (upload_from_sheet reads
    # the module-level CHUNK_SIZE itself when the flag is absent, so this
    # still defaults to whatever CHUNK_SIZE is at call time - including a
    # test's own monkeypatched value).
    chunk_size: int = CHUNK_SIZE

    def execute(self, targets: list[UploadTarget]) -> dict[str, int]:
        """One chunk at a time: verify, reserve, upload, verify, confirm,
        having logged each row's outcome as it happened.

        Chunking is what keeps this inside the Sheets API's 60 writes per
        minute per user - a batch counts as one request, so ~10,000 rows cost
        about 40 requests instead of 10,000. It is also why the guard has to
        run per chunk: the last chunk's reserve write can be hours after the
        read that fixed its row numbers.

        `self.chunk_size` is read here rather than taken as chunk_rows'
        default so the chunk boundary is reachable in a test - a protocol
        that is only ever exercised with a single chunk is a protocol nobody
        has tested. It defaults to CHUNK_SIZE (see the field above) and is
        overridable per run via --chunk-size.

        A rate-limited row (is_rate_limit_error() matches its exception)
        stops the run after finishing this chunk's confirm write, rather
        than being logged as an ordinary failure and moving on to the next
        target - see RateLimited's docstring. Every row this run already
        uploaded successfully, in this chunk or an earlier one, is still
        confirmed before returning: a rate limit must not leave a row
        RESERVED-but-unconfirmed, which would make tomorrow's run re-upload
        it under a second identifier."""
        counts = {"success": 0, "failure": 0, "unconfirmed": 0, "not_attempted": 0, "rate_limited": 0}
        total = len(targets)
        position = 0
        # Targets that already have a verdict of any kind - uploaded, failed, or
        # reported as moved. `total - settled` is therefore exactly what a
        # run-stopping problem leaves unattempted, with nothing counted twice.
        settled = 0

        for chunk in chunk_rows(targets, self.chunk_size):
            # Every chunk gets a fresh timestamp. One timestamp for the whole
            # run would stamp chunk 20 with the time chunk 1 started, which on
            # a full-collection run is hours wrong.
            uploaded_at = upload_timestamp()
            working = chunk

            if self.write_back:
                outcome = self._verify(chunk, reserved_already=False)
                for target in outcome.moved:
                    counts["not_attempted"] += 1
                    settled += 1
                    self._report_moved(target, uploaded=False, cause=outcome.stop_reason)
                if outcome.stop_reason is not None or not self._write(
                    reserve_updates(outcome.ok, self.columns), "reserve"
                ):
                    counts["not_attempted"] += total - settled
                    return counts
                working = outcome.ok

            succeeded: list[UploadTarget] = []
            rate_limited = False
            for target in working:
                position += 1
                settled += 1
                print(f"[{position}/{total}] uploading {target.uploaded_as} ({target.row['file']})")
                try:
                    upload_row(
                        sheet_upload_metadata(target, self.column_map, self.mediatype),
                        target.uploaded_as,
                        self.collection,
                        self.files_dir,
                    )
                except Exception as exc:
                    # Wrapping (rather than a bare is_rate_limit_error() check
                    # here) keeps the detection logic in one place and makes
                    # the `except RateLimited` below the only thing that can
                    # stop the run - str(exc) is unchanged either way, so the
                    # logged message is identical to an ordinary failure's.
                    if is_rate_limit_error(exc):
                        exc = RateLimited(str(exc))
                    counts["failure"] += 1
                    self._log(target, "failure", error=str(exc))
                    if isinstance(exc, RateLimited):
                        rate_limited = True
                        break
                    continue
                counts["success"] += 1
                self._log(target, "success")
                succeeded.append(target)

            if self.write_back and succeeded:
                outcome = self._verify(succeeded, reserved_already=True)
                for target in outcome.moved:
                    counts["unconfirmed"] += 1
                    self._report_moved(target, uploaded=True, cause=outcome.stop_reason)
                if outcome.stop_reason is not None:
                    counts["not_attempted"] += total - settled
                    return counts
                if not self._write(confirm_updates(outcome.ok, self.columns, uploaded_at), "confirm"):
                    counts["unconfirmed"] += len(outcome.ok)
                    for target in outcome.ok:
                        self._log(target, "unconfirmed", error="the Sheet write failed")
                    counts["not_attempted"] += total - settled
                    return counts

            if rate_limited:
                attempted = counts["success"] + counts["failure"]
                counts["not_attempted"] += total - settled
                counts["rate_limited"] = 1
                print(
                    f"stopped: Internet Archive reported a rate limit after "
                    f"{_pluralize(attempted, 'item')}",
                    file=sys.stderr,
                )
                print(f"{counts['success']} uploaded this run - resume by re-running tomorrow")
                return counts

        return counts

    def _verify(self, targets: list[UploadTarget], reserved_already: bool) -> VerifyOutcome:
        """Re-reads the Sheet and splits the targets into those still at the
        row this run planned for them and those that have moved. Runs before
        BOTH writes - see split_moved_targets for why the fingerprint, and not
        `ia_identifier`, is what makes the check meaningful.

        This step added two Sheets READS per chunk - roughly 80 across a
        full-collection run spanning hours - so one transient 503 among them is
        likely rather than exotic, and it must not end a run that has already
        created thousands of permanent Internet Archive items with a stack
        trace. Both failure modes below stop the run cleanly instead: the
        caller still prints the summary and the log path, and every affected
        row is logged."""
        try:
            snapshot = read_sheet_snapshot(self.client, self.file_template)
        except MissingWriteBackColumns as exc:
            return VerifyOutcome([], list(targets), f"a column this run writes to is gone: {exc}")
        except Exception as exc:
            return VerifyOutcome([], list(targets), f"the Sheet could not be re-read: {exc}")

        if snapshot.columns != self.columns:
            # A column was inserted, deleted or renamed. Every cached column
            # index is now wrong, so every write this run could make would land
            # in the wrong column - which is true for every remaining chunk,
            # not just this one, hence a stop_reason rather than a per-row
            # verdict that would re-read and re-report the whole Sheet 20 more
            # times on the way to the same conclusion.
            return VerifyOutcome(
                [],
                list(targets),
                "the Sheet's columns moved while this run was in progress, so every cell it "
                "would write now lands in the wrong column",
            )

        if not reserved_already:
            # Only on the reserve leg. After reserve, this run's own numbers
            # ARE in the Sheet - checking then would flag every one of them.
            collision = check_claimed_identifiers(targets, snapshot)
            if collision is not None:
                return VerifyOutcome([], list(targets), collision)

        ok, moved = split_moved_targets(targets, snapshot, reserved_already)
        return VerifyOutcome(ok, moved, None)

    def _report_moved(
        self, target: UploadTarget, uploaded: bool, cause: str | None = None
    ) -> None:
        """`cause` names a whole-Sheet problem (a failed read, a moved column)
        when there is one. Without it this said "row N is no longer the row
        this run planned for" for a COLUMN change too, which is wrong in kind
        and sends the operator to look at the wrong thing.

        The filename is on screen, not just in the log, because for a row that
        was never uploaded the identifier exists nowhere yet and the row number
        is precisely what has gone stale - `file` is the only durable handle
        the operator has left."""
        what_changed = cause or (
            f"row {target.row_number} is no longer the row this run planned for "
            f"'{target.identifier}' - the Sheet was edited while the run was in progress, so "
            "writing there would land on a different photograph"
        )
        outcome = (
            f"The item IS on Internet Archive as '{target.uploaded_as}' but is NOT recorded in "
            "the Sheet."
            if uploaded
            else "Nothing was uploaded for it."
        )
        message = (
            f"{what_changed}. {outcome} File: '{target.row['file']}' (planned row "
            f"{target.row_number}, identifier '{target.identifier}'). Rerun once the Sheet has "
            "settled."
        )
        print(message, file=sys.stderr)
        self._log(target, "unconfirmed" if uploaded else "skipped", error=message)

    def _write(self, updates: list[CellUpdate], step: str) -> bool:
        """A Sheets write failing (a 503, an expired token, a revoked share) is
        an ordinary operational event, not a reason to hand the operator a
        traceback in place of the run summary and the log path."""
        try:
            write_cells_if_any(self.client, updates)
        except Exception as exc:
            print(
                f"the Sheet {step} write failed: {exc}. Stopping here rather than uploading more "
                "items this run cannot record. Nothing is lost - rerun once the Sheet is "
                "reachable and every unrecorded row is picked up from where it stopped.",
                file=sys.stderr,
            )
            return False
        return True

    def _log(self, target: UploadTarget, status: str, error: str | None = None) -> None:
        log_result(
            self.log_path,
            target.identifier,
            target.row["file"],
            status,
            self.live,
            error=error,
            uploaded_as=target.uploaded_as,
        )


def print_dry_run(
    targets: list[UploadTarget], columns: SheetColumns, write_back: bool, uploaded_at: str
) -> None:
    if not targets:
        print("nothing to upload")
        return

    print(f"would upload {_pluralize(len(targets), 'item')}:")
    for target in targets:
        if target.newly_minted:
            print(
                f"  row {target.row_number}: would mint '{target.identifier}' and upload it as "
                f"'{target.uploaded_as}' ({target.row['file']})"
            )
        else:
            print(
                f"  row {target.row_number}: would upload under its existing identifier "
                f"'{target.identifier}', as '{target.uploaded_as}' ({target.row['file']})"
            )
    print()

    if not write_back:
        print(
            "would write nothing to the Sheet - neither --live nor --write-identifier was passed"
        )
        return

    print("would write these cells:")
    for update in reserve_updates(targets, columns) + confirm_updates(targets, columns, uploaded_at):
        print(f"  {update.a1} = {update.value}")


def cmd_upload(args) -> int:
    # `is not None`, not truthiness, and matching cmd_validate: --csv "" must
    # be an explicit (if useless) request to read a CSV named "", and fail as
    # such, rather than silently falling through to the Sheet path because an
    # empty string is falsy.
    csv_path = getattr(args, "csv", None)
    if csv_path is not None:
        return upload_from_csv(args, csv_path)
    return upload_from_sheet(args)


def upload_from_csv(args, csv_path: str) -> int:
    """Unchanged in behavior: a CSV is small and hand-prepared, so any failing
    row means the file is wrong and nothing is uploaded. See docs/DECISIONS.md,
    "On the Sheet path, `upload` uploads the valid rows and reports the rest"
    for why the Sheet path deliberately does the opposite."""
    if getattr(args, "write_identifier", False) or getattr(args, "dry_run", False):
        print(
            "--write-identifier and --dry-run describe what happens to the project's Sheet, so "
            "they apply to the Sheet path only. Drop --csv to run against the Sheet.",
            file=sys.stderr,
        )
        return 1

    # Task 12: --limit and --chunk-size describe SheetUploadRun's own
    # reserve/upload/confirm batching and quota-stopping behavior, which the
    # CSV path (a small, hand-prepared file uploaded via run_rows - see this
    # function's own docstring) does not have. Silently ignoring an explicit
    # flag on the wrong path is its own trap (see --collection's old "lcps"
    # default, above), so this is checked the same way --write-identifier and
    # --dry-run are just above rather than left to do nothing. Only an
    # explicitly-changed --chunk-size trips this: the default (unset, or
    # equal to CHUNK_SIZE) is indistinguishable from "the flag was never
    # passed" and must not block an ordinary --csv run.
    limit = getattr(args, "limit", None)
    chunk_size = getattr(args, "chunk_size", None)
    if limit is not None or (chunk_size is not None and chunk_size != CHUNK_SIZE):
        print(
            "--limit and --chunk-size describe the Sheet path's batching and quota-stopping "
            "behavior, so they apply to the Sheet path only. Drop --csv to run against the "
            "Sheet.",
            file=sys.stderr,
        )
        return 1

    files_dir = getattr(args, "files_dir", None) or "."
    collection = TEST_COLLECTION
    if args.live:
        collection = getattr(args, "collection", None)
        if not collection:
            # The old "lcps" default was not a real Internet Archive
            # collection: a --live run pushed real files at a collection that
            # does not exist and reported success. Guessing is worse than
            # refusing.
            print(
                "--live needs an explicit --collection on the --csv path; there is no default "
                "any more. Drop --csv to take the collection from the project's registry entry "
                "instead.",
                file=sys.stderr,
            )
            return 1

    data = read_csv(csv_path)
    rows = data.rows
    registry = load_registry(args.registry)

    skip_identifiers: set[str] = set()
    if args.resume_from:
        skip_identifiers = load_prior_successes(args.resume_from, args.live)

    to_upload = [row for row in rows if (row.get("identifier") or "").strip() not in skip_identifiers]

    validation_results = header_validation(data.fieldnames) + validate_csv_rows(
        rows, files_dir, registry, frozenset(skip_identifiers)
    )
    if not all(r.is_valid for r in validation_results):
        print(format_report(validation_results))
        print(
            "validation failed; run 'validate' and fix the errors above before uploading",
            file=sys.stderr,
        )
        return 1

    log_path = open_log(args.log_dir, "upload")
    for identifier in skip_identifiers:
        log_result(log_path, identifier, "", "success", args.live, error="carried over from resumed log")

    counts = run_rows(
        to_upload,
        log_path,
        args.live,
        run_stamp(),
        action="uploading",
        process_row=lambda row, target: upload_row(row, target, collection, files_dir),
        describe=lambda row, target: f"{target} ({row['file'].strip()})",
        file_value_for=lambda row: row["file"].strip(),
    )

    print(f"{counts['success']} file(s) uploaded successfully, {counts['failure']} error(s)")
    print(f"log written to {log_path}")
    return 1 if counts["failure"] else 0


def upload_from_sheet(args) -> int:
    registry = load_registry(args.registry)
    config = load_project_config(registry, args.project)

    live = bool(args.live)
    dry_run = bool(getattr(args, "dry_run", False))
    # --live always records. An item that exists on Internet Archive under a
    # permanent identifier the Sheet does not know about is precisely what the
    # reserve-first ordering exists to prevent, so there is no live-without-
    # write-back mode to opt into.
    write_back = live or bool(getattr(args, "write_identifier", False))
    sheet_id = config.sheet_id_for(live)
    mode = "live" if live else "test"

    print(
        f"project '{config.project_id}': {mode} mode, spreadsheet '{sheet_id}', "
        f"tab '{config.sheet_tab}'"
    )
    if dry_run:
        print("--dry-run: nothing is uploaded, and nothing is written to the Sheet")
    elif write_back:
        print(f"results WILL be written back to spreadsheet '{sheet_id}'")
    else:
        print(
            "nothing will be written back to the Sheet - pass --write-identifier to record "
            "identifiers there"
        )
    print()

    for flag, value in (
        ("--collection", getattr(args, "collection", None)),
        ("--files-dir", getattr(args, "files_dir", None)),
    ):
        # Silently ignoring an explicit flag is its own trap, and this one used
        # to be the dangerous kind: --collection defaulting to "lcps" would
        # have sent real photographs to a collection that does not exist.
        if value:
            print(
                f"{flag} is a --csv-path override; on the Sheet path this comes from project "
                f"'{config.project_id}' in {args.registry}. Remove {flag}, or change the "
                "registry.",
                file=sys.stderr,
            )
            return 1

    if getattr(args, "resume_from", None):
        print(
            "--resume-from is a --csv-path flag. On the Sheet path the 'ia_uploaded' column is "
            "the record of what is already done, so a rerun picks up where the last one stopped "
            "by itself.",
            file=sys.stderr,
        )
        return 1

    # Task 12: read and validate --limit/--chunk-size as early as possible -
    # before any Sheet I/O, field-receipt printing, or validation work - so a
    # typo'd flag fails fast instead of only surfacing after the run has
    # already done everything short of uploading.
    limit = getattr(args, "limit", None)
    if limit is not None and limit <= 0:
        # Silently doing nothing is the trap here, not a crash: a limit of
        # zero (or negative) would slice plan_upload_targets()'s output down
        # to nothing, upload zero items, and still report the run as clean -
        # the operator would have no reason to suspect --limit was the cause.
        print(
            f"--limit must be a positive number of items, not {limit}. A run with nothing to "
            "upload is what dropping --limit already means - drop it instead of passing zero "
            "or a negative number.",
            file=sys.stderr,
        )
        return 1

    # getattr's default is looked up fresh on every call (it is an ordinary
    # function argument, not a class default evaluated once at import time),
    # so falling back to the module-level CHUNK_SIZE here - rather than
    # snapshotting it into make_upload_args()'s Namespace - is what keeps
    # every existing `monkeypatch.setattr("ia_bulk.CHUNK_SIZE", 1)` test
    # working: those tests never set args.chunk_size at all, so this always
    # sees whatever CHUNK_SIZE is right now.
    chunk_size = getattr(args, "chunk_size", None)
    if chunk_size is None:
        chunk_size = CHUNK_SIZE
    if chunk_size <= 0:
        # Two different failure modes, both worse than a clean refusal:
        # chunk_rows()'s range(0, len(rows), chunk_size) raises ValueError
        # mid-run for 0 (a bare traceback in place of the run summary), and
        # silently produces ZERO chunks for a negative value - the run
        # uploads nothing and still reports success, exactly like the
        # --limit <= 0 case above.
        print(
            f"--chunk-size must be a positive number of items, not {chunk_size}. Zero raises "
            "inside chunk_rows(); a negative value silently produces zero chunks, uploading "
            "nothing while the run still reports success.",
            file=sys.stderr,
        )
        return 1

    if sheet_id.startswith(PLACEHOLDER_SHEET_ID_PREFIX):
        print(
            f"the {mode}-mode spreadsheet ID for project '{config.project_id}' is still the "
            f"placeholder '{sheet_id}' - edit it in {args.registry} to the real Google Sheet ID "
            "before running upload.",
            file=sys.stderr,
        )
        return 1

    client = build_sheet_client(config, live)
    try:
        grid = client.read_grid()
    except HttpError as exc:
        print(
            f"could not read spreadsheet '{sheet_id}' tab '{config.sheet_tab}': {exc}. Check "
            f"that 'sheet_tab' in {args.registry} names the tab exactly (case-sensitive) as it "
            "appears in the Sheet, that the spreadsheet ID is correct, and that the Sheet has "
            "been shared with the Google account you authorized as.",
            file=sys.stderr,
        )
        return 1

    column_map, rows = grid_to_rows(grid)
    for row in rows:
        row["mediatype"] = config.mediatype

    structure_results = sheet_structure_validation(column_map, grid)

    if not rows:
        if structure_results:
            print("\n".join(_format_result_lines(structure_results)))
            print()
        print(
            "the Sheet has no data rows (only a header, or nothing at all) - check that "
            "'sheet_tab' in the project's registry entry names the right tab, and that "
            "the Sheet has actually been populated and shared"
        )
        return 1

    try:
        check_file_template(config.file_template, column_map)
    except TemplateError as exc:
        print(
            f"project '{config.project_id}': {exc} - fix 'file_template' in {args.registry}",
            file=sys.stderr,
        )
        return 1

    config_errors = check_required_for_upload(config, column_map)
    if config_errors:
        print("\n".join(config_errors), file=sys.stderr)
        print(
            f"fix required_for_upload in {args.registry} - as written, every row would "
            "be reported as not yet catalogued and nothing would ever upload",
            file=sys.stderr,
        )
        return 1

    try:
        columns = locate_write_back_columns(column_map)
    except MissingWriteBackColumns as exc:
        print(f"project '{config.project_id}': {exc}", file=sys.stderr)
        return 1

    # Fingerprints come from the RAW cells, so they must be taken before
    # resolve_sheet_files() rewrites row['file'] to the resolved name. They are
    # what every later mid-run-edit check compares against.
    source_fingerprints = sheet_row_fingerprints(rows, config.file_template)

    file_outcomes = resolve_sheet_files(rows, config)
    header_results, row_results = validate_sheet_grid(
        rows, registry, config, structure_results, file_outcomes
    )

    if header_results:
        # A header defect (two columns normalizing to the same IA field name,
        # a column normalizing to nothing) silently corrupts EVERY row, so
        # unlike a bad row it cannot be routed around by skipping it.
        print("\n".join(_format_result_lines(header_results)))
        print()
        print(
            "the Sheet's header row has problems that affect every row - refusing to upload "
            "anything until they are fixed",
            file=sys.stderr,
        )
        return 1

    # `upload` owns the run, not the data: it itemizes only rows it would
    # otherwise have uploaded (`blocked` - ready, but invalid) and gives one
    # contained line for the rest (`not_ready` - never yet catalogued, so
    # never in this run's scope to begin with). `validate` owns the
    # per-field breakdown of the backlog - repeating it here would make
    # `upload` loud about the ~2,900 uncatalogued rows on every single run,
    # which is the exact noise this split exists to stop.
    blocked = [
        result
        for result in row_results
        if not result.is_valid and result.readiness is Readiness.READY
    ]
    not_ready = [result for result in row_results if result.readiness is Readiness.NOT_READY]
    not_ready_broken = [result for result in not_ready if not result.is_valid]

    if blocked:
        print("\n".join(_format_result_lines(blocked)))
        print(
            f"{_pluralize(len(blocked), 'row')} failed validation and will be skipped; the rest "
            "are uploaded, and this command still exits non-zero so a partial run is never "
            "mistaken for a clean one"
        )
    if not_ready:
        # The sub-count is deliberately CONTAINED inside the not-ready
        # sentence - in parentheses, mid-sentence - rather than printed as
        # a second number beside it: two adjacent numbers read as a
        # partition (as if they summed to something) unless something says
        # otherwise, and a not-ready-and-broken row is a SUBSET of the
        # not-ready total, not a disjoint group next to it.
        line = f"{_pluralize(len(not_ready), 'row')} not yet catalogued"
        if not_ready_broken:
            verb = "has" if len(not_ready_broken) == 1 else "have"
            line += (
                f" ({len(not_ready_broken)} of them also {verb} an unresolvable "
                "filename - run `validate` to see them)"
            )
        print(line)
    if blocked or not_ready:
        print()

    targets = plan_upload_targets(rows, row_results, config, live, source_fingerprints, run_stamp())

    # Task 12: --limit counts PLANNED targets (valid AND ready AND not
    # already done), not Sheet rows scanned - plan_upload_targets has
    # already done that filtering above, so slicing ITS OUTPUT here (never
    # `rows`/`row_results` before it runs - that would count raw Sheet rows
    # instead, a materially different and wrong reading, see
    # docs/DECISIONS.md) is what makes "--limit 100" mean "100 of the rows
    # actually in scope", not "stop after the first 100 rows read". Numbers
    # were minted for every pending row before this slice runs
    # (plan_upload_targets mints for the whole run up front - see its own
    # docstring), but minting is pure arithmetic with no side effect: a
    # target dropped here is never reserved, so its number is never spent
    # and next_identifiers() mints it again next run. `limit` and
    # `chunk_size` were already read and validated at the top of this
    # function.
    if limit is not None:
        targets = targets[:limit]

    collection = config.ia_collection if live else TEST_COLLECTION

    # `upload` is where something permanent happens, so it shows the same
    # field receipt `validate` does rather than assuming the operator ran
    # validate first and remembers what it said.
    print(format_field_receipt(column_map))
    print()

    if dry_run:
        print_dry_run(targets, columns, write_back, upload_timestamp())
        return 1 if blocked else 0

    if not targets:
        print("nothing to upload - every valid row is already marked uploaded")
        return 1 if blocked else 0

    log_path = open_log(args.log_dir, "upload")
    try:
        log_run_header(log_path, config, column_map, live, dry_run, limit=limit, chunk_size=chunk_size)
    except Exception as exc:
        # This record is a receipt for later, not part of the upload itself -
        # a run about to create permanent Internet Archive items must not be
        # stopped by a failure to write it. Same reasoning as _write() below:
        # a clean stderr message, never a traceback in place of the actual
        # work this command exists to do.
        print(
            f"could not write the run-header record to {log_path}: {exc}. Continuing without "
            "it - this only affects the log's own audit trail, not the upload that follows.",
            file=sys.stderr,
        )
    counts = SheetUploadRun(
        client=client,
        columns=columns,
        column_map=column_map,
        mediatype=config.mediatype,
        file_template=config.file_template,
        files_dir=config.files_dir,
        collection=collection,
        live=live,
        write_back=write_back,
        log_path=log_path,
        chunk_size=chunk_size,
    ).execute(targets)

    print(f"{counts['success']} file(s) uploaded successfully, {counts['failure']} error(s)")
    if counts["unconfirmed"]:
        print(
            f"{_pluralize(counts['unconfirmed'], 'item')} uploaded but NOT recorded in the Sheet "
            "- each one is named on stderr above, and in the log"
        )
    if counts["not_attempted"]:
        print(
            f"{_pluralize(counts['not_attempted'], 'row')} not attempted - the run stopped early; "
            "see the reason on stderr above"
        )
    if blocked:
        print(f"{_pluralize(len(blocked), 'row')} skipped (failed validation)")
    print(f"log written to {log_path}")
    return (
        1
        if (counts["failure"] or counts["unconfirmed"] or counts["not_attempted"] or blocked)
        else 0
    )


def cmd_sync_metadata(args) -> int:
    data = read_csv(args.csv)
    rows = data.rows
    registry = load_registry(args.registry)

    skip_identifiers: set[str] = set()
    if args.resume_from:
        skip_identifiers = load_prior_successes(args.resume_from, args.live)

    to_sync = [row for row in rows if (row.get("identifier") or "").strip() not in skip_identifiers]

    validation_results = header_validation(data.fieldnames) + validate_identifiers(
        rows, registry, frozenset(skip_identifiers)
    )
    if not all(r.is_valid for r in validation_results):
        print(format_report(validation_results))
        print("identifier validation failed; fix the errors above before syncing", file=sys.stderr)
        return 1

    log_path = open_log(args.log_dir, "sync-metadata")
    for identifier in skip_identifiers:
        log_result(log_path, identifier, "", "success", args.live, error="carried over from resumed log")

    counts = run_rows(
        to_sync,
        log_path,
        args.live,
        run_stamp(),
        action="updating metadata for",
        process_row=lambda row, target: update_metadata_row(row, target),
        describe=lambda row, target: target,
        file_value_for=lambda row: "",
    )

    print(f"{counts['success']} item(s) updated successfully, {counts['unchanged']} unchanged, {counts['failure']} error(s)")
    print(f"log written to {log_path}")
    return 1 if counts["failure"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ia_bulk",
        description=(
            "Validate, upload, and sync metadata for Internet Archive items from "
            "a project's Google Sheet (read live) or, for validate, an offline CSV."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate", help="Validate a project's Sheet, or an offline CSV, without touching the network"
    )
    validate_parser.add_argument("--project", required=True, help="Project ID from the registry")
    validate_parser.add_argument(
        "--csv", default=None, help="Validate this CSV offline instead of reading the project's Sheet"
    )
    validate_parser.add_argument(
        "--files-dir", default=".", help="Base directory the CSV's 'file' column is resolved against (--csv only)"
    )
    validate_parser.add_argument("--registry", default="projects_registry.json", help="Path to the project registry JSON")
    validate_parser.add_argument(
        "--live",
        action="store_true",
        help="Read the project's real Sheet instead of its test Sheet (ignored with --csv)",
    )

    upload_parser = subparsers.add_parser(
        "upload", help="Upload items from a project's Sheet, or from an offline CSV"
    )
    upload_parser.add_argument("--project", required=True, help="Project ID from the registry")
    upload_parser.add_argument(
        "--csv", default=None, help="Upload from this CSV instead of the project's Sheet"
    )
    # No defaults on --files-dir/--collection. Both are technical
    # configuration that belongs in the registry, confirmed once in version
    # control per project, rather than retyped correctly on every run forever;
    # --collection's old "lcps" default was not even a real Internet Archive
    # collection. See docs/DECISIONS.md, "Technical configuration lives in the
    # registry, not the command line".
    upload_parser.add_argument(
        "--files-dir",
        default=None,
        help="Base directory the 'file' column is resolved against (--csv only; the Sheet path takes it from the registry)",
    )
    upload_parser.add_argument("--registry", default="projects_registry.json", help="Path to the project registry JSON")
    upload_parser.add_argument("--live", action="store_true", help="Target the real Sheet and the registry's real collection instead of the test Sheet and test_collection")
    upload_parser.add_argument(
        "--collection",
        default=None,
        help="Collection to upload to when --live is passed (--csv only; the Sheet path takes it from the registry's ia_collection)",
    )
    upload_parser.add_argument(
        "--write-identifier",
        action="store_true",
        help="Write minted identifiers and results back to the TEST Sheet (--live always writes back)",
    )
    upload_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Upload nothing and write nothing; print the identifiers that would be minted and the cells that would be written",
    )
    upload_parser.add_argument("--log-dir", default="logs", help="Directory to write the timestamped run log to")
    upload_parser.add_argument("--resume-from", default=None, help="Path to a prior log; identifiers marked success there are skipped (--csv only)")
    upload_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Upload at most this many items this run (Sheet path only; must be positive). "
            "Counts rows actually in scope to upload - valid AND ready AND not already done - "
            "not every row scanned; on a Sheet with 2,900 uncatalogued rows and 150 ready ones, "
            "--limit 100 uploads 100 of the 150 ready rows, not the first 100 rows read. "
            "Combines with --chunk-size as 'this many total, batched this way': --limit 10 "
            "--chunk-size 3 uploads 10 items in chunks of 3, not 10 chunks of 3."
        ),
    )
    upload_parser.add_argument(
        "--chunk-size",
        type=int,
        default=CHUNK_SIZE,
        help=(
            f"Items per reserve/upload/confirm batch (Sheet path only; must be positive; "
            f"default {CHUNK_SIZE}, Internet Archive's own per-run item cap). Applied to "
            "whatever --limit leaves, not instead of it - see --limit's help for the exact "
            "combination."
        ),
    )

    sync_parser = subparsers.add_parser("sync-metadata", help="Update metadata on already-uploaded items")
    sync_parser.add_argument("csv", help="Path to the CSV of identifier + changed metadata columns")
    sync_parser.add_argument("--project", required=True, help="Project ID from the registry")
    sync_parser.add_argument("--registry", default="projects_registry.json", help="Path to the project registry JSON")
    sync_parser.add_argument("--live", action="store_true", help="Target the real collection instead of test_collection")
    sync_parser.add_argument("--log-dir", default="logs", help="Directory to write the timestamped run log to")
    sync_parser.add_argument("--resume-from", default=None, help="Path to a prior log; identifiers marked success there are skipped")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "upload":
        return cmd_upload(args)
    if args.command == "sync-metadata":
        return cmd_sync_metadata(args)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
