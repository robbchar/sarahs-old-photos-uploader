"""Bulk validate/upload/sync-metadata CLI for Internet Archive, driven by a CSV
exported from the LCPS Google Sheet. See docs/ARCHITECTURE.md for the CSV
schema and identifier scheme this script assumes."""
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

import internetarchive

IDENTIFIER_RE = re.compile(r"^[a-z0-9]+-[a-z0-9]+-\d{5}$")
REQUIRED_UPLOAD_COLUMNS = ("identifier", "file", "mediatype", "title")
CHUNK_SIZE = 500
TEST_COLLECTION = "test_collection"
TEST_IDENTIFIER_PREFIX = "zztest-"
UNDATED_PLACEHOLDER = "[n.d.]"


def chunk_rows(rows: list[dict], chunk_size: int = CHUNK_SIZE) -> "Iterator[list[dict]]":
    for start in range(0, len(rows), chunk_size):
        yield rows[start : start + chunk_size]


def read_rows(csv_path: str | Path) -> list[dict[str, str]]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


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
) -> list[RowValidation]:
    """skip_identifiers lets a --resume-from run skip re-validating rows a
    prior run already validated and uploaded successfully - the identifier
    is still tracked for duplicate detection, just without redoing the
    regex/registry/disk-stat checks."""
    seen_identifiers: dict[str, int] = {}
    results: list[RowValidation] = []

    for offset, row in enumerate(rows):
        row_number = offset + 2  # header is row 1
        identifier = (row.get("identifier") or "").strip()

        if identifier in skip_identifiers:
            seen_identifiers.setdefault(identifier, row_number)
            results.append(RowValidation(row_number=row_number, identifier=identifier))
            continue

        errors: list[str] = []

        for column in REQUIRED_UPLOAD_COLUMNS:
            if not (row.get(column) or "").strip():
                errors.append(f"missing required column '{column}'")

        if identifier:
            errors.extend(check_identifier(identifier, row_number, registry, seen_identifiers))

        file_value = (row.get("file") or "").strip()
        if file_value:
            file_path = Path(files_dir) / file_value
            if not file_path.is_file():
                errors.append(f"file not found: {file_path}")

        results.append(RowValidation(row_number=row_number, identifier=identifier, errors=errors))

    return results


def format_report(results: list[RowValidation]) -> str:
    lines: list[str] = []
    for result in results:
        status = "PASS" if result.is_valid else "FAIL"
        label = result.identifier or f"(row {result.row_number})"
        lines.append(f"[{status}] row {result.row_number} {label}")
        for error in result.errors:
            lines.append(f"    - {error}")

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


def cmd_validate(args) -> int:
    rows = read_rows(args.csv)
    registry = load_registry(args.registry)
    results = validate_rows(rows, args.files_dir, registry)
    print(format_report(results))
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
    rows = read_rows(args.csv)
    registry = load_registry(args.registry)

    skip_identifiers: set[str] = set()
    if args.resume_from:
        skip_identifiers = load_prior_successes(args.resume_from, args.live)

    to_upload = [row for row in rows if row["identifier"].strip() not in skip_identifiers]

    validation_results = validate_rows(rows, args.files_dir, registry, frozenset(skip_identifiers))
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
    rows = read_rows(args.csv)
    registry = load_registry(args.registry)

    skip_identifiers: set[str] = set()
    if args.resume_from:
        skip_identifiers = load_prior_successes(args.resume_from, args.live)

    to_sync = [row for row in rows if row["identifier"].strip() not in skip_identifiers]

    validation_results = validate_identifiers(rows, registry, frozenset(skip_identifiers))
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
        description="Validate, upload, and sync metadata for Internet Archive items from a CSV.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="Validate a CSV without touching the network")
    validate_parser.add_argument("csv", help="Path to the CSV to validate")
    validate_parser.add_argument("--files-dir", default=".", help="Base directory the 'file' column is resolved against")
    validate_parser.add_argument("--registry", default="projects_registry.json", help="Path to the project registry JSON")

    upload_parser = subparsers.add_parser("upload", help="Upload items from a validated CSV")
    upload_parser.add_argument("csv", help="Path to the validated CSV")
    upload_parser.add_argument("--files-dir", default=".", help="Base directory the 'file' column is resolved against")
    upload_parser.add_argument("--registry", default="projects_registry.json", help="Path to the project registry JSON")
    upload_parser.add_argument("--live", action="store_true", help="Target the real collection instead of test_collection")
    upload_parser.add_argument("--collection", default="lcps", help="Collection to upload to when --live is passed")
    upload_parser.add_argument("--log-dir", default="logs", help="Directory to write the timestamped run log to")
    upload_parser.add_argument("--resume-from", default=None, help="Path to a prior log; identifiers marked success there are skipped")

    sync_parser = subparsers.add_parser("sync-metadata", help="Update metadata on already-uploaded items")
    sync_parser.add_argument("csv", help="Path to the CSV of identifier + changed metadata columns")
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
