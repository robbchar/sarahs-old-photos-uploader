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
REQUIRED_UPLOAD_COLUMNS = ("identifier", "file", "mediatype", "title", "date")
CHUNK_SIZE = 500
TEST_COLLECTION = "test_collection"
TEST_IDENTIFIER_PREFIX = "zztest-"


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
        # Test-collection identifiers use the literal "zztest" in place of
        # the real collection_key (see TEST_IDENTIFIER_PREFIX), keeping the
        # real PROJECTID — e.g. zztest-astoriaphotos-00001.
        is_real_prefix = collection_key == registry.get("collection_key")
        is_test_prefix = collection_key == TEST_IDENTIFIER_PREFIX.rstrip("-")
        known_prefix = (is_real_prefix or is_test_prefix) and project_id in registry.get(
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


def validate_rows(rows: list[dict[str, str]], files_dir: str | Path, registry: dict) -> list[RowValidation]:
    seen_identifiers: dict[str, int] = {}
    results: list[RowValidation] = []

    for offset, row in enumerate(rows):
        row_number = offset + 2  # header is row 1
        identifier = row.get("identifier", "").strip()
        errors: list[str] = []

        for column in REQUIRED_UPLOAD_COLUMNS:
            if not row.get(column, "").strip():
                errors.append(f"missing required column '{column}'")

        if identifier:
            errors.extend(check_identifier(identifier, row_number, registry, seen_identifiers))

        file_value = row.get("file", "").strip()
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
    error: str | None = None,
) -> None:
    entry = {
        "identifier": identifier,
        "file": file_value,
        "status": status,
        "error": error,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def load_prior_successes(log_path: str | Path) -> set[str]:
    successes: set[str] = set()
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("status") == "success":
                successes.add(entry["identifier"])
    return successes


def check_live_safety(rows: list[dict], live: bool) -> list[str]:
    if live:
        return []

    errors: list[str] = []
    for offset, row in enumerate(rows):
        row_number = offset + 2
        identifier = row.get("identifier", "").strip()
        if identifier and not identifier.startswith(TEST_IDENTIFIER_PREFIX):
            errors.append(
                f"row {row_number}: identifier '{identifier}' is not prefixed with "
                f"'{TEST_IDENTIFIER_PREFIX}' (required unless --live is passed)"
            )
    return errors


def upload_row(row: dict, collection: str, files_dir: str | Path) -> None:
    identifier = row["identifier"].strip()
    file_path = Path(files_dir) / row["file"].strip()
    metadata = {
        key: value.strip()
        for key, value in row.items()
        if key not in ("identifier", "file") and value.strip()
    }
    metadata["collection"] = collection

    responses = internetarchive.upload(
        identifier,
        files=[str(file_path)],
        metadata=metadata,
    )
    for response in responses:
        if not response.ok:
            raise RuntimeError(
                f"upload of '{identifier}' failed with status {response.status_code}: {response.text}"
            )


def update_metadata_row(row: dict) -> None:
    identifier = row["identifier"].strip()
    metadata = {
        key: value.strip()
        for key, value in row.items()
        if key != "identifier" and value.strip()
    }

    response = internetarchive.modify_metadata(identifier, metadata=metadata)
    if not response.ok:
        raise RuntimeError(
            f"metadata update of '{identifier}' failed with status {response.status_code}: {response.text}"
        )


def validate_identifiers(rows: list[dict[str, str]], registry: dict) -> list[RowValidation]:
    seen_identifiers: dict[str, int] = {}
    results: list[RowValidation] = []

    for offset, row in enumerate(rows):
        row_number = offset + 2
        identifier = row.get("identifier", "").strip()
        errors = check_identifier(identifier, row_number, registry, seen_identifiers)
        results.append(RowValidation(row_number=row_number, identifier=identifier, errors=errors))

    return results


def cmd_validate(args) -> int:
    rows = read_rows(args.csv)
    registry = load_registry(args.registry)
    results = validate_rows(rows, args.files_dir, registry)
    print(format_report(results))
    return 0 if all(r.is_valid for r in results) else 1


def cmd_upload(args) -> int:
    rows = read_rows(args.csv)
    registry = load_registry(args.registry)

    validation_results = validate_rows(rows, args.files_dir, registry)
    if not all(r.is_valid for r in validation_results):
        print(format_report(validation_results))
        print(
            "validation failed; run 'validate' and fix the errors above before uploading",
            file=sys.stderr,
        )
        return 1

    safety_errors = check_live_safety(rows, args.live)
    if safety_errors:
        for error in safety_errors:
            print(error, file=sys.stderr)
        return 1

    collection = args.collection if args.live else TEST_COLLECTION

    skip_identifiers: set[str] = set()
    if args.resume_from:
        skip_identifiers = load_prior_successes(args.resume_from)

    log_path = open_log(args.log_dir, "upload")
    for identifier in skip_identifiers:
        log_result(log_path, identifier, "", "success", error="carried over from resumed log")

    had_failure = False
    for chunk in chunk_rows(rows):
        for row in chunk:
            identifier = row["identifier"].strip()
            if identifier in skip_identifiers:
                continue
            file_value = row["file"].strip()
            try:
                upload_row(row, collection, args.files_dir)
                log_result(log_path, identifier, file_value, "success")
            except Exception as exc:
                had_failure = True
                log_result(log_path, identifier, file_value, "failure", error=str(exc))

    print(f"log written to {log_path}")
    return 1 if had_failure else 0


def cmd_sync_metadata(args) -> int:
    rows = read_rows(args.csv)
    registry = load_registry(args.registry)

    validation_results = validate_identifiers(rows, registry)
    if not all(r.is_valid for r in validation_results):
        print(format_report(validation_results))
        print("identifier validation failed; fix the errors above before syncing", file=sys.stderr)
        return 1

    safety_errors = check_live_safety(rows, args.live)
    if safety_errors:
        for error in safety_errors:
            print(error, file=sys.stderr)
        return 1

    skip_identifiers: set[str] = set()
    if args.resume_from:
        skip_identifiers = load_prior_successes(args.resume_from)

    log_path = open_log(args.log_dir, "sync-metadata")
    for identifier in skip_identifiers:
        log_result(log_path, identifier, "", "success", error="carried over from resumed log")

    had_failure = False
    for chunk in chunk_rows(rows):
        for row in chunk:
            identifier = row["identifier"].strip()
            if identifier in skip_identifiers:
                continue
            try:
                update_metadata_row(row)
                log_result(log_path, identifier, "", "success")
            except Exception as exc:
                had_failure = True
                log_result(log_path, identifier, "", "failure", error=str(exc))

    print(f"log written to {log_path}")
    return 1 if had_failure else 0


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
