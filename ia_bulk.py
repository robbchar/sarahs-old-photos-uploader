"""Bulk validate/upload/sync-metadata CLI for Internet Archive, driven by a CSV
exported from the LCPS Google Sheet. See docs/ARCHITECTURE.md for the CSV
schema and identifier scheme this script assumes."""
from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

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


def cmd_validate(args) -> int:
    rows = read_rows(args.csv)
    registry = load_registry(args.registry)
    results = validate_rows(rows, args.files_dir, registry)
    print(format_report(results))
    return 0 if all(r.is_valid for r in results) else 1
