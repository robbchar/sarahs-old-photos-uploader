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
from typing import Iterator

import googleapiclient.discovery
import internetarchive
import requests
from googleapiclient.errors import HttpError

import google_auth
from column_map import ColumnMap, check_column_map, check_grid_shape, grid_to_rows
from ia_fields import suggest_standard_fields
from identifiers import RowState, classify_row
from project_config import ProjectConfig, load_project_config
from sheet_client import SheetClient

IDENTIFIER_RE = re.compile(r"^[a-z0-9]+-[a-z0-9]+-\d{5}$")
REQUIRED_UPLOAD_COLUMNS = ("identifier", "file", "mediatype", "title")
# Deliberately excludes both "identifier" and "file" - do not "fix" this back
# to REQUIRED_UPLOAD_COLUMNS.
#
# "identifier": a Sheet row's identifier is minted by `upload`, not
# pre-assigned like a CSV row's - a blank identifier is the normal starting
# state for every new row (RowState.UNASSIGNED), not an error.
#
# "file": a Sheet row has no literal `file` column at all. Task 9 builds a
# row's file path from `file_template` in the registry (e.g.
# "{cd}/{file_on_array}"), combining several Sheet columns with a root
# directory - `file` is Task 9's output, not Task 8's input. Requiring it
# here (or checking it against disk - see validate_rows' check_file_exists)
# would fail every row on a machine that doesn't have the photos present,
# which is exactly the machine Phase 1 (this task) is meant to run on: a
# human validating a copy of the real Sheet with "no IA credentials and not
# a single file on disk".
SHEET_REQUIRED_COLUMNS = ("mediatype", "title")
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


def chunk_rows(rows: list[dict], chunk_size: int = CHUNK_SIZE) -> "Iterator[list[dict]]":
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
) -> list[str]:
    identifier = identifier.strip()
    if not identifier:
        return ["missing required column 'identifier'"]

    errors: list[str] = []
    if not IDENTIFIER_RE.match(identifier):
        errors.append(
            f"identifier '{identifier}' does not match scheme COLLECTIONKEY-PROJECTID-NUMBER"
        )
    else:
        collection_key, project_id, _number = identifier.split("-")
        known_prefix = collection_key == registry.get("collection_key") and project_id in registry.get(
            "projects", {}
        )
        if not known_prefix:
            errors.append(
                f"identifier prefix '{collection_key}-{project_id}' not found in project registry"
            )

    if identifier in seen_identifiers:
        errors.append(f"identifier '{identifier}' duplicates row {seen_identifiers[identifier]}")
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
) -> list[RowValidation]:
    """skip_identifiers lets a --resume-from run skip re-validating rows a
    prior run already validated and uploaded successfully - the identifier
    is still tracked for duplicate detection, just without redoing the
    regex/registry/disk-stat checks.

    required_columns defaults to REQUIRED_UPLOAD_COLUMNS (the CSV path,
    unchanged) but the Sheet path passes SHEET_REQUIRED_COLUMNS, which
    excludes 'identifier' and 'file' - see that constant's comment for why.

    check_file_exists defaults to True (the CSV path, unchanged: a CSV row's
    'file' is a real, already-resolvable path, so checking it on disk is
    correct there). The Sheet path passes False explicitly - Phase 1 (this
    task) runs with no files on disk by design, and a Sheet row has no
    'file' value to check yet regardless (see SHEET_REQUIRED_COLUMNS). This
    is a dedicated flag rather than relying on required_columns excluding
    'file', or on files_dir pointing nowhere, so the skip is an explicit
    statement of intent instead of an incidental side effect of some other
    setting."""
    seen_identifiers: dict[str, int] = {}
    results: list[RowValidation] = []

    for offset, row in enumerate(rows):
        row_number = offset + 2  # header is row 1
        identifier = (row.get("identifier") or "").strip()

        if identifier in skip_identifiers:
            seen_identifiers.setdefault(identifier, row_number)
            results.append(RowValidation(row_number=row_number, identifier=identifier))
            continue

        errors: list[str] = check_row_shape(row)

        for column in required_columns:
            if not (row.get(column) or "").strip():
                errors.append(f"missing required column '{column}'")

        if identifier:
            errors.extend(check_identifier(identifier, row_number, registry, seen_identifiers))

        if check_file_exists:
            file_value = (row.get("file") or "").strip()
            if file_value:
                file_path = Path(files_dir) / file_value
                if not file_path.is_file():
                    errors.append(f"file not found: {file_path}")

        results.append(RowValidation(row_number=row_number, identifier=identifier, errors=errors))

    return results


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
    Sheet header to IA field name is reviewable by a human."""
    lines = ["will upload these metadata fields:"]
    fields = column_map.uploadable_fields()
    lines.append("  " + ", ".join(fields) if fields else "  (none)")
    if column_map.held_back:
        lines.append("held back (LCPS Internal): " + ", ".join(column_map.held_back))
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

    row_results = validate_rows(
        rows,
        config.files_dir,
        registry,
        required_columns=SHEET_REQUIRED_COLUMNS,
        check_file_exists=False,
    )
    # A structural problem with one specific data row (a long row) belongs
    # IN that row's own result, not in a second entry printed beside it.
    # Filing it separately made the row appear twice with opposite verdicts
    # ("[FAIL] row 9" from check_grid_shape, "[PASS] row 9" from
    # validate_rows), inflated format_report's denominator past the number
    # of data rows the Sheet actually has, and - worst - left the row
    # counted as "ready to upload" by the lifecycle summary, which reads
    # row_results alone. check_grid_shape and validate_rows both number
    # rows `offset + 2`, so row_number - 2 indexes row_results exactly.
    # Anything outside that range is header-level and keeps its own row-1
    # entry (as does _GRID_SHAPE_ROW_NUMBER_RE's row-1 fallback) - the
    # bounds check is load-bearing, not defensive: a bare row_number - 2
    # would quietly fold row 1 into the LAST data row via negative indexing.
    header_results: list[RowValidation] = []
    for entry in structure_results:
        index = entry.row_number - 2
        if 0 <= index < len(row_results):
            row_result = row_results[index]
            # structural errors first: a long row's mis-attributed values
            # are the likely cause of whatever content errors follow.
            row_result.errors = entry.errors + row_result.errors
        else:
            header_results.append(entry)

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


def cmd_upload(args) -> int:
    data = read_csv(args.csv)
    rows = data.rows
    registry = load_registry(args.registry)

    skip_identifiers: set[str] = set()
    if args.resume_from:
        skip_identifiers = load_prior_successes(args.resume_from, args.live)

    to_upload = [row for row in rows if (row.get("identifier") or "").strip() not in skip_identifiers]

    validation_results = header_validation(data.fieldnames) + validate_rows(
        rows, args.files_dir, registry, frozenset(skip_identifiers)
    )
    if not all(r.is_valid for r in validation_results):
        print(format_report(validation_results))
        print(
            "validation failed; run 'validate' and fix the errors above before uploading",
            file=sys.stderr,
        )
        return 1

    collection = args.collection if args.live else TEST_COLLECTION

    log_path = open_log(args.log_dir, "upload")
    for identifier in skip_identifiers:
        log_result(log_path, identifier, "", "success", args.live, error="carried over from resumed log")

    counts = run_rows(
        to_upload,
        log_path,
        args.live,
        action="uploading",
        process_row=lambda row, target: upload_row(row, target, collection, args.files_dir),
        describe=lambda row, target: f"{target} ({row['file'].strip()})",
        file_value_for=lambda row: row["file"].strip(),
    )

    print(f"{counts['success']} file(s) uploaded successfully, {counts['failure']} error(s)")
    print(f"log written to {log_path}")
    return 1 if counts["failure"] else 0


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

    upload_parser = subparsers.add_parser("upload", help="Upload items from a validated CSV")
    upload_parser.add_argument("csv", help="Path to the validated CSV")
    upload_parser.add_argument("--project", required=True, help="Project ID from the registry")
    upload_parser.add_argument("--files-dir", default=".", help="Base directory the 'file' column is resolved against")
    upload_parser.add_argument("--registry", default="projects_registry.json", help="Path to the project registry JSON")
    upload_parser.add_argument("--live", action="store_true", help="Target the real collection instead of test_collection")
    upload_parser.add_argument("--collection", default="lcps", help="Collection to upload to when --live is passed")
    upload_parser.add_argument("--log-dir", default="logs", help="Directory to write the timestamped run log to")
    upload_parser.add_argument("--resume-from", default=None, help="Path to a prior log; identifiers marked success there are skipped")

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
