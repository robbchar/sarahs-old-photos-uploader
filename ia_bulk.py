"""Bulk validate/upload/sync-metadata CLI for Internet Archive, driven by a
project's Google Sheet (read live) or, for offline/dry-run work, a CSV
exported from it. See docs/ARCHITECTURE.md for the CSV schema and identifier
scheme this script assumes, and docs/DECISIONS.md ("The Sheet is read live")
for why both sources exist."""
from __future__ import annotations

import argparse
import csv
import functools
import json
import os
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
from identifiers import RowState, classify_row, next_identifiers, parse_identifier
from project_config import ProjectConfig, load_project_config, unregistered_project_error
from reconcile import AmbiguousMatch, Proposal, propose_match
from sheet_client import CellUpdate, SheetClient, column_letter

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
# Internet Archive's per-account daily item cap. CHUNK_SIZE covers the
# 500-items-per-run half of the limit in `.claude/CLAUDE.md` ("IA batch
# limits: 500 items per upload run, 5000/day"); this covers the other half,
# which nothing enforced. Applies in test mode too - a rehearsal uploads to
# test_collection through the same account and spends the same quota.
DAILY_ITEM_CAP = 5000
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
    project_id: str,
    seen_identifiers: dict[str, int],
    column_name: str = "identifier",
) -> list[str]:
    """project_id is the run's own --project, and it has no default on
    purpose. This function used to ask only whether a prefix belonged to
    SOME project in the registry, which accepted `lcps-otherproject-00099`
    on a `--project astoriaphotos` run (issue #2) - an item filed under
    another project's numbering, quietly, under a name that can never be
    renamed. A default here would let the next call site re-introduce
    exactly that, so every caller has to say which project it means.

    column_name defaults to "identifier" (the CSV path, unchanged) and
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
    # identifiers.parse_identifier is the single decoder for the scheme - see
    # its docstring for why this file no longer carries a second copy of the
    # pattern.
    parsed = parse_identifier(identifier)
    if parsed is None:
        errors.append(
            f"{column_name} '{identifier}' does not match scheme COLLECTIONKEY-PROJECTID-NUMBER"
        )
    else:
        collection_key, identifier_project, _number = parsed
        collection_matches = collection_key == registry.get("collection_key")
        project_registered = identifier_project in registry.get("projects", {})
        if not (collection_matches and project_registered):
            errors.append(
                f"{column_name} prefix '{collection_key}-{identifier_project}' not found in "
                "project registry"
            )
        elif identifier_project != project_id:
            # Kept distinct from the "not found" message above because the
            # two are different mistakes with different fixes: an
            # unregistered prefix means the identifier is wrong, while a
            # registered-but-other prefix means --project may be the thing
            # that is wrong. Both are named so the operator can tell which.
            errors.append(
                f"{column_name} '{identifier}' belongs to project "
                f"'{identifier_project}', but this run is --project {project_id}"
            )

    if identifier in seen_identifiers:
        errors.append(f"{column_name} '{identifier}' duplicates row {seen_identifiers[identifier]}")
    else:
        seen_identifiers[identifier] = row_number

    return errors


def refuse_unregistered_project(registry: dict, project_id: str) -> bool:
    """True if the run must stop. The --csv paths never build a
    ProjectConfig, so they never inherited load_project_config's
    unknown-project guard - which cost nothing while --project went unread
    there, and started costing the moment check_identifier began comparing
    every row against it (issue #2). Without this, a mistyped --project
    fails every single row with "belongs to project 'astoriaphotos', but
    this run is --project astoriaphoto": true, useless, and repeated once
    per row, with the flag that is actually wrong named only in passing.
    """
    message = unregistered_project_error(registry, project_id)
    if message is None:
        return False
    print(message, file=sys.stderr)
    return True


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
    project_id: str,
    skip_identifiers: frozenset[str] = frozenset(),
    required_columns: tuple[str, ...] = REQUIRED_UPLOAD_COLUMNS,
    check_file_exists: bool = True,
    identifier_column: str = "identifier",
    required_for_upload: tuple[str, ...] = (),
) -> list[RowValidation]:
    """project_id is the run's own --project. It is threaded through to
    check_identifier and used nowhere else here - see that function for why
    it is required rather than defaulted (issue #2).

    skip_identifiers lets a --resume-from run skip re-validating rows a
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
                    identifier,
                    row_number,
                    registry,
                    project_id,
                    seen_identifiers,
                    identifier_column,
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
    project_id: str,
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
        project_id,
        skip_identifiers=skip_identifiers,
        required_columns=REQUIRED_UPLOAD_COLUMNS,
        check_file_exists=True,
        identifier_column="identifier",
    )


def validate_sheet_rows(
    rows: list[dict[str, str]],
    files_dir: str | Path,
    registry: dict,
    project_id: str,
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
        project_id,
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


CONSOLE_ERROR_WIDTH = 300


def format_row_error(exc: Exception) -> str:
    """A failing row's error, as one line to print underneath it.

    "1 error(s)" plus a path to a JSONL file was the entire console output
    for a failed row. This tool is meant to be run by volunteers who are
    comfortable with spreadsheets and not with code, and the information
    already existed - the log's `error` field held the full message, it just
    never reached the screen.

    Whitespace is collapsed because Internet Archive's S3 failures carry a
    multi-line XML body; dumped raw under a progress line it swamps the
    [N/M] rhythm the operator is reading. Truncated for the same reason. The
    log keeps the complete text, which is what the log is for.
    """
    text = " ".join(str(exc).split())
    if len(text) > CONSOLE_ERROR_WIDTH:
        text = text[:CONSOLE_ERROR_WIDTH - 3] + "..."
    return text


def _pluralize(count: int, noun: str) -> str:
    """Headline counts reach ~3,000 on the real Sheet, so thousands are
    separated. Any raw count printed beside a _pluralize line must use the
    same {:,} format, or adjacent lines disagree about how a number looks."""
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


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

    lines.append(f"{counts[(RowState.DONE, 'ready')]:,} already uploaded")
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
        f"{counts[(RowState.RESERVED, 'ready')]:,} reserved but unconfirmed - will retry "
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
        lines.append(f"    {count:,} missing {name}")
    if any(len(result.missing_fields) > 1 for result in not_ready):
        lines.append(
            "    (a row missing more than one field appears in more than one count "
            f"above, so these do not sum to {len(not_ready):,})"
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


def _read_log_results(log_path: str | Path, live: bool) -> list[dict]:
    """Every row-result record in a prior run's log, for the SAME mode (test
    vs --live) as this run. Damaged lines are skipped and reported.

    Shared by load_prior_successes() and load_uploaded_as(), which ask two
    different questions of the same records. The mode filter is load-bearing
    for both: a test-mode entry only ever confirms that the zztest-prefixed
    item landed in test_collection, never the real one, so it must not answer
    for a --live run (or vice versa). Logs from before the "live" field
    existed have no mode recorded and are treated conservatively as matching
    neither.

    A line that will not parse is SKIPPED, not raised on. log_result appends
    one line per row with no atomic write, so a run killed mid-write - Ctrl-C,
    a full disk, a closed laptop - leaves a truncated final line. Raising
    there made the log permanently unusable, disabling the only recovery
    mechanism the CSV path has using the exact crash it exists to recover
    from. Every intact line before the damaged one is still a real record and
    is still honored.

    Damaged lines are counted and reported on stderr rather than skipped in
    silence: a lost line is a row this run no longer knows anything about, and
    the operator should know why the run does not match the last one."""
    results: list[dict] = []
    damaged = 0
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                # A run_header is skipped explicitly rather than by merely
                # lacking a "status" key - the header schema is free to grow a
                # "status"-named field of its own later without silently
                # turning this into a bug.
                if entry.get("record") == "run_header":
                    continue
                if entry.get("live") != live:
                    continue
                # A record naming no identifier cannot answer either caller's
                # question, and is damage of the same kind as a line that will
                # not parse.
                if not entry.get("identifier"):
                    damaged += 1
                    continue
                results.append(entry)
            except (json.JSONDecodeError, AttributeError):
                # AttributeError covers a line that parses as valid JSON of the
                # wrong shape entirely - a bare list or string has no .get().
                damaged += 1

    if damaged:
        print(
            f"{_pluralize(damaged, 'line')} in {log_path} could not be read and were skipped - "
            "the log is damaged, most likely truncated by a run that was killed mid-write.",
            file=sys.stderr,
        )
    return results


def load_prior_successes(log_path: str | Path, live: bool) -> set[str]:
    """Identifiers logged as success/unchanged in the same mode as this run,
    for --resume-from to skip. See _read_log_results() for the mode filter and
    the damaged-line handling."""
    return {
        entry["identifier"]
        for entry in _read_log_results(log_path, live)
        if entry.get("status") in ("success", "unchanged")
    }


def load_uploaded_as(log_path: str | Path, live: bool) -> dict[str, str]:
    """Real identifier -> the identifier that run actually sent to Internet
    Archive.

    In test mode the two differ: effective_identifier() prepends
    `zztest-<that run's stamp>-`, and the stamp is unique per invocation (see
    run_stamp()). Any later command that re-derives the target with its OWN
    stamp therefore names an item that has never existed - which is precisely
    what made test-mode sync-metadata impossible before this existed. The log's
    `uploaded_as` field is the only record of which stamped item a row's files
    actually went to, so it is read rather than recomputed.

    Entries with no `uploaded_as` are skipped rather than counted as damage:
    upload_from_csv writes a "carried over from resumed log" success record per
    skipped identifier, and those legitimately name no target.

    Later entries win, which is what a resumed run needs: a row re-uploaded
    under a second stamp is on Internet Archive under the LATER one, and that
    is the item to correct."""
    return {
        entry["identifier"]: entry["uploaded_as"]
        for entry in _read_log_results(log_path, live)
        if entry.get("status") in ("success", "unchanged") and entry.get("uploaded_as")
    }


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
    project_id: str,
    skip_identifiers: frozenset[str] = frozenset(),
) -> list[RowValidation]:
    """project_id is the run's own --project - see check_identifier for why
    it is required. It matters more here than anywhere else: sync-metadata
    writes metadata to whatever identifier the row names, so a wrong-project
    identifier does not merely misfile this project's item, it overwrites
    another project's."""
    seen_identifiers: dict[str, int] = {}
    results: list[RowValidation] = []

    for offset, row in enumerate(rows):
        row_number = offset + 2
        identifier = (row.get("identifier") or "").strip()

        if identifier in skip_identifiers:
            seen_identifiers.setdefault(identifier, row_number)
            results.append(RowValidation(row_number=row_number, identifier=identifier))
            continue

        errors = check_identifier(identifier, row_number, registry, project_id, seen_identifiers)
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


@dataclass(frozen=True)
class FileSurvey:
    """What the Sheet says versus what is on the drive.

    `claimed` is built before any matching runs, and `unclaimed` is its
    complement. That ordering is the whole safety property: a file another
    row already resolves to can never be proposed to a second row, which is
    the misattribution hazard in issue #1.

    `unresolved` holds only rows that ASSERTED a filename and were wrong.
    Rows whose file_template cells are blank are counted in `not_ready` and
    nowhere else - see survey_files()."""

    unresolved: dict[int, str]
    wanted: dict[int, str]
    claimed: set[str]
    unclaimed: dict[str, list[str]]
    not_ready: list[int]


def claim_key(folder_and_name: str) -> str:
    """The one spelling of "some row has already taken this file".

    `claimed` holds `<folder cell as typed>/<real disk name>`: the name half
    comes back from resolve_file() exactly as it is on disk, but the folder
    half is whatever the Sheet cell said. On Windows - a case-insensitive
    filesystem - `SOP CD 1` and `sop cd 1` are ONE folder holding ONE
    photograph, so keyed by the raw cell those two rows get two disjoint
    namespaces and the check that stops two rows being pointed at one file
    silently misses. os.path.normcase folds that difference on Windows and
    nothing on Linux, where two such folders really are two folders - but
    it goes by platform CONVENTION, not the actual filesystem, and on macOS
    the convention (identity) disagrees with the default filesystem (APFS
    is case-insensitive too), so the fold is done here instead. On the rare
    case-sensitive Mac volume that fold merely withholds a same-name-
    different-case file from auto-proposal - erring on the quiet side,
    where the un-folded key errs by letting two rows take one file.

    Every producer and every consumer of `claimed` goes through here: a set
    half of whose members are normalized is worse than one that is not
    normalized at all."""
    if sys.platform == "darwin":
        return folder_and_name.casefold()
    return os.path.normcase(folder_and_name)


def survey_files(rows: list[dict[str, str]], config: ProjectConfig) -> FileSurvey:
    """Resolve every row, then work out which files nothing points at.

    A row with a blank file_template cell is NOT unresolved - it is
    not-ready, the same split resolve_sheet_files() draws between `errors`
    and `blank`. Nobody asserted a file, so there is nothing to be wrong and
    nothing a proposal could be matched against. Filing those as unresolved
    made reconcile-files raise one dead prompt per uncatalogued row: on the
    real Sheet that is ~2,900 prompts with no proposal and no candidates,
    burying the ~150-300 genuinely fixable rows - the exact failure
    docs/decisions/READINESS.md exists to prevent. They are counted in
    `not_ready` so the run can say how many it passed over in one line.

    Deliberately does not mutate rows, unlike resolve_sheet_files(): this runs
    before any decision is made, and a row's cells must still read as the
    operator wrote them when they are shown one."""
    listing_cache: dict[Path, list[str]] = {}
    fields = template_fields(config.file_template)
    unresolved: dict[int, str] = {}
    wanted: dict[int, str] = {}
    claimed: set[str] = set()
    folders: set[str] = set()
    not_ready: list[int] = []

    for offset, row in enumerate(rows):
        row_number = offset + 2
        folder = (row.get(fields[0]) or "").strip() if fields else ""
        name_field = fields[-1] if fields else ""

        blank = [name for name in fields if not (row.get(name) or "").strip()]
        if blank:
            not_ready.append(row_number)
            continue

        # After the blank check: a folder only an uncatalogued row names has
        # no row that could be prompted about it, so listing it is a disk
        # scan whose result nothing reads.
        folders.add(folder)
        try:
            claimed.add(
                claim_key(
                    resolve_file(
                        config.files_dir,
                        candidate_path(config.file_template, row),
                        listing_cache,
                    )
                )
            )
        except FileResolutionError:
            unresolved[row_number] = folder
            wanted[row_number] = (row.get(name_field) or "").strip()

    unclaimed: dict[str, list[str]] = {}
    for folder in sorted(f for f in folders if f):
        directory = Path(config.files_dir) / folder
        if not directory.is_dir():
            continue
        unclaimed[folder] = sorted(
            entry.name
            for entry in directory.iterdir()
            if entry.is_file()
            and entry.suffix.lower() in config.photo_extensions
            and claim_key(f"{folder}/{entry.name}") not in claimed
        )
    return FileSurvey(
        unresolved=unresolved,
        wanted=wanted,
        claimed=claimed,
        unclaimed=unclaimed,
        not_ready=not_ready,
    )


@dataclass(frozen=True)
class Decision:
    action: str          # "accept" | "reject" | "stop"
    filename: str        # the RESOLVED name, empty unless accepting
    # How the operator answered, not what they answered. [y] accepts the
    # proposal; [e] is a name they typed - which resolve_file() may well
    # resolve to the proposed file anyway. Comparing the two strings cannot
    # tell those apart, and the decision log has to.
    typed: bool = False


def prompt_for_decision(
    row_number: int,
    folder: str,
    wanted: str,
    proposal: Proposal | None,
    unclaimed: list[str],
    config: ProjectConfig,
    claimed: set[str],
    read_line=input,
) -> Decision:
    """Ask about one row and return what the operator decided.

    `read_line` is injected so tests drive this without a terminal.

    A typed name goes through the same resolve_file() every other path uses,
    so it must resolve to exactly one real file AND that file must not
    already be claimed. That is what keeps typing from becoming a new way to
    introduce the error being fixed - and it means a name typed without its
    extension, or in the wrong case, resolves anyway."""
    shown = wanted or "(blank)"
    print(f"row {row_number}  '{shown}'  does not resolve in '{folder}'")
    keys = "[y] accept   " if proposal else ""
    if proposal:
        print(f"       proposed: '{proposal.filename}'   ({proposal.reason})")
    # Reprinted on every path that lands the operator back at '>' from
    # somewhere else - after [l]'s listing or a failed [e] the keys have
    # scrolled away, and a bare '>' does not say which prompt this is.
    keys_line = f"       {keys}[n] not this one   [e] type it   [l] list unclaimed   [q] stop"
    print(keys_line)

    while True:
        answer = read_line("       > ").strip().lower()
        if answer == "q":
            return Decision(action="stop", filename="")
        if answer == "n":
            return Decision(action="reject", filename="")
        if answer == "y" and proposal:
            return Decision(action="accept", filename=proposal.filename)
        if answer == "l":
            if unclaimed:
                for name in unclaimed:
                    print(f"           {name}")
            else:
                print(f"           nothing unclaimed in '{folder}'")
            print(keys_line)
            continue
        if answer == "e":
            typed = read_line("       filename> ").strip()
            if not typed:
                print(keys_line)
                continue
            try:
                resolved = resolve_file(config.files_dir, f"{folder}/{typed}", {})
            except FileResolutionError as exc:
                print(f"           {exc}")
                print(keys_line)
                continue
            name = resolved.rpartition("/")[2]
            if claim_key(resolved) in claimed:
                print(f"           '{name}' is already used by another row")
                print(keys_line)
                continue
            return Decision(action="accept", filename=name, typed=True)
        print("           expected y, n, e, l or q")


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
    the operator, and two copies of this loop would eventually disagree.

    Rows resolving to the SAME file are all errors - every row in the group,
    not just the ones after the first, because the tool usually cannot know
    which row is the wrong one and flagging all but one silently elects a
    winner. The exception is a group holding exactly one row that has already
    uploaded: that row's identifier is permanent and its row is the only link
    between the identifier and its metadata, so it is named as the one to
    keep rather than offered for deletion alongside the others. Two
    rows claiming one photograph would mint two permanent identifiers for it,
    and - issue #1 - identical file_template cells are identical fingerprints,
    which is exactly when the mid-run-edit guard stops being able to tell the
    rows apart. Keyed on claim_key() of the RESOLVED path, not the raw cells:
    the resolver is deliberately forgiving (case, extension), so two rows can
    spell one disk file differently, and the raw cells would miss them."""
    listing_cache: dict[Path, list[str]] = {}
    errors: dict[int, str] = {}
    blank: dict[int, list[str]] = {}
    resolved_rows: dict[int, str] = {}
    claims: dict[str, list[int]] = {}
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
        resolved_rows[row_number] = resolved
        claims.setdefault(claim_key(resolved), []).append(row_number)

    # Second pass, because the FIRST row of a duplicate group is already
    # resolved and recorded by the time the second one reveals the conflict.
    for claimants in claims.values():
        if len(claimants) < 2:
            continue
        # "The tool cannot know which row is the wrong one" stops being true
        # the moment exactly one claimant has already uploaded: its identifier
        # is permanent, and its Sheet row is the only thing tying that
        # identifier to its metadata - including the ia_url `sync-metadata`
        # reads to find its targets. Left symmetrical, the message invited
        # deleting precisely that row. So the DONE claimant is named as the
        # one to keep, and only the others are offered for deletion. Two DONE
        # claimants is a worse problem than this function can adjudicate (one
        # photograph already holds two permanent identifiers), so it falls
        # back to the symmetrical wording rather than electing a winner.
        uploaded = [n for n in claimants if classify_row(rows[n - 2]) is RowState.DONE]
        keeper = uploaded[0] if len(uploaded) == 1 else None
        for row_number in claimants:
            others = [n for n in claimants if n != row_number]
            label = "row" if len(others) == 1 else "rows"
            listed = ", ".join(str(n) for n in others)
            if keeper == row_number:
                remedy = (
                    f"this row has already uploaded, so it is the one to keep - fix or "
                    f"delete {label} {listed} instead"
                )
            elif keeper is not None:
                remedy = (
                    f"row {keeper} has already uploaded and must be kept, so delete this "
                    "row, or point it at the right file"
                )
            else:
                remedy = "delete the duplicate row, or point it at the right file"
            errors[row_number] = (
                f"resolves to '{resolved_rows[row_number]}' - the same file as {label} "
                f"{listed}. Two rows cannot claim one photograph: {remedy}"
            )
            # The same invariant as the failure paths above: a row filed in
            # `errors` keeps no 'file' (or resolved bib) that a later disk
            # check or an upload could coincidentally use.
            row = rows[row_number - 2]
            row["file"] = ""
            row[IA_IDENTIFIER_BIB_COLUMN] = ""

    return FileOutcomes(errors=errors, blank=blank)


def split_structure_results(
    structure_results: list[RowValidation], row_count: int
) -> tuple[list[RowValidation], list[RowValidation]]:
    """Split sheet_structure_validation()'s output into (header-level,
    per-data-row) - the two halves every Sheet command treats differently: a
    bad header stops the whole run, a bad row is skipped.

    check_grid_shape and validate_rows both number rows `offset + 2`, so
    `row_number - 2` indexes the data rows exactly. The bounds check is
    load-bearing, not defensive: a bare `row_number - 2` would quietly fold
    row 1 - where check_column_map's header defects are filed - into the LAST
    data row via negative indexing."""
    header_level: list[RowValidation] = []
    per_row: list[RowValidation] = []
    for entry in structure_results:
        index = entry.row_number - 2
        (per_row if 0 <= index < row_count else header_level).append(entry)
    return header_level, per_row


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
        rows,
        config.files_dir,
        registry,
        config.project_id,
        required_for_upload=config.required_for_upload,
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

    header_results, row_structure_results = split_structure_results(
        structure_results, len(row_results)
    )
    for entry in row_structure_results:
        # structural errors first: a long row's mis-attributed values are
        # the likely cause of whatever content errors follow.
        row_result = row_results[entry.row_number - 2]
        row_result.errors = entry.errors + row_result.errors

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


class SheetSetupFailed(Exception):
    """A Sheet-path command could not get far enough to start its own work.

    Carries no message of its own - the operator-facing one is already on
    stderr by the time this is raised. Raising rather than returning a
    sentinel is what lets read_sheet() and validate_sheet_content() have a
    single return type, so a caller cannot forget to check one."""


@dataclass(frozen=True)
class SheetRead:
    """A project's Sheet, read and far enough along that a command can start.

    Holds the raw `grid` as well as `rows`: sheet_structure_validation needs
    the grid to see a data row longer than the header, which grid_to_rows has
    already truncated away by the time it produces rows. `client` is kept
    because `upload` writes back through the same one it read with."""

    registry: dict
    config: ProjectConfig
    live: bool
    client: SheetClient
    grid: list[list[str]]
    column_map: ColumnMap
    rows: list[dict[str, str]]
    structure_results: list[RowValidation]


def sheet_banner(config: ProjectConfig, live: bool) -> str:
    """The line every Sheet-path command prints before anything else.

    The run mode is this project's core safety design - a rehearsal must
    never touch the real Sheet - so which spreadsheet and tab back it is
    printed unconditionally, not just on success. A human staring at a report
    has to be able to confirm at a glance that they are pointed where they
    think they are."""
    mode = "live" if live else "test"
    return (
        f"project '{config.project_id}': {mode} mode, "
        f"spreadsheet '{config.sheet_id_for(live)}', tab '{config.sheet_tab}'"
    )


def read_sheet(args, registry: dict, config: ProjectConfig, live: bool, command: str) -> SheetRead:
    """Everything all three Sheet-path commands do between printing their
    banner and starting their own work.

    Deliberately does NOT print the banner or run the per-command flag
    checks. Those happen first and differ per command - `upload` rejects
    --collection and validates --limit, `sync-metadata` rejects --from-log -
    and moving the placeholder check ahead of them would change which
    complaint an operator sees when both are wrong.

    `command` appears in the placeholder message only ("before running
    upload"), which is the sole text that differed between the three copies
    this replaces."""
    sheet_id = config.sheet_id_for(live)
    mode = "live" if live else "test"

    if sheet_id.startswith(PLACEHOLDER_SHEET_ID_PREFIX):
        print(
            f"the {mode}-mode spreadsheet ID for project '{config.project_id}' is still the "
            f"placeholder '{sheet_id}' - edit it in {args.registry} to the real Google Sheet ID "
            f"before running {command}.",
            file=sys.stderr,
        )
        raise SheetSetupFailed

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
        raise SheetSetupFailed from exc

    column_map, rows = grid_to_rows(grid)

    # mediatype is a per-project constant, never a Sheet column - inject it
    # before validating so every row satisfies the required-column check
    # instead of failing on a column that was never meant to exist. Harmless
    # for sync-metadata, which never sends it: sheet_metadata_fields()
    # subtracts PIPELINE_OWNED_FIELDS, and Internet Archive will not change
    # an item's mediatype after upload anyway.
    for row in rows:
        row["mediatype"] = config.mediatype

    structure_results = sheet_structure_validation(column_map, grid)

    if not rows:
        # A dedicated branch, not just another row-1 structural error: an
        # empty read is far more likely to mean a wrong tab name, an
        # unpopulated copy of the Sheet, or a Sheet never actually shared
        # with the account you authorized as than a real project with zero
        # rows, and reporting that as success would defeat the purpose of
        # running the command at all. Handled separately from the normal
        # report (rather than folded into sheet_structure_validation's row-1
        # entry) specifically so the summary line never has to say
        # "0/1 rows passed" - that "1" would be a synthetic entry standing in
        # for zero real rows, which reads as nonsense arithmetic.
        if structure_results:
            print("\n".join(_format_result_lines(structure_results)))
            print()
        print(
            "the Sheet has no data rows (only a header, or nothing at all) - check that "
            "'sheet_tab' in the project's registry entry names the right tab, and that "
            "the Sheet has actually been populated and shared"
        )
        raise SheetSetupFailed

    return SheetRead(
        registry=registry,
        config=config,
        live=live,
        client=client,
        grid=grid,
        column_map=column_map,
        rows=rows,
        structure_results=structure_results,
    )


def validate_sheet_content(
    sheet: SheetRead, args
) -> tuple[list[RowValidation], list[RowValidation]]:
    """The registry-vs-Sheet checks and per-row validation `validate` and
    `upload` share. Returns (header_results, row_results).

    `sync-metadata` does not use this: it corrects rows that already
    uploaded, so a file_template that no longer matches the Sheet is not its
    problem, and re-resolving files would make a correction depend on the
    drive being attached.

    Callers that need row fingerprints (upload, for the mid-run-edit guard)
    must take them BEFORE calling this - resolve_sheet_files rewrites
    row['file'] to the resolved name, and the fingerprint has to be the raw
    cell to compare against a fresh read."""
    # A file_template naming a column the Sheet's header row doesn't have (a
    # registry typo, or a Sheet whose columns changed) is checked once, here,
    # rather than surfacing as the same resolution failure repeated on every
    # row. Deliberately after read_sheet's no-rows branch: an empty or
    # header-only Sheet already gets a more useful diagnostic.
    try:
        check_file_template(sheet.config.file_template, sheet.column_map)
    except TemplateError as exc:
        print(
            f"project '{sheet.config.project_id}': {exc} - fix 'file_template' in "
            f"{args.registry}",
            file=sys.stderr,
        )
        raise SheetSetupFailed from exc

    config_errors = check_required_for_upload(sheet.config, sheet.column_map)
    if config_errors:
        print("\n".join(config_errors), file=sys.stderr)
        print(
            f"fix required_for_upload in {args.registry} - as written, every row would "
            "be reported as not yet catalogued and nothing would ever upload",
            file=sys.stderr,
        )
        raise SheetSetupFailed

    # See docs/decisions/FILES-AND-METADATA.md, "A file is found by
    # resolution, not by constructing a path". Both commands run the
    # identical two steps, so the value `upload` records in
    # ia_identifier_bib is the one `validate` showed the operator.
    file_outcomes = resolve_sheet_files(sheet.rows, sheet.config)
    return validate_sheet_grid(
        sheet.rows, sheet.registry, sheet.config, sheet.structure_results, file_outcomes
    )


def cmd_validate(args) -> int:
    # `is not None`, not truthiness: --csv "" must be an explicit (if
    # useless) request to read a CSV named "", and fail as such, rather than
    # silently falling through to the Sheet path because an empty string is
    # falsy.
    csv_path = getattr(args, "csv", None)
    if csv_path is not None:
        data = read_csv(csv_path)
        registry = load_registry(args.registry)
        if refuse_unregistered_project(registry, args.project):
            return 1
        results = header_validation(data.fieldnames) + validate_csv_rows(
            data.rows, args.files_dir, registry, args.project
        )
        print(format_report(results))
        return 0 if all(r.is_valid for r in results) else 1

    registry = load_registry(args.registry)
    config = load_project_config(registry, args.project)
    live = bool(args.live)

    print(sheet_banner(config, live))
    print()

    try:
        sheet = read_sheet(args, registry, config, live, "validate")
        header_results, row_results = validate_sheet_content(sheet, args)
    except SheetSetupFailed:
        return 1

    column_map, rows = sheet.column_map, sheet.rows

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
    targets: dict[str, str] | None = None,
) -> dict[str, int]:
    """Shared progress/log-and-count loop for cmd_upload and
    cmd_sync_metadata - they differ only in how a row is processed, how its
    progress line reads, and what (if anything) goes in the log's file
    field. process_row(row, target_identifier) may raise MetadataUnchanged
    to count as "unchanged" rather than "failure".

    Deliberately a flat loop. This used to iterate chunk_rows(rows) and then
    the rows within each chunk, which was exactly equivalent - nothing
    happened at a chunk boundary, no pause, no batched write - while implying
    a batching guarantee this path does not have. SheetUploadRun.execute()
    chunks for a real reason (a reserve and a confirm write per chunk, and a
    re-read of the Sheet between them); there is no equivalent here, which is
    why --chunk-size is rejected on the --csv path rather than honored.

    `stamp` is computed once by the caller (run_stamp(), called once per
    command invocation) and passed in rather than computed here, so every row
    this run touches shares one stamp. See run_stamp()'s docstring for why
    that matters.

    `targets` maps a real identifier to the one to send, for a caller that
    must NOT re-derive it - sync-metadata corrects items an earlier run
    created, and in test mode those carry that run's stamp, not this one's
    (see load_uploaded_as()). When it is passed, every row is guaranteed to
    be in it: cmd_sync_metadata rejects the whole file up front otherwise,
    rather than letting a miss fall back to a recomputed target that names
    an item which has never existed."""
    total = len(rows)
    counts = {"success": 0, "unchanged": 0, "failure": 0}
    position = 0
    for row in rows:
        position += 1
        identifier = row["identifier"].strip()
        target_identifier = (
            targets[identifier] if targets is not None
            else effective_identifier(identifier, live, stamp)
        )
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
            print(f"    - {format_row_error(exc)}")
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
    `upload` never writes them. Checking `ia_identifier` instead would be
    tautological on the reserve->confirm leg, because reserve is what put that
    value there - the check would be verifying its own write.

    `reconcile-files` is the one command that DOES write a file_template
    column (the filename cell, and only that one). It never runs inside an
    upload run, so it cannot make this check verify its own write; a
    reconciliation landing in the Sheet mid-upload instead makes that row
    fingerprint as moved, so `upload` skips it and it goes out on the next
    run - the safe direction, and the same outcome as any other human edit
    to the same cell.

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
    miss a row someone else assigned a number to in the meantime.

    And the fingerprint only proves anything while exactly one row in the
    fresh read carries it (issue #1). With two rows resolving to one file, a
    shift leaves a MATCHING fingerprint at the target's position with a
    different physical row underneath, and the write lands on the wrong row -
    misattributing the item and leaving the planned row to be minted again
    next run. resolve_sheet_files() refuses duplicates present at the initial
    read, so a duplicated fingerprint here means one appeared mid-run; the
    target is filed as moved, the safe direction, and goes out on a rerun
    once the Sheet is untangled.

    That ambiguity check runs on the RESERVE leg only. After reserve, this
    run's own number is in the target's row and check_claimed_identifiers has
    already proved it unique across the whole Sheet, so the identifier
    comparison below is a complete proof of identity by itself: a shift puts a
    row that does NOT carry our number underneath. Vetoing a duplicated
    fingerprint there would withhold the confirm write for an edit that cannot
    have moved anything - an appended duplicate row shifts nothing - and the
    cost of that false positive is the worst outcome this tool has short of a
    wrong write: an item live on Internet Archive with no record in the
    Sheet."""
    duplicated: set[str] = set()
    if not reserved_already:
        seen: set[str] = set()
        for fingerprint in snapshot.fingerprints.values():
            if fingerprint in seen:
                duplicated.add(fingerprint)
            seen.add(fingerprint)

    still_there: list[UploadTarget] = []
    moved: list[UploadTarget] = []
    for target in targets:
        expected_identifier = (
            target.identifier if reserved_already or not target.newly_minted else ""
        )
        fingerprint_now = snapshot.fingerprints.get(target.row_number, "")
        matches = (
            bool(fingerprint_now)
            and fingerprint_now not in duplicated
            and fingerprint_now == target.source_fingerprint
            and cell_value(snapshot.grid, target.row_number, snapshot.columns.ia_identifier)
            == expected_identifier
        )
        if matches:
            still_there.append(target)
        else:
            moved.append(target)
    return still_there, moved


def sheet_metadata_fields(column_map: ColumnMap) -> frozenset[str]:
    """The normalized column names whose values this tool sends to Internet
    Archive as item metadata.

    One definition, shared by `upload` and `sync-metadata`, so the two cannot
    disagree about what a row means - a column that uploads but does not sync
    (or the reverse) would leave the Sheet and the item permanently out of
    step in a way neither command reports.

    Subtracts PIPELINE_OWNED_FIELDS as well as DROPPED_BY_UPLOAD_ROW.
    `mediatype` and `collection` are generated, and upload overwrites them
    anyway, so excluding them here changes nothing for upload - but for sync
    it matters twice over: Internet Archive will not change an item's
    mediatype after upload, and `collection` is membership, not metadata."""
    return (
        frozenset(column_map.uploadable_fields())
        - DROPPED_BY_UPLOAD_ROW
        - PIPELINE_OWNED_FIELDS
    )


def identifier_from_url(url: str) -> str | None:
    """The item identifier out of an `ia_url` cell, or None if the cell is not
    one of this tool's own URLs.

    `ia_url` is what upload's confirm write recorded, so in test mode it
    already carries THAT run's stamp. This is why the Sheet path needs no
    equivalent of the --csv path's --from-log: the Sheet is its own record of
    what landed where. Returns None rather than guessing at an unrecognised
    cell - a human having pasted something is far likelier than the URL prefix
    having changed."""
    url = url.strip()
    if not url.startswith(ITEM_URL_PREFIX):
        return None
    return url[len(ITEM_URL_PREFIX):].strip("/") or None


def item_project_id(uploaded_as: str, live: bool) -> str | None:
    """The PROJECTID of the item `uploaded_as` names, or None if it cannot be
    read as one of this tool's identifiers.

    A test item is `zztest-<stamp>-<identifier>` (see effective_identifier),
    so the real identifier is whatever follows the stamp - the stamp itself is
    dropped rather than parsed, since it is only ever the run's timestamp."""
    real = uploaded_as.strip()
    if not live and real.startswith(TEST_IDENTIFIER_PREFIX):
        _stamp, _sep, real = real[len(TEST_IDENTIFIER_PREFIX):].partition("-")
    parsed = parse_identifier(real)
    return parsed[1] if parsed else None


def sheet_upload_metadata(
    target: UploadTarget, uploadable: frozenset[str], mediatype: str
) -> dict[str, str]:
    """The row dict handed to upload_row on the Sheet path.

    upload_row turns every key it is given (bar `identifier` and `file`) into
    an Internet Archive metadata field, and IA metadata is permanent - so the
    tool's own bookkeeping columns and anything a Sheet author marked (LCPS
    Internal) have to be filtered out HERE, before upload_row ever sees them.
    ColumnMap.uploadable_fields() is the single definition of what may be
    uploaded and already excludes both. It is passed in already computed
    (see SheetUploadRun.uploadable) rather than derived here: the column map
    is fixed for the whole run, and rebuilding the set per row made a
    10,000-row upload rebuild it 10,000 times.

    `identifier-bib` and `mediatype` are generated rather than read from a
    column - see docs/DECISIONS.md, "`identifier-bib` and `mediatype` are
    generated, not columns". The surviving test item
    zztest-lcps-sarahsoldphotos-00005 carries a permanently misspelled
    `indentifier-bib` because a header typo shipped once; a generated field
    name cannot do that."""
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

    @functools.cached_property
    def uploadable(self) -> frozenset[str]:
        """Which normalized field names may be sent as IA metadata.

        Computed once per run, not once per row. The column map is fixed for
        the whole run - it is a field on this dataclass - so deriving this
        inside sheet_upload_metadata() meant a 10,000-row upload rebuilding
        the same set 10,000 times. cached_property works on a frozen
        dataclass because it writes through __dict__ rather than
        __setattr__."""
        return sheet_metadata_fields(self.column_map)

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
        target. Every row this run already
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
                        sheet_upload_metadata(target, self.uploadable, self.mediatype),
                        target.uploaded_as,
                        self.collection,
                        self.files_dir,
                    )
                except Exception as exc:
                    # A rate limit is logged as an ordinary failure - the
                    # message is the server's either way - and additionally
                    # ends the run after this chunk's confirm write.
                    counts["failure"] += 1
                    print(f"    - {format_row_error(exc)}")
                    self._log(target, "failure", error=str(exc))
                    if is_rate_limit_error(exc):
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


REMOVE_TAG_SENTINEL = "REMOVE_TAG"
# How much of a field value a dry run prints before eliding. Long enough for a
# typo to be visible in place, short enough that one changed field stays one
# readable pair of lines.
DRY_RUN_VALUE_WIDTH = 96


def fetch_current_metadata(identifier: str) -> dict | None:
    """The metadata Internet Archive currently holds for an item, or None if
    it could not be read.

    Its own function so a test can pin it, and so the dry run's one network
    dependency is in a single place. Returns None rather than raising: a dry
    run that cannot reach one item should still report the other 9,999.
    """
    try:
        return dict(internetarchive.get_item(identifier).metadata)
    except Exception:
        return None


def _render(value: object) -> str:
    """IA returns a list for a field that occurs more than once."""
    if isinstance(value, list):
        value = "; ".join(str(part) for part in value)
    text = str(value)
    if len(text) > DRY_RUN_VALUE_WIDTH:
        # ASCII, deliberately: this report is read on a Windows console whose
        # codepage cannot encode U+2026, where one non-ASCII character raises
        # UnicodeEncodeError and truncates the whole report mid-run.
        text = text[:DRY_RUN_VALUE_WIDTH - 3] + "..."
    return text


def metadata_changes(
    sheet_metadata: dict[str, str], remote: dict
) -> list[tuple[str, str, str]]:
    """(field, what IA holds now, what the Sheet would make it), for the
    fields a sync would actually alter.

    Mirrors update_metadata_row's rules exactly, because a dry run that
    predicts something other than what the real run does is worse than no dry
    run. A blank cell is dropped there, so it means "leave this field alone"
    and is not a change here. REMOVE_TAG deletes there, so it shows as a
    deletion here - and only when the field actually exists on the item, since
    removing what is not present changes nothing."""
    changes: list[tuple[str, str, str]] = []
    for field_name, raw in sorted(sheet_metadata.items()):
        value = (raw or "").strip()
        if not value:
            continue
        current = remote.get(field_name)
        if value == REMOVE_TAG_SENTINEL:
            if current is not None:
                changes.append((field_name, _render(current), "(deleted)"))
            continue
        if current is None:
            changes.append((field_name, "(not set)", _render(value)))
        elif _render(current) != _render(value):
            changes.append((field_name, _render(current), _render(value)))
    return changes


def print_sync_dry_run(targets: list[SyncTarget], problems: list[RowValidation]) -> int:
    """Shows what a sync would CHANGE, not merely which fields it would send.

    Listing field names alone made the dry run unable to answer the one
    question it is run to answer - "did my edit get picked up?" - because
    editing a description on a row that already had one produced byte-
    identical output. So each item's current metadata is read back from
    Internet Archive and diffed against the Sheet.

    That costs one read per item. It is the right trade for a command whose
    other mode writes to permanent public items, and it makes the run's
    `unchanged` count visible BEFORE anything is sent rather than after."""
    print(f"reading current metadata for {_pluralize(len(targets), 'item')}...")
    print()

    changed = 0
    unreadable = 0
    for target in targets:
        remote = fetch_current_metadata(target.uploaded_as)
        if remote is None:
            unreadable += 1
            print(
                f"  row {target.row_number}: {target.uploaded_as} - could not read its "
                "current metadata, so what would change is unknown"
            )
            continue

        changes = metadata_changes(target.metadata, remote)
        if not changes:
            continue

        changed += 1
        print(f"  row {target.row_number}: {target.uploaded_as}")
        for field_name, current, new in changes:
            print(f"      {field_name}")
            print(f"          now: {current}")
            print(f"          new: {new}")

    if changed or unreadable:
        print()
    unchanged = len(targets) - changed - unreadable
    print(
        f"{changed} of {_pluralize(len(targets), 'item')} would change; "
        f"{unchanged} already match and would be reported as unchanged"
    )
    if unreadable:
        print(f"{_pluralize(unreadable, 'item')} could not be read")
    return 1 if problems else 0


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
    if refuse_unregistered_project(registry, args.project):
        return 1

    skip_identifiers: set[str] = set()
    if args.resume_from:
        skip_identifiers = load_prior_successes(args.resume_from, args.live)

    to_upload = [row for row in rows if (row.get("identifier") or "").strip() not in skip_identifiers]

    validation_results = header_validation(data.fieldnames) + validate_csv_rows(
        rows, files_dir, registry, args.project, frozenset(skip_identifiers)
    )
    if not all(r.is_valid for r in validation_results):
        print(format_report(validation_results))
        print(
            "validation failed; run 'validate' and fix the errors above before uploading",
            file=sys.stderr,
        )
        return 1

    # The CSV path has no --limit to trim with (it is rejected above), so the
    # fix here is to split the file - but the cap is Internet Archive's and
    # applies to this path just as much, and relying on the operator to
    # remember it is what left it unenforced. Counted after --resume-from
    # filtering: rows a prior run already uploaded do not spend today's quota.
    if len(to_upload) > DAILY_ITEM_CAP and not getattr(args, "allow_over_daily_cap", False):
        print(
            f"this CSV would upload {len(to_upload)} items, over Internet Archive's "
            f"{DAILY_ITEM_CAP}/day cap for the account. Split it into files of "
            f"{DAILY_ITEM_CAP} rows or fewer and run them on separate days. Pass "
            "--allow-over-daily-cap to override if you know this account's cap has been "
            "raised.",
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
    print(sheet_banner(config, live))
    if dry_run:
        print("--dry-run: nothing is uploaded, and nothing is written to the Sheet")
    elif write_back:
        print(f"results WILL be written back to spreadsheet '{config.sheet_id_for(live)}'")
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

    try:
        sheet = read_sheet(args, registry, config, live, "upload")
    except SheetSetupFailed:
        return 1

    client, column_map, rows = sheet.client, sheet.column_map, sheet.rows

    try:
        columns = locate_write_back_columns(column_map)
    except MissingWriteBackColumns as exc:
        print(f"project '{config.project_id}': {exc}", file=sys.stderr)
        return 1

    # Fingerprints come from the RAW cells, so they must be taken before
    # resolve_sheet_files() rewrites row['file'] to the resolved name. They are
    # what every later mid-run-edit check compares against.
    source_fingerprints = sheet_row_fingerprints(rows, config.file_template)

    try:
        header_results, row_results = validate_sheet_content(sheet, args)
    except SheetSetupFailed:
        return 1

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
                f" ({len(not_ready_broken):,} of them also {verb} an unresolvable "
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

    # Checked AFTER --limit, so --limit is the ordinary way to satisfy it: a
    # Sheet with 9,000 ready rows is not an error, running at all 9,000 of
    # them in one day is. Refuses rather than silently capping - a run that
    # quietly stops short reads as a complete one, which is the same trap the
    # --limit <= 0 guard above exists to avoid.
    if len(targets) > DAILY_ITEM_CAP and not getattr(args, "allow_over_daily_cap", False):
        print(
            f"this run would upload {len(targets)} items, over Internet Archive's "
            f"{DAILY_ITEM_CAP}/day cap for the account. Pass --limit {DAILY_ITEM_CAP} (or "
            "less) and run again tomorrow for the rest; identifiers are minted fresh each "
            "run, so nothing is lost by splitting it. Pass --allow-over-daily-cap to "
            "override if you know this account's cap has been raised.",
            file=sys.stderr,
        )
        return 1

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


@dataclass(frozen=True)
class SyncTarget:
    """One already-uploaded row whose Sheet metadata this run will push to
    Internet Archive."""

    row_number: int
    identifier: str        # the real, permanent one, from ia_identifier
    uploaded_as: str       # the item to actually send to, from ia_url
    metadata: dict[str, str]


def plan_sync_targets(
    rows: list[dict[str, str]], column_map: ColumnMap, live: bool, project_id: str
) -> tuple[list[SyncTarget], list[RowValidation]]:
    """Decides which rows this run will correct, and what it will send.

    project_id is the run's own --project, required here for the same reason
    check_identifier requires it (issue #2). This path targets whatever item
    `ia_url` names, so a cell pointing at another project's item does not
    merely misfile this project's row - it overwrites that project's
    metadata. The --csv path gets this check from validate_identifiers; the
    Sheet path never runs that, so it is made here.

    Scope is RowState.DONE and nothing else. An UNASSIGNED row has no item to
    correct, and a RESERVED row's upload never confirmed - correcting metadata
    on an item that may not exist is a different problem, and `upload` already
    retries those under their existing identifier.

    Every DONE row is sent, not only rows that look changed. Internet Archive
    answers "no changes to _meta.xml" for an item that already matches, which
    update_metadata_row turns into MetadataUnchanged and the runner counts as
    `unchanged` rather than an error - so a full sync is idempotent by
    construction and needs no per-row change detection, no extra tool-owned
    column, and no second place for the Sheet and the item to drift apart.

    Blank cells are dropped by update_metadata_row, so a cleared cell means
    "leave this field alone" and REMOVE_TAG deletes - identical to the --csv
    path. A Sheet cell cleared by accident can therefore never strip metadata
    from a permanent public item.

    Returns (targets, problems). A DONE row this run cannot safely target is a
    problem rather than a silent skip: the operator edited it expecting the
    edit to reach the site."""
    fields = sheet_metadata_fields(column_map)
    targets: list[SyncTarget] = []
    problems: list[RowValidation] = []

    for offset, row in enumerate(rows):
        row_number = offset + 2
        if classify_row(row) is not RowState.DONE:
            continue

        identifier = (row.get(IA_IDENTIFIER_COLUMN) or "").strip()
        uploaded_as = identifier_from_url(row.get(IA_URL_COLUMN) or "")

        if uploaded_as is None:
            problems.append(
                RowValidation(
                    row_number=row_number,
                    identifier=identifier,
                    errors=[
                        f"row is marked uploaded but its '{IA_URL_COLUMN}' cell is blank or "
                        "does not look like an Internet Archive item URL, so there is no "
                        "item to correct - restore it from the upload log, or clear "
                        f"'{IA_UPLOADED_COLUMN}' to have `upload` do the row again"
                    ],
                )
            )
            continue

        # The Sheet is the mode boundary (test and live are different
        # spreadsheets), so a mismatch here means the wrong Sheet is in the
        # registry or someone pasted a URL across - either way, sending a
        # live correction to a test item, or the reverse, is not recoverable
        # by rerunning.
        is_test_item = uploaded_as.startswith(TEST_IDENTIFIER_PREFIX)
        if live and is_test_item:
            problems.append(
                RowValidation(
                    row_number=row_number,
                    identifier=identifier,
                    errors=[
                        f"--live, but '{IA_URL_COLUMN}' points at the test item "
                        f"'{uploaded_as}'. Refusing to send a live correction to a "
                        "rehearsal item"
                    ],
                )
            )
            continue
        if not live and not is_test_item:
            problems.append(
                RowValidation(
                    row_number=row_number,
                    identifier=identifier,
                    errors=[
                        f"test mode, but '{IA_URL_COLUMN}' points at the real item "
                        f"'{uploaded_as}'. Refusing to send a rehearsal correction to a "
                        "permanent item - pass --live if that is what you meant"
                    ],
                )
            )
            continue

        if item_project_id(uploaded_as, live) != project_id:
            problems.append(
                RowValidation(
                    row_number=row_number,
                    identifier=identifier,
                    errors=[
                        f"'{IA_URL_COLUMN}' points at item '{uploaded_as}', which does not "
                        f"belong to this run's --project {project_id}. Refusing to send this "
                        "project's metadata to another project's item"
                    ],
                )
            )
            continue

        targets.append(
            SyncTarget(
                row_number=row_number,
                identifier=identifier,
                uploaded_as=uploaded_as,
                metadata={key: value for key, value in row.items() if key in fields},
            )
        )

    return targets, problems


def run_sheet_sync(targets: list[SyncTarget], log_path: Path, live: bool) -> dict[str, int]:
    """Its own loop rather than run_rows(): that helper keys everything off
    `row["identifier"]`, and on the Sheet path that column holds the DONOR's
    archival reference, not this tool's identifier. Reusing it would have
    meant writing the tool's identifier into a column that means something
    else."""
    counts = {"success": 0, "unchanged": 0, "failure": 0}
    total = len(targets)
    for position, target in enumerate(targets, start=1):
        print(f"[{position}/{total}] updating metadata for {target.uploaded_as}")
        try:
            update_metadata_row(target.metadata, target.uploaded_as)
        except MetadataUnchanged:
            counts["unchanged"] += 1
            log_result(log_path, target.identifier, "", "unchanged", live, uploaded_as=target.uploaded_as)
        except Exception as exc:
            counts["failure"] += 1
            print(f"    - {format_row_error(exc)}")
            log_result(
                log_path, target.identifier, "", "failure", live,
                error=str(exc), uploaded_as=target.uploaded_as,
            )
        else:
            counts["success"] += 1
            log_result(log_path, target.identifier, "", "success", live, uploaded_as=target.uploaded_as)
    return counts


def check_uploaded_as(
    rows: list[dict[str, str]],
    targets: dict[str, str],
    skip_identifiers: frozenset[str],
    log_path: str,
) -> list[RowValidation]:
    """Rows whose identifier the --from-log upload log does not record as
    having been uploaded.

    A miss is an error, never a fall back to recomputing the target. In test
    mode recomputing is the exact bug --from-log exists to fix, and it fails
    SILENTLY: modify_metadata() is sent to a stamped identifier that has never
    existed, so the run reports failures whose message says nothing about the
    real cause. Reported per row, before anything is sent, so a wrong CSV is
    fixed in one pass.

    A blank identifier is skipped here - validate_identifiers already reports
    it as a missing required column, and two messages for one cell reads as
    two problems."""
    results: list[RowValidation] = []
    for offset, row in enumerate(rows):
        identifier = (row.get("identifier") or "").strip()
        if not identifier or identifier in skip_identifiers or identifier in targets:
            continue
        results.append(
            RowValidation(
                row_number=offset + 2,
                identifier=identifier,
                errors=[
                    f"'{identifier}' is not recorded as uploaded in {log_path} - "
                    "sync-metadata can only correct an item an upload run actually created"
                ],
            )
        )
    return results


def cmd_sync_metadata(args) -> int:
    """`--csv` is the offline fallback, exactly as it is for validate and
    upload. Without it the Sheet is read live and IS the correction: edit a
    description in the Sheet, run this, it is on the site. That round trip is
    the whole point of the Sheet being the source of truth, and this command
    was the last one still requiring a hand-made CSV to do it."""
    csv_path = getattr(args, "csv", None)
    if csv_path is not None:
        return sync_from_csv(args)
    return sync_from_sheet(args)


def sync_from_sheet(args) -> int:
    registry = load_registry(args.registry)
    config = load_project_config(registry, args.project)

    live = bool(args.live)
    dry_run = bool(getattr(args, "dry_run", False))
    print(sheet_banner(config, live))
    if dry_run:
        print("--dry-run: nothing is sent to Internet Archive")
    print()

    for flag in ("resume_from", "from_log"):
        # Both name a prior run's LOG, which the Sheet path does not need: the
        # Sheet's own ia_uploaded/ia_url columns are the record of what was
        # uploaded and where it went.
        if getattr(args, flag, None):
            name = "--" + flag.replace("_", "-")
            print(
                f"{name} is a --csv-path flag. On the Sheet path, 'ia_uploaded' and 'ia_url' "
                "are the record of what was uploaded and which item it became, so there is "
                "no log to read.",
                file=sys.stderr,
            )
            return 1

    try:
        sheet = read_sheet(args, registry, config, live, "sync-metadata")
    except SheetSetupFailed:
        return 1

    column_map, rows = sheet.column_map, sheet.rows

    # A header defect corrupts every row's field names identically, and unlike
    # upload there is no per-row way around it.
    header_errors = check_column_map(column_map)
    if header_errors:
        print("\n".join(f"    - {error}" for error in header_errors))
        print(
            "the Sheet's header row has problems that affect every row - refusing to send "
            "metadata until they are fixed",
            file=sys.stderr,
        )
        return 1

    targets, problems = plan_sync_targets(rows, column_map, live, config.project_id)

    if problems:
        print("\n".join(_format_result_lines(problems)))
        print(
            f"{_pluralize(len(problems), 'row')} marked uploaded but not safely targetable "
            "and will be skipped; this command still exits non-zero so a partial run is "
            "never mistaken for a clean one"
        )
        print()

    print(format_field_receipt(column_map))
    print()

    if not targets:
        print("nothing to sync - no row is marked uploaded yet")
        return 1 if problems else 0

    if dry_run:
        return print_sync_dry_run(targets, problems)

    log_path = open_log(args.log_dir, "sync-metadata")
    try:
        log_run_header(log_path, config, column_map, live, dry_run)
    except Exception as exc:
        print(
            f"could not write the run-header record to {log_path}: {exc}. Continuing without "
            "it - this only affects the log's own audit trail.",
            file=sys.stderr,
        )

    counts = run_sheet_sync(targets, log_path, live)

    print(
        f"{counts['success']} item(s) updated successfully, {counts['unchanged']} unchanged, "
        f"{counts['failure']} error(s)"
    )
    if problems:
        print(f"{_pluralize(len(problems), 'row')} skipped (not safely targetable)")
    print(f"log written to {log_path}")
    return 1 if (counts["failure"] or problems) else 0


def sync_from_csv(args) -> int:
    data = read_csv(args.csv)
    rows = data.rows
    registry = load_registry(args.registry)
    if refuse_unregistered_project(registry, args.project):
        return 1
    live = bool(args.live)
    from_log = getattr(args, "from_log", None)

    # Every test item carries the stamp of the run that created it, and a
    # stamp is unique per invocation (see run_stamp()), so the CSV alone
    # cannot say which zztest- item a row's metadata belongs to. Deriving it
    # here with THIS run's stamp named an item that has never existed and
    # failed every row - a rehearsal mode that cannot rehearse. --from-log
    # names the upload run being corrected; its `uploaded_as` field is the
    # only record of that mapping. Live identifiers are unstamped, so the
    # flag is optional there.
    if not live and not from_log:
        print(
            "sync-metadata needs --from-log <upload log> in test mode. A test item is named "
            "zztest-<stamp>-<identifier>, where the stamp is unique to the run that created "
            "it, so the CSV alone cannot say which items to correct. Pass the log written by "
            "the upload run you are correcting, or pass --live to target the real, unstamped "
            "identifiers.",
            file=sys.stderr,
        )
        return 1

    targets: dict[str, str] | None = None
    if from_log:
        targets = load_uploaded_as(from_log, live)

    skip_identifiers: set[str] = set()
    if args.resume_from:
        skip_identifiers = load_prior_successes(args.resume_from, live)

    to_sync = [row for row in rows if (row.get("identifier") or "").strip() not in skip_identifiers]

    validation_results = header_validation(data.fieldnames) + validate_identifiers(
        rows, registry, args.project, frozenset(skip_identifiers)
    )
    if targets is not None:
        validation_results += check_uploaded_as(
            rows, targets, frozenset(skip_identifiers), str(from_log)
        )
    if not all(r.is_valid for r in validation_results):
        print(format_report(validation_results))
        print("identifier validation failed; fix the errors above before syncing", file=sys.stderr)
        return 1

    log_path = open_log(args.log_dir, "sync-metadata")
    for identifier in skip_identifiers:
        log_result(log_path, identifier, "", "success", live, error="carried over from resumed log")

    counts = run_rows(
        to_sync,
        log_path,
        live,
        run_stamp(),
        action="updating metadata for",
        process_row=lambda row, target: update_metadata_row(row, target),
        describe=lambda row, target: target,
        file_value_for=lambda row: "",
        targets=targets,
    )

    print(f"{counts['success']} item(s) updated successfully, {counts['unchanged']} unchanged, {counts['failure']} error(s)")
    print(f"log written to {log_path}")
    return 1 if counts["failure"] else 0


RECONCILE_FLUSH_EVERY = 25


@dataclass(frozen=True)
class PendingCorrection:
    """An accepted correction waiting to be written, carrying what its cell
    said when it was matched.

    `wanted` is the whole point of the dataclass: it is what flush() checks
    the row against on a fresh read before writing, so a Sheet edited mid
    session cannot land a filename on a photograph it was never about. Same
    role as `sheet_row_fingerprints()` for `upload`, one cell wide."""

    update: CellUpdate
    row_number: int
    wanted: str


def log_decision(log_path, row_number: int, folder: str, wanted: str, status: str,
                 chosen: str = "", reason: str = "", proposed: str = "",
                 matches: list[str] | None = None) -> None:
    """One line per row considered. Prompt-per-proposal leaves no record of
    what was decided; this is that record.

    `proposed` and `chosen` are separate on purpose. `chosen` is what was
    written, so it is empty on every path but an acceptance - and a rejected
    proposal with no record of WHAT was rejected cannot be reviewed later,
    which is half of what this log is for. `matches` does the same job for
    the ambiguous path, where the console names every candidate and the
    durable record used to name none. Every key is present on every line,
    empty where it does not apply, so a reader never has to know which
    statuses carry which fields."""
    entry = {
        "row": row_number, "folder": folder, "wanted": wanted,
        "status": status, "chosen": chosen, "proposed": proposed,
        "matches": list(matches or []), "reason": reason,
        "timestamp": utc_timestamp(),
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def cmd_reconcile_files(args) -> int:
    registry = load_registry(args.registry)
    config = load_project_config(registry, args.project)
    live = bool(args.live)
    dry_run = bool(getattr(args, "dry_run", False))

    print(sheet_banner(config, live))
    if dry_run:
        print("--dry-run: nothing is written to the Sheet")
    print()

    try:
        sheet = read_sheet(args, registry, config, live, "reconcile-files")
    except SheetSetupFailed:
        return 1

    # A bad row is skipped; a bad header stops the whole run - the same split
    # `upload` makes, for the same reason: a header defect corrupts every row
    # identically, so unlike a bad row it cannot be routed around. It is not
    # merely a missing warning here. Two headers normalizing to `file_name`
    # leave grid_to_rows reading the LAST of them (dict comprehension, last
    # key wins) while the write below targets the FIRST - so the correction
    # lands in a column nothing reads, the effective cell keeps its stale
    # value, and the run reports success. structural_rows are individual data
    # rows whose cells may be shifted relative to the header (check_grid_shape);
    # those are skipped rather than fatal.
    header_defects, structural_rows = split_structure_results(
        sheet.structure_results, len(sheet.rows)
    )
    if header_defects:
        print("\n".join(_format_result_lines(header_defects)))
        print()
        print(
            "the Sheet's header row has problems that affect every row - refusing to "
            "reconcile anything until they are fixed",
            file=sys.stderr,
        )
        return 1
    if structural_rows:
        print("\n".join(_format_result_lines(structural_rows)))
        print(
            f"{_pluralize(len(structural_rows), 'row')} above may have cells shifted "
            "against the header - skipped, since which cell a correction would land in "
            "is exactly what is in doubt"
        )
        print()

    name_field = template_fields(config.file_template)[-1]
    try:
        name_column = next(
            index for index, header in enumerate(sheet.column_map.headers)
            if sheet.column_map.field_names[header] == name_field
        )
    except StopIteration:
        print(
            f"the Sheet has no '{name_field}' column, which file_template names - "
            f"fix 'file_template' in {args.registry} or add the column.",
            file=sys.stderr,
        )
        return 1

    survey = survey_files(sheet.rows, config)
    skipped_rows = {entry.row_number for entry in structural_rows}
    to_review = [n for n in sorted(survey.unresolved) if n not in skipped_rows]
    if survey.not_ready:
        # One contained line, never one line (let alone one prompt) per row.
        # The real Sheet is ~3,000 rows of which ~2,900 carry no filename at
        # all; those are not-ready, not broken, and there is nothing an
        # operator could decide about them here. Same judgment `upload`
        # makes about the same rows - see docs/decisions/READINESS.md.
        print(f"{_pluralize(len(survey.not_ready), 'row')} not yet catalogued - "
              "no filename to reconcile, skipped")
    if not to_review:
        if survey.unresolved:
            # Every row that failed resolution was skipped above, so there is
            # nothing left to ask about - but saying "every row resolves"
            # here would be untrue.
            print("nothing left to reconcile - every row that named a file either "
                  "resolves or was skipped above")
        else:
            print("nothing to reconcile - every row with a filename resolves against the drive")
        return 0

    print(f"{_pluralize(len(to_review), 'row')} named a file that does not resolve")
    print()

    log_path = None if dry_run else open_log(args.log_dir, "reconcile-files")
    pending: list[PendingCorrection] = []
    accepted = stopped = 0

    def flush() -> bool:
        """Write what has been accepted, dropping anything whose row moved.

        This command has by far the longest read-to-write window in the tool:
        an interactive session over a Sheet several volunteers share can put
        an hour between the grid read that fixed `row_number` and the write
        that uses it, and one row inserted or deleted in that hour shifts
        every later write by one - silently putting a filename on the wrong
        photograph. `upload` already refuses to write through that window
        (sheet_row_fingerprints/read_sheet_snapshot/split_moved_targets);
        this is the same idea at a fraction of the cost, since reconcile
        knows exactly what each target cell said when it matched.

        A re-read that fails stops the run rather than writing unverified:
        the whole point is that an unchecked write here is the hazard."""
        nonlocal pending, accepted
        if not pending:
            return True
        try:
            grid = sheet.client.read_grid()
        except Exception as exc:
            print(
                f"could not re-read the Sheet to check the rows before writing: {exc}. "
                "Stopping here without writing - rerun to pick these up.",
                file=sys.stderr,
            )
            return False

        updates: list[CellUpdate] = []
        for correction in pending:
            now = cell_value(grid, correction.row_number, name_column)
            if now != correction.wanted:
                accepted -= 1
                print(f"row {correction.row_number}  the Sheet now says '{now}' where this "
                      f"run read '{correction.wanted}' - the row moved or was edited, so "
                      "the correction was NOT written")
                continue
            updates.append(correction.update)

        try:
            sheet.client.write_cells(updates)
        except Exception as exc:
            print(f"the Sheet write failed: {exc}. Stopping here.", file=sys.stderr)
            return False
        pending = []
        return True

    for row_number in to_review:
        folder = survey.unresolved[row_number]
        wanted = survey.wanted[row_number]
        # survey.unclaimed is a snapshot taken once, before any row in this
        # run was decided - it never shrinks on its own. Re-filter against
        # survey.claimed on every iteration (not just once before the loop):
        # accepting row N adds its file to `claimed` a few lines below, and
        # without this filter row N+1 in the same folder would still see
        # that same file as a candidate and could be proposed - and
        # accepted onto - it too. That is the exact misattribution FileSurvey's
        # own docstring promises cannot happen.
        candidates = [
            name for name in survey.unclaimed.get(folder, [])
            if claim_key(f"{folder}/{name}") not in survey.claimed
        ]
        try:
            proposal = propose_match(wanted, candidates)
            reason = proposal.reason if proposal else ""
        except AmbiguousMatch as exc:
            print(f"row {row_number}  '{wanted}'  matches {len(exc.matches)} files - "
                  f"leaving it alone: {', '.join(exc.matches)}")
            if log_path:
                log_decision(log_path, row_number, folder, wanted, "ambiguous",
                             matches=exc.matches)
            continue

        if dry_run:
            if proposal:
                print(f"row {row_number}  '{wanted}' -> '{proposal.filename}'  ({reason})")
            else:
                print(f"row {row_number}  '{wanted}'  no candidate in '{folder}'")
            continue

        decision = prompt_for_decision(
            row_number, folder, wanted, proposal, candidates, config, survey.claimed
        )
        if decision.action == "stop":
            stopped = 1
            if log_path:
                log_decision(log_path, row_number, folder, wanted, "stopped")
            break
        if decision.action == "reject":
            if log_path:
                log_decision(log_path, row_number, folder, wanted,
                             "rejected" if proposal else "no_candidate", reason=reason,
                             proposed=proposal.filename if proposal else "")
            continue

        accepted += 1
        survey.claimed.add(claim_key(f"{folder}/{decision.filename}"))
        pending.append(
            PendingCorrection(
                update=CellUpdate(
                    f"{column_letter(name_column)}{row_number}", decision.filename
                ),
                row_number=row_number,
                wanted=wanted,
            )
        )
        if log_path:
            # From how the operator answered, not from whether the two
            # strings happen to agree: a name typed at [e] that matches the
            # proposal is still a name a human typed, and a log that calls
            # it `accepted` claims the tool proposed something it did not.
            status = "typed" if decision.typed else "accepted"
            log_decision(log_path, row_number, folder, wanted, status,
                         chosen=decision.filename, reason=reason,
                         proposed=proposal.filename if proposal else "")
        if len(pending) >= RECONCILE_FLUSH_EVERY and not flush():
            return 1

    if not flush():
        return 1

    print()
    print(f"{accepted} filename(s) corrected")
    remaining = len(to_review) - accepted
    if remaining:
        print(f"{_pluralize(remaining, 'row')} still unresolved")
    if stopped:
        print("stopped early - rerun to pick up where this left off")
    if log_path:
        print(f"log written to {log_path}")
    return 0


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
        "--allow-over-daily-cap",
        action="store_true",
        help=(
            f"Upload more than Internet Archive's {DAILY_ITEM_CAP}/day account cap in one "
            "run. Only pass this if you know the cap has been raised for this account - "
            "otherwise the run is throttled partway through and stops mid-batch."
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
    sync_parser.add_argument(
        "--csv",
        default=None,
        help=(
            "Correct from this CSV instead of the project's Sheet. Without it the Sheet is "
            "read live and its own columns are the correction - edit a description there, "
            "run this, and it is on the site."
        ),
    )
    sync_parser.add_argument("--project", required=True, help="Project ID from the registry")
    sync_parser.add_argument("--registry", default="projects_registry.json", help="Path to the project registry JSON")
    sync_parser.add_argument("--live", action="store_true", help="Read the project's real Sheet and target the real, permanent items instead of the test Sheet and its zztest- rehearsal items")
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Send nothing; print the items that would be updated and which fields would go to each (Sheet path only)",
    )
    sync_parser.add_argument("--log-dir", default="logs", help="Directory to write the timestamped run log to")
    sync_parser.add_argument("--resume-from", default=None, help="Path to a prior log; identifiers marked success there are skipped")
    sync_parser.add_argument(
        "--from-log",
        default=None,
        help=(
            "Path to the log of the upload run whose items are being corrected. REQUIRED in "
            "test mode: a test item is named zztest-<stamp>-<identifier> where the stamp is "
            "unique to the run that created it, so the CSV alone cannot say which items to "
            "correct - this log's 'uploaded_as' field is the only record of that mapping. "
            "Optional with --live, where identifiers are unstamped. Distinct from "
            "--resume-from, which says which rows to SKIP; this says where the rows that "
            "remain should be SENT."
        ),
    )

    reconcile_parser = subparsers.add_parser(
        "reconcile-files",
        help="Find rows whose filename does not resolve against the drive and correct them",
    )
    reconcile_parser.add_argument("--project", required=True, help="Project ID from the registry")
    reconcile_parser.add_argument("--registry", default="projects_registry.json", help="Path to the project registry JSON")
    reconcile_parser.add_argument("--live", action="store_true", help="Read and write the project's real Sheet instead of its test Sheet")
    reconcile_parser.add_argument("--dry-run", action="store_true", help="Print what would be proposed; prompt for nothing and write nothing")
    reconcile_parser.add_argument("--log-dir", default="logs", help="Directory to write the timestamped run log to")

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
    if args.command == "reconcile-files":
        return cmd_reconcile_files(args)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
