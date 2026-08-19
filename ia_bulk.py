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
)
from ia_fields import suggest_standard_fields
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
# "file" IS required here - this is Phase 2's deliberate reversal of Task
# 8's Phase 1 exemption (which ran with no photos on disk by design). By
# the time validate_rows sees a Sheet row, cmd_validate has already turned
# `file_template` plus the row's own columns into a candidate path and
# resolved it against `files_dir` (see resolve_file() in column_map.py) -
# so every row either carries a real, disk-verified `file` value or has
# already failed with the resolver's own error message. See
# docs/DECISIONS.md, "A file is found by resolution, not by constructing a
# path".
SHEET_REQUIRED_COLUMNS = ("mediatype", "title", "file")
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


@dataclass
class RowValidation:
    row_number: int
    identifier: str
    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def validate_rows(
    rows: list[dict[str, str]],
    files_dir: str | Path,
    registry: dict,
    skip_identifiers: frozenset[str] = frozenset(),
    required_columns: tuple[str, ...] = REQUIRED_UPLOAD_COLUMNS,
    check_file_exists: bool = True,
    identifier_column: str = "identifier",
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
    `ia_`-prefixed"."""
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

        results.append(RowValidation(row_number=row_number, identifier=identifier, errors=errors))

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
) -> list[RowValidation]:
    """The Sheet path's answer, named. It differs from the CSV path's in
    exactly two ways, both of which used to travel as loose parameters at
    every call site:

    - the tool's minted identifier lives in `ia_identifier`, never
      `identifier` (which on the real Sheet is the donor's own archival
      reference and would fail the COLLECTIONKEY-PROJECTID-NUMBER regex on
      every row);
    - `ia_identifier` is not required, because blank is the normal starting
      state of a new row - RowState.UNASSIGNED, not an error."""
    return validate_rows(
        rows,
        files_dir,
        registry,
        skip_identifiers=skip_identifiers,
        required_columns=SHEET_REQUIRED_COLUMNS,
        check_file_exists=True,
        identifier_column=IA_IDENTIFIER_COLUMN,
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

    The "not uploaded" section is not decoration. A Sheet column named
    `Identifier` (the real one has one, holding the donor's archival reference)
    normalizes to `identifier`, which upload_row strips because that name is
    Internet Archive's own item identifier. The receipt used to list it among
    the fields that would upload, which was simply untrue - and a receipt an
    operator learns to disbelieve is worse than no receipt."""
    all_fields = column_map.uploadable_fields()
    fields = [name for name in all_fields if name not in DROPPED_BY_UPLOAD_ROW]
    dropped = [name for name in all_fields if name in DROPPED_BY_UPLOAD_ROW]

    lines = ["will upload these metadata fields:"]
    lines.append("  " + ", ".join(fields) if fields else "  (none)")
    if dropped:
        lines.append("NOT uploaded - Internet Archive reserves these names:")
        lines.append(f"  {', '.join(dropped)}")
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
    row failing identifier validation cannot keep. Every row falls into
    exactly one of UNASSIGNED/DONE/RESERVED and then exactly one of
    valid/invalid, so the six counts below always sum to len(rows)."""
    if len(rows) != len(row_results):
        raise ValueError(
            f"format_lifecycle_summary: got {len(rows)} row(s) but {len(row_results)} "
            "row_results - they must be the same length, in the same order. Pass "
            "validate_rows()'s own return value here, not the combined report (which "
            "also carries sheet_structure_validation()'s row-1/shape entries)."
        )

    ready = failed_unassigned = 0
    done = failed_done = 0
    reserved = failed_reserved = 0

    for row, result in zip(rows, row_results):
        state = classify_row(row)
        valid = result.is_valid
        if state is RowState.UNASSIGNED:
            if valid:
                ready += 1
            else:
                failed_unassigned += 1
        elif state is RowState.DONE:
            if valid:
                done += 1
            else:
                failed_done += 1
        elif state is RowState.RESERVED:
            if valid:
                reserved += 1
            else:
                failed_reserved += 1

    lines = [f"{_pluralize(ready, 'row')} ready to upload (no identifier yet)"]
    if failed_unassigned:
        lines.append(
            f"{_pluralize(failed_unassigned, 'row')} not yet assigned an identifier but failed "
            "validation - see the errors above; will not be uploaded until fixed"
        )
    lines.append(f"{done} already uploaded")
    if failed_done:
        lines.append(
            f"{_pluralize(failed_done, 'row')} already uploaded but now fail validation - see "
            "the errors above; this needs a human to look, not an automatic retry"
        )
    lines.append(f"{reserved} reserved but unconfirmed - will retry under existing identifier")
    if failed_reserved:
        lines.append(
            f"{_pluralize(failed_reserved, 'row')} reserved but invalid - see the errors above; "
            "will NOT retry automatically until fixed"
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
        lines.append(f"[{status}] row {result.row_number}{label}")
        for error in result.errors:
            lines.append(f"    - {error}")
    return lines


def format_report(results: list[RowValidation]) -> str:
    lines = _format_result_lines(results)
    passed = sum(1 for r in results if r.is_valid)
    lines.append("")
    lines.append(f"{passed}/{len(results)} rows passed")
    return "\n".join(lines)


def open_log(log_dir: str | Path, command_name: str) -> Path:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%S")
    return log_dir / f"{command_name}-{timestamp}.jsonl"


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
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def load_prior_successes(log_path: str | Path, live: bool) -> set[str]:
    """Identifiers logged as success/unchanged in the SAME mode (test vs
    --live) as this run. A test-mode log entry only ever confirms that the
    zztest-prefixed item landed in test_collection, never the real one, so
    it must not be allowed to skip a real --live upload (and vice versa).
    Logs from before the "live" field existed have no mode recorded and are
    treated conservatively as not matching either mode."""
    successes: set[str] = set()
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("status") in ("success", "unchanged") and entry.get("live") == live:
                successes.add(entry["identifier"])
    return successes


def effective_identifier(identifier: str, live: bool) -> str:
    return identifier if live else f"{TEST_IDENTIFIER_PREFIX}{identifier}"


def upload_row(row: dict, target_identifier: str, collection: str, files_dir: str | Path) -> None:
    file_path = Path(files_dir) / row["file"].strip()
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
            raise RuntimeError(
                f"upload of '{target_identifier}' failed with status {response.status_code}: {response.text}"
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


def resolve_sheet_files(rows: list[dict[str, str]], config: ProjectConfig) -> dict[int, str]:
    """Resolves each row's file against disk BEFORE validation runs, so a row
    either carries a real, disk-verified 'file' value (and the resolved name,
    which may differ from what the Sheet cell says - see resolve_file() - also
    becomes 'ia_identifier_bib') or is recorded here with the resolver's own
    message. Returns row_number -> message for the rows that failed.

    Shared by `validate` and `upload` deliberately: the value `upload` records
    in `ia_identifier_bib` has to be the same resolved name `validate` showed
    the operator, and two copies of this loop would eventually disagree.

    On failure the Sheet cell's raw, UNVERIFIED candidate must not survive as
    row['file'] - left in place it would coincidentally resolve as a literal
    path for the later disk check (or for an upload), silently masking the
    fact that resolution never actually confirmed this file exists."""
    listing_cache: dict[Path, list[str]] = {}
    file_resolution_errors: dict[int, str] = {}

    for offset, row in enumerate(rows):
        row_number = offset + 2  # header is row 1
        candidate = candidate_path(config.file_template, row)
        try:
            resolved = resolve_file(config.files_dir, candidate, listing_cache)
        except FileResolutionError as exc:
            file_resolution_errors[row_number] = str(exc)
            row["file"] = ""
            continue
        row["file"] = resolved
        row[IA_IDENTIFIER_BIB_COLUMN] = resolved

    return file_resolution_errors


def validate_sheet_grid(
    rows: list[dict[str, str]],
    registry: dict,
    config: ProjectConfig,
    structure_results: list[RowValidation],
    file_resolution_errors: dict[int, str],
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
    row_results = validate_sheet_rows(rows, config.files_dir, registry)

    for row_number, message in file_resolution_errors.items():
        # The resolver's own message first: it names the folder and the name
        # that was looked for, which is the actionable part. The generic
        # "missing required column 'file'" that follows from required_columns
        # (row['file'] was never set - see resolve_sheet_files) merely
        # restates that the row has no file.
        row_result = row_results[row_number - 2]
        row_result.errors = [message] + row_result.errors

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
            "been shared with the service account.",
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
        # with the service account than a real project with zero rows, and
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

    # See docs/DECISIONS.md, "A file is found by resolution, not by
    # constructing a path". `upload` runs the identical two steps, so the
    # value it records in ia_identifier_bib is the one shown here.
    file_resolution_errors = resolve_sheet_files(rows, config)
    header_results, row_results = validate_sheet_grid(
        rows, registry, config, structure_results, file_resolution_errors
    )

    results = header_results + row_results
    print(format_report(results))
    print()
    print(format_field_receipt(column_map))
    print()
    print(format_lifecycle_summary(rows, row_results))
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
    action: str,
    process_row,
    describe,
    file_value_for,
) -> dict[str, int]:
    """Shared chunk/progress/log-and-count loop for cmd_upload and
    cmd_sync_metadata - they differ only in how a row is processed, how its
    progress line reads, and what (if anything) goes in the log's file
    field. process_row(row, target_identifier) may raise MetadataUnchanged
    to count as "unchanged" rather than "failure"."""
    total = len(rows)
    counts = {"success": 0, "unchanged": 0, "failure": 0}
    position = 0
    for chunk in chunk_rows(rows):
        for row in chunk:
            position += 1
            identifier = row["identifier"].strip()
            target_identifier = effective_identifier(identifier, live)
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
    exact ordered sequence rather than "a cell holding some string"."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


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
    and the grid itself for reading `ia_identifier` back."""

    columns: SheetColumns
    grid: list[list[str]]
    fingerprints: dict[int, str]


def read_sheet_snapshot(client: SheetClient, file_template: str) -> SheetSnapshot:
    grid = client.read_grid()
    column_map, rows = grid_to_rows(grid)
    return SheetSnapshot(
        columns=locate_write_back_columns(column_map),
        grid=grid,
        fingerprints=sheet_row_fingerprints(rows, file_template),
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
) -> list[UploadTarget]:
    """Decides what this run will upload and under which identifier.

    Numbers are minted for the whole run up front, before any chunk is
    reserved: minting is pure arithmetic with no side effect, and doing it once
    means a later chunk cannot re-mint an earlier chunk's numbers by reading a
    Sheet that has not been written yet.

    `existing` deliberately spans EVERY row, including rows that failed
    validation and rows already DONE - a number that appears anywhere in the
    Sheet is spent, whatever the state of the row holding it."""
    if len(rows) != len(row_results):
        raise ValueError(
            f"plan_upload_targets: got {len(rows)} row(s) but {len(row_results)} row_results - "
            "they must be the same length, in the same order."
        )

    existing = [row.get(IA_IDENTIFIER_COLUMN) or "" for row in rows]

    pending: list[tuple[int, dict[str, str], RowState]] = []
    for offset, (row, result) in enumerate(zip(rows, row_results)):
        if not result.is_valid:
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
                uploaded_as=effective_identifier(identifier, live),
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

    def execute(self, targets: list[UploadTarget]) -> dict[str, int]:
        """One chunk at a time: verify, reserve, upload, verify, confirm,
        having logged each row's outcome as it happened.

        Chunking is what keeps this inside the Sheets API's 60 writes per
        minute per user - a batch counts as one request, so ~10,000 rows cost
        about 40 requests instead of 10,000. It is also why the guard has to
        run per chunk: the last chunk's reserve write can be hours after the
        read that fixed its row numbers.

        `CHUNK_SIZE` is read here rather than taken as chunk_rows' default so
        the chunk boundary is reachable in a test - a protocol that is only
        ever exercised with a single chunk is a protocol nobody has tested."""
        counts = {"success": 0, "failure": 0, "unconfirmed": 0, "not_attempted": 0}
        total = len(targets)
        position = 0
        done = 0

        for chunk in chunk_rows(targets, CHUNK_SIZE):
            # Every chunk gets a fresh timestamp. One timestamp for the whole
            # run would stamp chunk 20 with the time chunk 1 started, which on
            # a full-collection run is hours wrong.
            uploaded_at = upload_timestamp()

            if self.write_back:
                reservable, moved = self._verify(chunk, reserved_already=False)
                for target in moved:
                    counts["not_attempted"] += 1
                    self._report_moved(target, uploaded=False)
                if not self._write(reserve_updates(reservable, self.columns), "reserve"):
                    counts["not_attempted"] += len(targets) - done - len(moved)
                    return counts
                chunk = reservable

            succeeded: list[UploadTarget] = []
            for target in chunk:
                position += 1
                done += 1
                print(f"[{position}/{total}] uploading {target.uploaded_as} ({target.row['file']})")
                try:
                    upload_row(
                        sheet_upload_metadata(target, self.column_map, self.mediatype),
                        target.uploaded_as,
                        self.collection,
                        self.files_dir,
                    )
                except Exception as exc:
                    counts["failure"] += 1
                    self._log(target, "failure", error=str(exc))
                    continue
                counts["success"] += 1
                self._log(target, "success")
                succeeded.append(target)

            if self.write_back and succeeded:
                confirmable, moved = self._verify(succeeded, reserved_already=True)
                for target in moved:
                    counts["unconfirmed"] += 1
                    self._report_moved(target, uploaded=True)
                if not self._write(confirm_updates(confirmable, self.columns, uploaded_at), "confirm"):
                    counts["unconfirmed"] += len(confirmable)
                    for target in confirmable:
                        self._log(target, "unconfirmed", error="the Sheet write failed")
                    return counts

        return counts

    def _verify(
        self, targets: list[UploadTarget], reserved_already: bool
    ) -> tuple[list[UploadTarget], list[UploadTarget]]:
        """Re-reads the Sheet and splits the targets into those still at the
        row this run planned for them and those that have moved. Runs before
        BOTH writes - see split_moved_targets for why the fingerprint, and not
        `ia_identifier`, is what makes the check meaningful."""
        snapshot = read_sheet_snapshot(self.client, self.file_template)
        if snapshot.columns != self.columns:
            # A column was inserted, deleted or renamed. Every cached column
            # index is now wrong, so every write this run could make would land
            # in the wrong column - not just for this chunk.
            return [], list(targets)
        return split_moved_targets(targets, snapshot, reserved_already)

    def _report_moved(self, target: UploadTarget, uploaded: bool) -> None:
        outcome = (
            f"The item IS on Internet Archive as '{target.uploaded_as}' but is NOT recorded in "
            "the Sheet."
            if uploaded
            else "Nothing was uploaded for it."
        )
        message = (
            f"row {target.row_number} is no longer the row this run planned for "
            f"'{target.identifier}' - the Sheet was edited while the run was in progress, so "
            f"writing there would land on a different photograph. {outcome} Rerun once the "
            "Sheet has settled."
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
            "been shared with the service account.",
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

    try:
        columns = locate_write_back_columns(column_map)
    except MissingWriteBackColumns as exc:
        print(f"project '{config.project_id}': {exc}", file=sys.stderr)
        return 1

    # Fingerprints come from the RAW cells, so they must be taken before
    # resolve_sheet_files() rewrites row['file'] to the resolved name. They are
    # what every later mid-run-edit check compares against.
    source_fingerprints = sheet_row_fingerprints(rows, config.file_template)

    file_resolution_errors = resolve_sheet_files(rows, config)
    header_results, row_results = validate_sheet_grid(
        rows, registry, config, structure_results, file_resolution_errors
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

    skipped = [result for result in row_results if not result.is_valid]
    if skipped:
        print("\n".join(_format_result_lines(skipped)))
        print(
            f"{_pluralize(len(skipped), 'row')} failed validation and will be skipped; the rest "
            "are uploaded, and this command still exits non-zero so a partial run is never "
            "mistaken for a clean one"
        )
        print()

    targets = plan_upload_targets(rows, row_results, config, live, source_fingerprints)
    collection = config.ia_collection if live else TEST_COLLECTION

    # `upload` is where something permanent happens, so it shows the same
    # field receipt `validate` does rather than assuming the operator ran
    # validate first and remembers what it said.
    print(format_field_receipt(column_map))
    print()

    if dry_run:
        print_dry_run(targets, columns, write_back, upload_timestamp())
        return 1 if skipped else 0

    if not targets:
        print("nothing to upload - every valid row is already marked uploaded")
        return 1 if skipped else 0

    log_path = open_log(args.log_dir, "upload")
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
    ).execute(targets)

    print(f"{counts['success']} file(s) uploaded successfully, {counts['failure']} error(s)")
    if counts["unconfirmed"]:
        print(
            f"{_pluralize(counts['unconfirmed'], 'item')} uploaded but NOT recorded in the Sheet "
            "- see the messages above"
        )
    if counts["not_attempted"]:
        print(
            f"{_pluralize(counts['not_attempted'], 'row')} not attempted - see the messages above"
        )
    if skipped:
        print(f"{_pluralize(len(skipped), 'row')} skipped (failed validation)")
    print(f"log written to {log_path}")
    return (
        1
        if (counts["failure"] or counts["unconfirmed"] or counts["not_attempted"] or skipped)
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
