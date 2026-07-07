# IA Bulk Upload CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a single Python CLI script (`ia_bulk.py`) with three subcommands — `validate`, `upload`, `sync-metadata` — that drive bulk Internet Archive operations from a CSV export of the LCPS Google Sheet, with offline validation, chunked/logged uploads, resumability, and a test-collection safety rail.

**Architecture:** One argparse-based script with pure, independently-testable functions (CSV reading, identifier/schema validation, chunking, JSONL logging) wired up by three thin `cmd_*` functions. Network calls go through the `internetarchive` Python library (not CLI shell-outs) so upload/metadata-update outcomes are captured per-row as structured success/failure for logging and resume support.

**Tech Stack:** Python 3.11+, stdlib `argparse`/`csv`/`json`/`re`/`dataclasses`, `internetarchive` package, `pytest` for tests.

## Global Constraints

- Identifier scheme: `COLLECTIONKEY-PROJECTID-NUMBER`, all lowercase, hyphen-separated, NUMBER is 5-digit zero-padded. Identifiers are pre-assigned in the Sheet — the script never generates or mutates them, only validates them.
- Required CSV columns for `validate`/`upload`: `identifier`, `file`, `mediatype`, `title`, `date`. All other columns pass through untouched to `ia` calls.
- `sync-metadata` CSVs only require an `identifier` column plus whichever changed metadata columns are present — not the full upload schema.
- IA batch limits: chunk all upload/sync-metadata runs at 500 rows.
- Safety rail: default target is `test_collection`; `--live` is required to target the real collection. When not `--live`, every identifier in the CSV must be prefixed `zztest-`, or the run fails before any network call.
- Project registry (`projects_registry.json`) holds the known `collection_key` and `PROJECTID` values; `validate` checks identifier prefixes against it.
- Logs are newline-delimited JSON (`.jsonl`), one line per row outcome, written to `logs/<command>-<timestamp>.jsonl`.
- US English spelling in all output strings, docs, and comments.
- Test files live alongside the code they test (`ia_bulk.py` / `test_ia_bulk.py`), not in a separate `tests/` directory.
- Before writing any code that calls the `internetarchive` library, verify its exact function signatures via the Context7 MCP tool — do not guess at library API shape.

---

## File Structure

```
ia_bulk.py                          # single script: all subcommands
test_ia_bulk.py                     # pytest suite, alongside ia_bulk.py
projects_registry.json              # collection_key + known PROJECTID registry
requirements.txt                    # internetarchive, pytest
docs/ARCHITECTURE.md                # schema, identifier scheme, chunking/resume/logging design
logs/                                # created at runtime, gitignored
.gitignore                          # logs/, __pycache__/, *.pyc
```

---

### Task 1: CSV reading + registry loading + core row validation (no network)

**Files:**
- Create: `ia_bulk.py`
- Test: `test_ia_bulk.py`

**Interfaces:**
- Produces:
  - `IDENTIFIER_RE: re.Pattern` — `^[a-z0-9]+-[a-z0-9]+-\d{5}$`
  - `REQUIRED_UPLOAD_COLUMNS: tuple[str, ...]` = `("identifier", "file", "mediatype", "title", "date")`
  - `read_rows(csv_path: str | Path) -> list[dict[str, str]]`
  - `load_registry(registry_path: str | Path) -> dict`
  - `@dataclass RowValidation` with fields `row_number: int`, `identifier: str`, `errors: list[str]`, and property `is_valid: bool`
  - `check_identifier(identifier: str, row_number: int, registry: dict, seen_identifiers: dict[str, int]) -> list[str]`
  - `validate_rows(rows: list[dict], files_dir: str | Path, registry: dict) -> list[RowValidation]`

- [ ] **Step 1: Write the failing tests for `read_rows` and `load_registry`**

```python
# test_ia_bulk.py
import json
import csv
from pathlib import Path

import pytest

from ia_bulk import read_rows, load_registry


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_read_rows_returns_list_of_dicts(tmp_path):
    csv_path = tmp_path / "items.csv"
    write_csv(
        csv_path,
        ["identifier", "file", "mediatype", "title", "date"],
        [
            {
                "identifier": "zztest-astoriaphotos-00001",
                "file": "photo1.jpg",
                "mediatype": "image",
                "title": "First photo",
                "date": "1958",
            }
        ],
    )

    rows = read_rows(csv_path)

    assert rows == [
        {
            "identifier": "zztest-astoriaphotos-00001",
            "file": "photo1.jpg",
            "mediatype": "image",
            "title": "First photo",
            "date": "1958",
        }
    ]


def test_load_registry_reads_json(tmp_path):
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "lcps", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )

    registry = load_registry(registry_path)

    assert registry == {"collection_key": "lcps", "projects": {"astoriaphotos": {}}}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest test_ia_bulk.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ia_bulk'` (file doesn't exist yet).

- [ ] **Step 3: Implement `read_rows` and `load_registry`**

```python
# ia_bulk.py
"""Bulk validate/upload/sync-metadata CLI for Internet Archive, driven by a CSV
exported from the LCPS Google Sheet. See docs/ARCHITECTURE.md for the CSV
schema and identifier scheme this script assumes."""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

IDENTIFIER_RE = re.compile(r"^[a-z0-9]+-[a-z0-9]+-\d{5}$")
REQUIRED_UPLOAD_COLUMNS = ("identifier", "file", "mediatype", "title", "date")
CHUNK_SIZE = 500
TEST_COLLECTION = "test_collection"
TEST_IDENTIFIER_PREFIX = "zztest-"


def read_rows(csv_path: str | Path) -> list[dict[str, str]]:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_registry(registry_path: str | Path) -> dict:
    with open(registry_path, encoding="utf-8") as f:
        return json.load(f)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_ia_bulk.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Write the failing tests for `check_identifier`**

```python
from ia_bulk import check_identifier


def make_registry():
    return {"collection_key": "lcps", "projects": {"astoriaphotos": {}}}


def test_check_identifier_accepts_valid_registered_identifier():
    errors = check_identifier(
        "lcps-astoriaphotos-00001", row_number=2, registry=make_registry(), seen_identifiers={}
    )
    assert errors == []


def test_check_identifier_rejects_bad_scheme():
    errors = check_identifier(
        "LCPS_astoriaphotos_1", row_number=2, registry=make_registry(), seen_identifiers={}
    )
    assert len(errors) == 1
    assert "does not match scheme" in errors[0]


def test_check_identifier_rejects_unknown_prefix():
    errors = check_identifier(
        "lcps-unknownproject-00001", row_number=2, registry=make_registry(), seen_identifiers={}
    )
    assert len(errors) == 1
    assert "not found in project registry" in errors[0]


def test_check_identifier_accepts_zztest_prefix_with_known_project():
    # Test-collection identifiers replace COLLECTIONKEY with the literal
    # "zztest" (per CLAUDE.md), keeping the real PROJECTID — e.g.
    # zztest-astoriaphotos-00001, not zztest-lcps-00001.
    errors = check_identifier(
        "zztest-astoriaphotos-00001", row_number=2, registry=make_registry(), seen_identifiers={}
    )
    assert errors == []


def test_check_identifier_rejects_zztest_prefix_with_unknown_project():
    errors = check_identifier(
        "zztest-unknownproject-00001", row_number=2, registry=make_registry(), seen_identifiers={}
    )
    assert len(errors) == 1
    assert "not found in project registry" in errors[0]


def test_check_identifier_rejects_duplicate():
    seen = {"lcps-astoriaphotos-00001": 2}
    errors = check_identifier(
        "lcps-astoriaphotos-00001", row_number=5, registry=make_registry(), seen_identifiers=seen
    )
    assert len(errors) == 1
    assert "duplicates row 2" in errors[0]


def test_check_identifier_rejects_empty():
    errors = check_identifier("", row_number=2, registry=make_registry(), seen_identifiers={})
    assert errors == ["missing required column 'identifier'"]
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `python -m pytest test_ia_bulk.py -v -k check_identifier`
Expected: FAIL with `ImportError: cannot import name 'check_identifier'`

- [ ] **Step 7: Implement `check_identifier`**

```python
# ia_bulk.py (append)
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
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest test_ia_bulk.py -v -k check_identifier`
Expected: PASS (5 passed)

- [ ] **Step 9: Write the failing tests for `validate_rows`**

```python
from ia_bulk import validate_rows, RowValidation


def test_validate_rows_passes_a_fully_valid_row(tmp_path):
    (tmp_path / "photo1.jpg").write_bytes(b"fake-image-bytes")
    rows = [
        {
            "identifier": "lcps-astoriaphotos-00001",
            "file": "photo1.jpg",
            "mediatype": "image",
            "title": "First photo",
            "date": "1958",
        }
    ]

    results = validate_rows(rows, files_dir=tmp_path, registry=make_registry())

    assert len(results) == 1
    assert results[0].is_valid
    assert results[0].errors == []


def test_validate_rows_flags_missing_file():
    rows = [
        {
            "identifier": "lcps-astoriaphotos-00001",
            "file": "does-not-exist.jpg",
            "mediatype": "image",
            "title": "First photo",
            "date": "1958",
        }
    ]

    results = validate_rows(rows, files_dir="/tmp", registry=make_registry())

    assert not results[0].is_valid
    assert any("file not found" in e for e in results[0].errors)


def test_validate_rows_flags_missing_required_metadata(tmp_path):
    (tmp_path / "photo1.jpg").write_bytes(b"fake-image-bytes")
    rows = [
        {
            "identifier": "lcps-astoriaphotos-00001",
            "file": "photo1.jpg",
            "mediatype": "",
            "title": "",
            "date": "1958",
        }
    ]

    results = validate_rows(rows, files_dir=tmp_path, registry=make_registry())

    assert not results[0].is_valid
    assert "missing required column 'mediatype'" in results[0].errors
    assert "missing required column 'title'" in results[0].errors


def test_validate_rows_row_numbers_start_at_2_for_header():
    rows = [
        {
            "identifier": "",
            "file": "",
            "mediatype": "",
            "title": "",
            "date": "",
        }
    ]

    results = validate_rows(rows, files_dir="/tmp", registry=make_registry())

    assert results[0].row_number == 2
```

- [ ] **Step 10: Run tests to verify they fail**

Run: `python -m pytest test_ia_bulk.py -v -k validate_rows`
Expected: FAIL with `ImportError: cannot import name 'validate_rows'`

- [ ] **Step 11: Implement `RowValidation` and `validate_rows`**

```python
# ia_bulk.py (append)
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
```

- [ ] **Step 12: Run tests to verify they pass**

Run: `python -m pytest test_ia_bulk.py -v -k validate_rows`
Expected: PASS (4 passed)

- [ ] **Step 13: Commit**

```bash
git add ia_bulk.py test_ia_bulk.py
git commit -m "feat: add CSV reading and offline row validation"
```

---

### Task 2: `validate` CLI subcommand — report formatting + exit codes

**Files:**
- Modify: `ia_bulk.py`
- Test: `test_ia_bulk.py`

**Interfaces:**
- Consumes: `read_rows`, `load_registry`, `validate_rows`, `RowValidation` from Task 1.
- Produces:
  - `format_report(results: list[RowValidation]) -> str`
  - `cmd_validate(args: argparse.Namespace) -> int` — reads `args.csv`, `args.files_dir`, `args.registry`; returns `0` if all rows valid, `1` otherwise; prints the report to stdout.

- [ ] **Step 1: Write the failing tests for `format_report`**

```python
from ia_bulk import format_report, RowValidation


def test_format_report_shows_pass_and_fail_with_summary():
    results = [
        RowValidation(row_number=2, identifier="lcps-astoriaphotos-00001", errors=[]),
        RowValidation(
            row_number=3,
            identifier="lcps-astoriaphotos-00002",
            errors=["file not found: /tmp/missing.jpg"],
        ),
    ]

    report = format_report(results)

    assert "[PASS] row 2 lcps-astoriaphotos-00001" in report
    assert "[FAIL] row 3 lcps-astoriaphotos-00002" in report
    assert "file not found: /tmp/missing.jpg" in report
    assert "1/2 rows passed" in report
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest test_ia_bulk.py -v -k format_report`
Expected: FAIL with `ImportError: cannot import name 'format_report'`

- [ ] **Step 3: Implement `format_report`**

```python
# ia_bulk.py (append)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest test_ia_bulk.py -v -k format_report`
Expected: PASS (1 passed)

- [ ] **Step 5: Write the failing tests for `cmd_validate`**

```python
from argparse import Namespace

from ia_bulk import cmd_validate


def test_cmd_validate_returns_zero_when_all_rows_valid(tmp_path, capsys):
    (tmp_path / "photo1.jpg").write_bytes(b"data")
    csv_path = tmp_path / "items.csv"
    write_csv(
        csv_path,
        ["identifier", "file", "mediatype", "title", "date"],
        [
            {
                "identifier": "lcps-astoriaphotos-00001",
                "file": "photo1.jpg",
                "mediatype": "image",
                "title": "First photo",
                "date": "1958",
            }
        ],
    )
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "lcps", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )
    args = Namespace(csv=str(csv_path), files_dir=str(tmp_path), registry=str(registry_path))

    exit_code = cmd_validate(args)

    assert exit_code == 0
    assert "1/1 rows passed" in capsys.readouterr().out


def test_cmd_validate_returns_one_when_a_row_fails(tmp_path, capsys):
    csv_path = tmp_path / "items.csv"
    write_csv(
        csv_path,
        ["identifier", "file", "mediatype", "title", "date"],
        [
            {
                "identifier": "lcps-astoriaphotos-00001",
                "file": "missing.jpg",
                "mediatype": "image",
                "title": "First photo",
                "date": "1958",
            }
        ],
    )
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "lcps", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )
    args = Namespace(csv=str(csv_path), files_dir=str(tmp_path), registry=str(registry_path))

    exit_code = cmd_validate(args)

    assert exit_code == 1
    assert "0/1 rows passed" in capsys.readouterr().out
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `python -m pytest test_ia_bulk.py -v -k cmd_validate`
Expected: FAIL with `ImportError: cannot import name 'cmd_validate'`

- [ ] **Step 7: Implement `cmd_validate`**

```python
# ia_bulk.py (append)
def cmd_validate(args) -> int:
    rows = read_rows(args.csv)
    registry = load_registry(args.registry)
    results = validate_rows(rows, args.files_dir, registry)
    print(format_report(results))
    return 0 if all(r.is_valid for r in results) else 1
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest test_ia_bulk.py -v -k cmd_validate`
Expected: PASS (2 passed)

- [ ] **Step 9: Commit**

```bash
git add ia_bulk.py test_ia_bulk.py
git commit -m "feat: add validate subcommand report and exit code"
```

---

### Task 3: Chunking + JSONL logging + resume support (no network)

**Files:**
- Modify: `ia_bulk.py`
- Test: `test_ia_bulk.py`

**Interfaces:**
- Produces:
  - `chunk_rows(rows: list[dict], chunk_size: int = CHUNK_SIZE) -> Iterator[list[dict]]`
  - `open_log(log_dir: str | Path, command_name: str) -> Path` — creates `log_dir` if needed, returns a fresh timestamped path, does not create the file itself.
  - `log_result(log_path: str | Path, identifier: str, file_value: str, status: str, error: str | None = None) -> None` — appends one JSON line.
  - `load_prior_successes(log_path: str | Path) -> set[str]` — returns identifiers with `status == "success"`.

- [ ] **Step 1: Write the failing tests for `chunk_rows`**

```python
from ia_bulk import chunk_rows


def test_chunk_rows_splits_into_groups_of_chunk_size():
    rows = [{"n": i} for i in range(1250)]

    chunks = list(chunk_rows(rows, chunk_size=500))

    assert [len(c) for c in chunks] == [500, 500, 250]
    assert chunks[0][0] == {"n": 0}
    assert chunks[2][-1] == {"n": 1249}


def test_chunk_rows_handles_empty_list():
    assert list(chunk_rows([], chunk_size=500)) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest test_ia_bulk.py -v -k chunk_rows`
Expected: FAIL with `ImportError: cannot import name 'chunk_rows'`

- [ ] **Step 3: Implement `chunk_rows`**

```python
# ia_bulk.py (append, near top-level imports add `from typing import Iterator`)
def chunk_rows(rows: list[dict], chunk_size: int = CHUNK_SIZE) -> "Iterator[list[dict]]":
    for start in range(0, len(rows), chunk_size):
        yield rows[start : start + chunk_size]
```

Add `from typing import Iterator` to the imports at the top of `ia_bulk.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_ia_bulk.py -v -k chunk_rows`
Expected: PASS (2 passed)

- [ ] **Step 5: Write the failing tests for logging functions**

```python
import time

from ia_bulk import open_log, log_result, load_prior_successes


def test_open_log_creates_log_dir_and_returns_timestamped_path(tmp_path):
    log_dir = tmp_path / "logs"

    log_path = open_log(log_dir, "upload")

    assert log_dir.is_dir()
    assert log_path.parent == log_dir
    assert log_path.name.startswith("upload-")
    assert log_path.suffix == ".jsonl"


def test_log_result_appends_one_json_line(tmp_path):
    log_path = tmp_path / "upload-test.jsonl"

    log_result(log_path, "lcps-astoriaphotos-00001", "photo1.jpg", "success")
    log_result(log_path, "lcps-astoriaphotos-00002", "photo2.jpg", "failure", error="timeout")

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["identifier"] == "lcps-astoriaphotos-00001"
    assert first["status"] == "success"
    assert first["error"] is None
    second = json.loads(lines[1])
    assert second["status"] == "failure"
    assert second["error"] == "timeout"


def test_load_prior_successes_returns_only_successful_identifiers(tmp_path):
    log_path = tmp_path / "upload-test.jsonl"
    log_result(log_path, "lcps-astoriaphotos-00001", "photo1.jpg", "success")
    log_result(log_path, "lcps-astoriaphotos-00002", "photo2.jpg", "failure", error="timeout")

    successes = load_prior_successes(log_path)

    assert successes == {"lcps-astoriaphotos-00001"}
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `python -m pytest test_ia_bulk.py -v -k "open_log or log_result or load_prior_successes"`
Expected: FAIL with `ImportError`

- [ ] **Step 7: Implement logging functions**

```python
# ia_bulk.py (append; add `import time` to imports)
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
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest test_ia_bulk.py -v -k "open_log or log_result or load_prior_successes"`
Expected: PASS (3 passed)

- [ ] **Step 9: Commit**

```bash
git add ia_bulk.py test_ia_bulk.py
git commit -m "feat: add row chunking and JSONL logging with resume support"
```

---

### Task 4: Live/test-collection safety rail

**Files:**
- Modify: `ia_bulk.py`
- Test: `test_ia_bulk.py`

**Interfaces:**
- Consumes: `TEST_IDENTIFIER_PREFIX` from Task 1.
- Produces: `check_live_safety(rows: list[dict], live: bool) -> list[str]` — returns a list of human-readable error strings; empty list means safe to proceed.

- [ ] **Step 1: Write the failing tests**

```python
from ia_bulk import check_live_safety


def test_check_live_safety_passes_when_live_flag_set():
    rows = [{"identifier": "lcps-astoriaphotos-00001"}]

    errors = check_live_safety(rows, live=True)

    assert errors == []


def test_check_live_safety_passes_when_not_live_and_all_test_prefixed():
    rows = [{"identifier": "zztest-astoriaphotos-00001"}]

    errors = check_live_safety(rows, live=False)

    assert errors == []


def test_check_live_safety_fails_when_not_live_and_real_identifier_present():
    rows = [{"identifier": "lcps-astoriaphotos-00001"}]

    errors = check_live_safety(rows, live=False)

    assert len(errors) == 1
    assert "zztest-" in errors[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest test_ia_bulk.py -v -k check_live_safety`
Expected: FAIL with `ImportError: cannot import name 'check_live_safety'`

- [ ] **Step 3: Implement `check_live_safety`**

```python
# ia_bulk.py (append)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_ia_bulk.py -v -k check_live_safety`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add ia_bulk.py test_ia_bulk.py
git commit -m "feat: add live/test-collection safety rail check"
```

---

### Task 5: `internetarchive` library integration for upload and metadata update

**Files:**
- Modify: `ia_bulk.py`
- Test: `test_ia_bulk.py`
- Create: `requirements.txt`

**Interfaces:**
- Consumes: nothing new from prior tasks beyond stdlib.
- Produces:
  - `upload_row(row: dict, collection: str, files_dir: str | Path) -> None` — raises `RuntimeError` on failure, returns normally on success.
  - `update_metadata_row(row: dict) -> None` — raises `RuntimeError` on failure, returns normally on success.
  - `validate_identifiers(rows: list[dict], registry: dict) -> list[RowValidation]` — identifier-only validation (scheme, registry membership, uniqueness), reused by `sync-metadata` which doesn't require the full upload schema.

- [ ] **Step 1: Verify the `internetarchive` library API via Context7**

Before writing any code in this task, query the Context7 MCP tool for the `internetarchive` PyPI package's current `upload()` and `modify_metadata()` (or equivalent) function signatures, return types, and error-handling conventions. Confirm:
- The exact function used to upload one or more files to an item with metadata (expected: `internetarchive.upload(identifier, files=..., metadata=..., ...)`).
- The exact function used to update metadata on an existing item (expected: `internetarchive.modify_metadata(identifier, metadata=..., ...)`).
- What a failed call looks like (raised exception vs. a response object with `.ok`/`.status_code`).

If the confirmed API differs from what's assumed in Steps 3 and 7 below, adjust the implementation to match the real signatures — the tests (which mock at the `internetarchive.upload`/`internetarchive.modify_metadata` boundary) will still pass as long as the mocked call signature matches what the implementation actually calls.

- [ ] **Step 2: Write the failing tests for `upload_row`**

```python
import internetarchive
from ia_bulk import upload_row


class FakeResponse:
    def __init__(self, ok, status_code=200, text=""):
        self.ok = ok
        self.status_code = status_code
        self.text = text


def test_upload_row_succeeds_when_library_returns_ok_responses(tmp_path, monkeypatch):
    (tmp_path / "photo1.jpg").write_bytes(b"data")
    row = {
        "identifier": "zztest-astoriaphotos-00001",
        "file": "photo1.jpg",
        "mediatype": "image",
        "title": "First photo",
        "date": "1958",
    }
    captured = {}

    def fake_upload(identifier, files, metadata, **kwargs):
        captured["identifier"] = identifier
        captured["files"] = files
        captured["metadata"] = metadata
        return [FakeResponse(ok=True)]

    monkeypatch.setattr(internetarchive, "upload", fake_upload)

    upload_row(row, collection="test_collection", files_dir=tmp_path)

    assert captured["identifier"] == "zztest-astoriaphotos-00001"
    assert captured["files"] == [str(tmp_path / "photo1.jpg")]
    assert captured["metadata"]["mediatype"] == "image"
    assert captured["metadata"]["collection"] == "test_collection"


def test_upload_row_raises_when_library_returns_failed_response(tmp_path, monkeypatch):
    (tmp_path / "photo1.jpg").write_bytes(b"data")
    row = {
        "identifier": "zztest-astoriaphotos-00001",
        "file": "photo1.jpg",
        "mediatype": "image",
        "title": "First photo",
        "date": "1958",
    }

    def fake_upload(identifier, files, metadata, **kwargs):
        return [FakeResponse(ok=False, status_code=503, text="Service Unavailable")]

    monkeypatch.setattr(internetarchive, "upload", fake_upload)

    with pytest.raises(RuntimeError, match="503"):
        upload_row(row, collection="test_collection", files_dir=tmp_path)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest test_ia_bulk.py -v -k upload_row`
Expected: FAIL with `ImportError: cannot import name 'upload_row'` (and `ModuleNotFoundError: No module named 'internetarchive'` until Step 4's install)

- [ ] **Step 4: Add the dependency**

```
# requirements.txt
internetarchive>=5.0
pytest>=8.0
```

Run: `pip install -r requirements.txt`

- [ ] **Step 5: Implement `upload_row`**

```python
# ia_bulk.py (add `import internetarchive` to imports)
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest test_ia_bulk.py -v -k upload_row`
Expected: PASS (2 passed)

- [ ] **Step 7: Write the failing tests for `update_metadata_row`**

```python
from ia_bulk import update_metadata_row


def test_update_metadata_row_succeeds_when_library_returns_ok_response(monkeypatch):
    row = {"identifier": "zztest-astoriaphotos-00001", "title": "Updated title"}
    captured = {}

    def fake_modify_metadata(identifier, metadata, **kwargs):
        captured["identifier"] = identifier
        captured["metadata"] = metadata
        return FakeResponse(ok=True)

    monkeypatch.setattr(internetarchive, "modify_metadata", fake_modify_metadata)

    update_metadata_row(row)

    assert captured["identifier"] == "zztest-astoriaphotos-00001"
    assert captured["metadata"] == {"title": "Updated title"}


def test_update_metadata_row_raises_when_library_returns_failed_response(monkeypatch):
    row = {"identifier": "zztest-astoriaphotos-00001", "title": "Updated title"}

    def fake_modify_metadata(identifier, metadata, **kwargs):
        return FakeResponse(ok=False, status_code=400, text="Bad Request")

    monkeypatch.setattr(internetarchive, "modify_metadata", fake_modify_metadata)

    with pytest.raises(RuntimeError, match="400"):
        update_metadata_row(row)
```

- [ ] **Step 8: Run tests to verify they fail**

Run: `python -m pytest test_ia_bulk.py -v -k update_metadata_row`
Expected: FAIL with `ImportError: cannot import name 'update_metadata_row'`

- [ ] **Step 9: Implement `update_metadata_row`**

```python
# ia_bulk.py (append)
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
```

- [ ] **Step 10: Run tests to verify they pass**

Run: `python -m pytest test_ia_bulk.py -v -k update_metadata_row`
Expected: PASS (2 passed)

- [ ] **Step 11: Write the failing tests for `validate_identifiers`**

```python
from ia_bulk import validate_identifiers


def test_validate_identifiers_passes_valid_unique_identifiers():
    rows = [
        {"identifier": "lcps-astoriaphotos-00001", "title": "New title"},
        {"identifier": "lcps-astoriaphotos-00002", "title": "Another title"},
    ]

    results = validate_identifiers(rows, registry=make_registry())

    assert all(r.is_valid for r in results)


def test_validate_identifiers_does_not_require_file_or_mediatype():
    rows = [{"identifier": "lcps-astoriaphotos-00001", "title": "New title"}]

    results = validate_identifiers(rows, registry=make_registry())

    assert results[0].is_valid


def test_validate_identifiers_flags_bad_scheme():
    rows = [{"identifier": "not-a-valid-id", "title": "New title"}]

    results = validate_identifiers(rows, registry=make_registry())

    assert not results[0].is_valid
```

- [ ] **Step 12: Run tests to verify they fail**

Run: `python -m pytest test_ia_bulk.py -v -k validate_identifiers`
Expected: FAIL with `ImportError: cannot import name 'validate_identifiers'`

- [ ] **Step 13: Implement `validate_identifiers`**

```python
# ia_bulk.py (append)
def validate_identifiers(rows: list[dict[str, str]], registry: dict) -> list[RowValidation]:
    seen_identifiers: dict[str, int] = {}
    results: list[RowValidation] = []

    for offset, row in enumerate(rows):
        row_number = offset + 2
        identifier = row.get("identifier", "").strip()
        errors = check_identifier(identifier, row_number, registry, seen_identifiers)
        results.append(RowValidation(row_number=row_number, identifier=identifier, errors=errors))

    return results
```

- [ ] **Step 14: Run tests to verify they pass**

Run: `python -m pytest test_ia_bulk.py -v -k validate_identifiers`
Expected: PASS (3 passed)

- [ ] **Step 15: Commit**

```bash
git add ia_bulk.py test_ia_bulk.py requirements.txt
git commit -m "feat: add internetarchive library integration for upload and metadata update"
```

---

### Task 6: `upload` and `sync-metadata` CLI subcommands

**Files:**
- Modify: `ia_bulk.py`
- Test: `test_ia_bulk.py`

**Interfaces:**
- Consumes: `read_rows`, `load_registry`, `validate_rows`, `validate_identifiers`, `format_report`, `check_live_safety`, `chunk_rows`, `open_log`, `log_result`, `load_prior_successes`, `upload_row`, `update_metadata_row`, `TEST_COLLECTION` from prior tasks.
- Produces:
  - `cmd_upload(args) -> int` — expects `args.csv`, `args.files_dir`, `args.registry`, `args.live`, `args.collection`, `args.log_dir`, `args.resume_from` (may be `None`).
  - `cmd_sync_metadata(args) -> int` — expects `args.csv`, `args.registry`, `args.live`, `args.log_dir`, `args.resume_from` (may be `None`).

- [ ] **Step 1: Write the failing tests for `cmd_upload`**

```python
def test_cmd_upload_writes_success_log_and_returns_zero(tmp_path, monkeypatch):
    (tmp_path / "photo1.jpg").write_bytes(b"data")
    csv_path = tmp_path / "items.csv"
    write_csv(
        csv_path,
        ["identifier", "file", "mediatype", "title", "date"],
        [
            {
                "identifier": "zztest-astoriaphotos-00001",
                "file": "photo1.jpg",
                "mediatype": "image",
                "title": "First photo",
                "date": "1958",
            }
        ],
    )
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "zztest", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )
    log_dir = tmp_path / "logs"

    monkeypatch.setattr("ia_bulk.upload_row", lambda row, collection, files_dir: None)

    args = Namespace(
        csv=str(csv_path),
        files_dir=str(tmp_path),
        registry=str(registry_path),
        live=False,
        collection="lcps",
        log_dir=str(log_dir),
        resume_from=None,
    )

    exit_code = cmd_upload(args)

    assert exit_code == 0
    log_files = list(log_dir.glob("upload-*.jsonl"))
    assert len(log_files) == 1
    entry = json.loads(log_files[0].read_text(encoding="utf-8").strip())
    assert entry["identifier"] == "zztest-astoriaphotos-00001"
    assert entry["status"] == "success"


def test_cmd_upload_fails_validation_before_touching_network(tmp_path, monkeypatch):
    csv_path = tmp_path / "items.csv"
    write_csv(
        csv_path,
        ["identifier", "file", "mediatype", "title", "date"],
        [
            {
                "identifier": "zztest-astoriaphotos-00001",
                "file": "missing.jpg",
                "mediatype": "image",
                "title": "First photo",
                "date": "1958",
            }
        ],
    )
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "zztest", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )

    upload_calls = []
    monkeypatch.setattr("ia_bulk.upload_row", lambda row, collection, files_dir: upload_calls.append(row))

    args = Namespace(
        csv=str(csv_path),
        files_dir=str(tmp_path),
        registry=str(registry_path),
        live=False,
        collection="lcps",
        log_dir=str(tmp_path / "logs"),
        resume_from=None,
    )

    exit_code = cmd_upload(args)

    assert exit_code == 1
    assert upload_calls == []


def test_cmd_upload_blocks_real_identifiers_without_live_flag(tmp_path, monkeypatch):
    (tmp_path / "photo1.jpg").write_bytes(b"data")
    csv_path = tmp_path / "items.csv"
    write_csv(
        csv_path,
        ["identifier", "file", "mediatype", "title", "date"],
        [
            {
                "identifier": "lcps-astoriaphotos-00001",
                "file": "photo1.jpg",
                "mediatype": "image",
                "title": "First photo",
                "date": "1958",
            }
        ],
    )
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "lcps", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )

    upload_calls = []
    monkeypatch.setattr("ia_bulk.upload_row", lambda row, collection, files_dir: upload_calls.append(row))

    args = Namespace(
        csv=str(csv_path),
        files_dir=str(tmp_path),
        registry=str(registry_path),
        live=False,
        collection="lcps",
        log_dir=str(tmp_path / "logs"),
        resume_from=None,
    )

    exit_code = cmd_upload(args)

    assert exit_code == 1
    assert upload_calls == []


def test_cmd_upload_resume_from_skips_prior_successes(tmp_path, monkeypatch):
    (tmp_path / "photo1.jpg").write_bytes(b"data")
    (tmp_path / "photo2.jpg").write_bytes(b"data")
    csv_path = tmp_path / "items.csv"
    write_csv(
        csv_path,
        ["identifier", "file", "mediatype", "title", "date"],
        [
            {
                "identifier": "zztest-astoriaphotos-00001",
                "file": "photo1.jpg",
                "mediatype": "image",
                "title": "First photo",
                "date": "1958",
            },
            {
                "identifier": "zztest-astoriaphotos-00002",
                "file": "photo2.jpg",
                "mediatype": "image",
                "title": "Second photo",
                "date": "1958",
            },
        ],
    )
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "zztest", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )
    prior_log = tmp_path / "prior.jsonl"
    log_result(prior_log, "zztest-astoriaphotos-00001", "photo1.jpg", "success")

    uploaded = []
    monkeypatch.setattr(
        "ia_bulk.upload_row", lambda row, collection, files_dir: uploaded.append(row["identifier"])
    )

    args = Namespace(
        csv=str(csv_path),
        files_dir=str(tmp_path),
        registry=str(registry_path),
        live=False,
        collection="lcps",
        log_dir=str(tmp_path / "logs"),
        resume_from=str(prior_log),
    )

    exit_code = cmd_upload(args)

    assert exit_code == 0
    assert uploaded == ["zztest-astoriaphotos-00002"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest test_ia_bulk.py -v -k cmd_upload`
Expected: FAIL with `ImportError: cannot import name 'cmd_upload'`

- [ ] **Step 3: Implement `cmd_upload`**

```python
# ia_bulk.py (append)
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
```

Add `import sys` to the imports at the top of `ia_bulk.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_ia_bulk.py -v -k cmd_upload`
Expected: PASS (4 passed)

- [ ] **Step 5: Write the failing tests for `cmd_sync_metadata`**

```python
def test_cmd_sync_metadata_writes_success_log_and_returns_zero(tmp_path, monkeypatch):
    csv_path = tmp_path / "updates.csv"
    write_csv(csv_path, ["identifier", "title"], [{"identifier": "zztest-astoriaphotos-00001", "title": "Corrected title"}])
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "zztest", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )
    log_dir = tmp_path / "logs"

    monkeypatch.setattr("ia_bulk.update_metadata_row", lambda row: None)

    args = Namespace(
        csv=str(csv_path),
        registry=str(registry_path),
        live=False,
        log_dir=str(log_dir),
        resume_from=None,
    )

    exit_code = cmd_sync_metadata(args)

    assert exit_code == 0
    log_files = list(log_dir.glob("sync-metadata-*.jsonl"))
    assert len(log_files) == 1
    entry = json.loads(log_files[0].read_text(encoding="utf-8").strip())
    assert entry["status"] == "success"


def test_cmd_sync_metadata_does_not_require_file_or_mediatype_columns(tmp_path, monkeypatch):
    csv_path = tmp_path / "updates.csv"
    write_csv(csv_path, ["identifier", "title"], [{"identifier": "zztest-astoriaphotos-00001", "title": "Corrected title"}])
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "zztest", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )

    monkeypatch.setattr("ia_bulk.update_metadata_row", lambda row: None)

    args = Namespace(
        csv=str(csv_path),
        registry=str(registry_path),
        live=False,
        log_dir=str(tmp_path / "logs"),
        resume_from=None,
    )

    exit_code = cmd_sync_metadata(args)

    assert exit_code == 0
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `python -m pytest test_ia_bulk.py -v -k cmd_sync_metadata`
Expected: FAIL with `ImportError: cannot import name 'cmd_sync_metadata'`

- [ ] **Step 7: Implement `cmd_sync_metadata`**

```python
# ia_bulk.py (append)
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
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest test_ia_bulk.py -v -k cmd_sync_metadata`
Expected: PASS (2 passed)

- [ ] **Step 9: Commit**

```bash
git add ia_bulk.py test_ia_bulk.py
git commit -m "feat: add upload and sync-metadata subcommands"
```

---

### Task 7: argparse wiring and `main()` entry point

**Files:**
- Modify: `ia_bulk.py`
- Test: `test_ia_bulk.py`

**Interfaces:**
- Consumes: `cmd_validate`, `cmd_upload`, `cmd_sync_metadata` from prior tasks.
- Produces:
  - `build_parser() -> argparse.ArgumentParser`
  - `main(argv: list[str] | None = None) -> int`
  - `if __name__ == "__main__": sys.exit(main())` at the bottom of the file.

- [ ] **Step 1: Write the failing tests for `build_parser` and `main`**

```python
from ia_bulk import build_parser, main


def test_build_parser_validate_subcommand_defaults():
    parser = build_parser()
    args = parser.parse_args(["validate", "items.csv"])
    assert args.command == "validate"
    assert args.csv == "items.csv"
    assert args.files_dir == "."
    assert args.registry == "projects_registry.json"


def test_build_parser_upload_subcommand_defaults_to_not_live():
    parser = build_parser()
    args = parser.parse_args(["upload", "items.csv"])
    assert args.command == "upload"
    assert args.live is False
    assert args.collection == "lcps"
    assert args.resume_from is None


def test_build_parser_upload_subcommand_accepts_live_and_resume_from():
    parser = build_parser()
    args = parser.parse_args(["upload", "items.csv", "--live", "--resume-from", "logs/upload-x.jsonl"])
    assert args.live is True
    assert args.resume_from == "logs/upload-x.jsonl"


def test_build_parser_sync_metadata_subcommand_defaults():
    parser = build_parser()
    args = parser.parse_args(["sync-metadata", "updates.csv"])
    assert args.command == "sync-metadata"
    assert args.csv == "updates.csv"
    assert args.live is False


def test_main_dispatches_to_cmd_validate(monkeypatch, tmp_path):
    csv_path = tmp_path / "items.csv"
    csv_path.write_text("identifier,file,mediatype,title,date\n", encoding="utf-8")

    calls = []
    monkeypatch.setattr("ia_bulk.cmd_validate", lambda args: calls.append(args.csv) or 0)

    exit_code = main(["validate", str(csv_path)])

    assert exit_code == 0
    assert calls == [str(csv_path)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest test_ia_bulk.py -v -k "build_parser or test_main_dispatches"`
Expected: FAIL with `ImportError: cannot import name 'build_parser'`

- [ ] **Step 3: Implement `build_parser` and `main`**

```python
# ia_bulk.py (append; add `import argparse` and `import sys` to imports if not already present)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest test_ia_bulk.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Manually verify the CLI end-to-end against the sandbox**

Run:
```bash
python ia_bulk.py validate data/sample.csv --files-dir data --registry projects_registry.json
```
Expected: a pass/fail report printed, correct exit code (`echo $?` on POSIX or `echo $LASTEXITCODE` in PowerShell).

- [ ] **Step 6: Commit**

```bash
git add ia_bulk.py test_ia_bulk.py
git commit -m "feat: wire up argparse CLI with validate/upload/sync-metadata subcommands"
```

---

### Task 8: Registry content, docs, and repo hygiene

**Files:**
- Create: `projects_registry.json`
- Create: `docs/ARCHITECTURE.md`
- Create: `.gitignore`
- Modify: `docs/ARCHITECTURE.md` (only file needing content beyond scaffolding)

**Interfaces:**
- Consumes: nothing (this is documentation/config, not code).
- Produces: nothing consumed by other tasks — this is the final task.

- [ ] **Step 1: Create the real project registry**

```json
// projects_registry.json
{
  "collection_key": "lcps",
  "projects": {
    "astoriaphotos": {
      "description": "Astoria historical photos donated collection (Sarah's old photos)"
    }
  }
}
```

Note: confirm the real `collection_key` value against LCPS's actual IA collection identifier before any `--live` run — this placeholder (`lcps`) must be verified, not assumed correct.

- [ ] **Step 2: Create `.gitignore`**

```
# .gitignore
logs/
__pycache__/
*.pyc
.pytest_cache/
```

- [ ] **Step 3: Write `docs/ARCHITECTURE.md`**

```markdown
# IA Bulk Upload CLI — Architecture

## Purpose
Single-script CLI (`ia_bulk.py`) for validating, uploading, and syncing
metadata for Internet Archive items from a CSV exported from the LCPS
Google Sheet. Generic to "a project" so a second LCPS project can reuse
this pipeline — see `projects_registry.json`.

## CSV schemas

### `validate` / `upload`
Required columns: `identifier`, `file`, `mediatype`, `title`, `date`.
All other columns pass through untouched as IA item metadata.

- `identifier`: pre-assigned in the Sheet, permanent, never generated or
  renamed by this tool. Must match `COLLECTIONKEY-PROJECTID-NUMBER`,
  lowercase, hyphen-separated, 5-digit zero-padded NUMBER.
- `file`: filename (optionally with a relative subpath), resolved against
  `--files-dir`.
- `mediatype`, `title`, `date`: required, non-empty.

### `sync-metadata`
Only requires an `identifier` column plus whichever metadata columns
changed. Does not require `file`, `mediatype`, `title`, or `date`.

## Identifier scheme
See `.claude/CLAUDE.md` for the full identifier scheme and project
registry rationale. `projects_registry.json` holds the known
`collection_key` and `PROJECTID` values; `validate` and `sync-metadata`
reject any identifier whose prefix isn't registered there.

Test-collection identifiers replace `COLLECTIONKEY` with the literal
`zztest`, keeping the real `PROJECTID` — e.g. `zztest-astoriaphotos-00001`,
not `zztest-lcps-00001`. `check_identifier` accepts either the real
`collection_key` or `zztest` as the first segment, as long as the
`PROJECTID` segment is registered.

## Chunking
All `upload`/`sync-metadata` runs process rows in batches of 500 (IA's
per-run batch limit), via `chunk_rows()`. This is a pacing/checkpoint
boundary, not a literal separate CSV file per chunk — each row is
uploaded individually through the `internetarchive` Python library so
outcomes are captured per-row.

## Logging and resume
Every `upload`/`sync-metadata` run writes a timestamped JSONL log to
`logs/<command>-<timestamp>.jsonl`, one line per row:
`{identifier, file, status, error, timestamp}`.

`--resume-from <log>` reads identifiers marked `"status": "success"` from
a prior log and skips them. The new run still writes its own complete
log (carrying forward the skipped identifiers as pre-recorded successes),
so each log is a self-contained record of what happened by that point.

## Safety rail
Default target is `test_collection`; `--live` is required to target the
real collection. When not `--live`, every identifier in the CSV must be
prefixed `zztest-`, checked before any network call.
```

- [ ] **Step 4: Verify no code references were left inconsistent**

Run: `python -m pytest test_ia_bulk.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add projects_registry.json docs/ARCHITECTURE.md .gitignore
git commit -m "docs: add architecture notes and project registry"
```
