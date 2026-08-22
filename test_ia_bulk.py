import dataclasses
import json
import csv
import re
from argparse import Namespace
from pathlib import Path

import internetarchive
import pytest
from googleapiclient.errors import HttpError

from column_map import build_column_map
from ia_bulk import (
    read_csv,
    load_registry,
    check_identifier,
    check_required_for_upload,
    resolve_sheet_files,
    validate_rows,
    validate_sheet_grid,
    RowValidation,
    Readiness,
    effective_identifier,
    run_stamp,
    log_result,
    build_parser,
    build_sheet_client,
    format_field_receipt,
    format_lifecycle_summary,
    main,
)
from project_config import ProjectConfig


class FakeResponse:
    def __init__(self, ok, status_code=200, text=""):
        self.ok = ok
        self.status_code = status_code
        self.text = text


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# A fixed stand-in for run_stamp(), threaded into every test that needs a
# deterministic non-live identifier - see docs/DECISIONS.md, "Test
# identifiers carry a per-run stamp". Tests that instead need to prove the
# stamp is computed once per run (not once per row/chunk) monkeypatch
# ia_bulk.run_stamp with their own counting fake instead of this constant.
FIXED_STAMP = "20260819t090000"


def test_read_csv_returns_list_of_dicts(tmp_path):
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

    rows = read_csv(csv_path).rows

    assert rows == [
        {
            "identifier": "lcps-astoriaphotos-00001",
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


def make_registry():
    return {"collection_key": "lcps", "projects": {"astoriaphotos": {}}}


def make_sheet_registry(files_dir=".", **project_overrides):
    """A full project_config-shaped registry, as opposed to make_registry()'s
    bare {collection_key, projects} shell - load_project_config needs every
    REQUIRED_KEYS field populated. sheet_id/test_sheet_id are deliberately
    different values (not "the same string twice") so a test that reads the
    wrong one is distinguishable from one that reads the right one - the
    exact gap the Task 7 review found in the SheetClient tests."""
    project = {
        "mediatype": "image",
        "ia_collection": "lcpsociety",
        "sheet_id": "REAL_SHEET_ID",
        "test_sheet_id": "TEST_SHEET_ID",
        "sheet_tab": "Sheet1",
        "files_dir": files_dir,
        "file_template": "{file}",
        "required_for_upload": ["title"],
    }
    project.update(project_overrides)
    return {"collection_key": "lcps", "projects": {"astoriaphotos": project}}


def _sheet_config(**overrides) -> ProjectConfig:
    """A ProjectConfig with the same defaults as make_sheet_registry(), for
    tests that need the dataclass itself (e.g. to call a function that takes
    ProjectConfig directly) rather than a registry dict routed through
    load_project_config(). Every field is overridable, not only
    required_for_upload - files_dir and file_template are exercised by other
    tasks in this same plan that reuse this helper.

    Built via dataclasses.replace() on a fully-typed base rather than
    ProjectConfig(**defaults): unpacking a plain dict mixing str and
    tuple[str] values makes every field's type just "str | tuple[str]" to a
    type checker, which cannot narrow any individual parameter at the `**`
    unpack - ProjectConfig is a frozen dataclass, so replace() is both the
    idiomatic way to override a subset of fields and the one that keeps each
    field's real type intact."""
    base = ProjectConfig(
        project_id="astoriaphotos",
        collection_key="lcps",
        mediatype="image",
        ia_collection="lcpsociety",
        sheet_id="REAL_SHEET_ID",
        test_sheet_id="TEST_SHEET_ID",
        sheet_tab="Sheet1",
        files_dir=".",
        file_template="{file}",
        required_for_upload=("title",),
    )
    return dataclasses.replace(base, **overrides)


class FakeSheetClient:
    """Stands in for sheet_client.SheetClient in cmd_validate tests -
    build_sheet_client is the one seam those tests monkeypatch, so nothing
    here ever touches google_auth or googleapiclient.discovery."""

    def __init__(self, grid):
        self._grid = grid

    def read_grid(self):
        return self._grid


class RaisingSheetClient:
    """Stands in for SheetClient when a test needs read_grid() to raise -
    e.g. an HttpError from a wrong tab name or a not-yet-shared Sheet."""

    def __init__(self, exc):
        self._exc = exc

    def read_grid(self):
        raise self._exc


def make_http_error(message="Unable to parse range: Sheet1", status=400):
    """A realistic HttpError, as googleapiclient actually raises it: the
    real .resp needs a .status and .reason, and .content must be the raw
    JSON error body Google's API returns, not a plain string."""

    class _FakeHttpResponse:
        def __init__(self, status, reason):
            self.status = status
            self.reason = reason

    content = json.dumps({"error": {"message": message}}).encode("utf-8")
    return HttpError(
        _FakeHttpResponse(status, reason="Bad Request"),
        content,
        uri="https://sheets.googleapis.com/v4/spreadsheets/TEST_SHEET_ID/values/Sheet1",
    )


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


def test_check_identifier_rejects_zztest_prefix_since_csv_always_holds_real_identifiers():
    # The CSV's identifier column always holds the real, permanent
    # identifier — "zztest-" prefixing is applied automatically by
    # effective_identifier() at network-call time, never authored in the CSV.
    errors = check_identifier(
        "zztest-astoriaphotos-00001", row_number=2, registry=make_registry(), seen_identifiers={}
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


def test_check_identifier_column_name_defaults_to_identifier_for_the_csv_path():
    """Pins the CSV path's wording exactly - column_name existing as a
    parameter must not change what the CSV path's messages say."""
    errors = check_identifier(
        "LCPS_astoriaphotos_1", row_number=2, registry=make_registry(), seen_identifiers={}
    )
    assert errors == [
        "identifier 'LCPS_astoriaphotos_1' does not match scheme COLLECTIONKEY-PROJECTID-NUMBER"
    ]


def test_check_identifier_names_the_column_it_checked():
    """A Sheet with both a donor 'Identifier' column (untouched metadata)
    and 'ia_identifier' (the tool's minted one) needs its error messages to
    say WHICH column is wrong - the bare word 'identifier' is ambiguous
    between the two and sends a volunteer to fix the wrong one."""
    errors = check_identifier(
        "lcps-astoriaphotos-00001",
        row_number=5,
        registry=make_registry(),
        seen_identifiers={"lcps-astoriaphotos-00001": 2},
        column_name="ia_identifier",
    )
    assert errors == ["ia_identifier 'lcps-astoriaphotos-00001' duplicates row 2"]


def test_check_identifier_names_the_column_for_a_blank_value_too():
    errors = check_identifier(
        "", row_number=2, registry=make_registry(), seen_identifiers={}, column_name="ia_identifier"
    )
    assert errors == ["missing required column 'ia_identifier'"]


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


def test_validate_rows_does_not_require_date(tmp_path):
    (tmp_path / "photo1.jpg").write_bytes(b"fake-image-bytes")
    rows = [
        {
            "identifier": "lcps-astoriaphotos-00001",
            "file": "photo1.jpg",
            "mediatype": "image",
            "title": "First photo",
            "date": "",
        }
    ]

    results = validate_rows(rows, files_dir=tmp_path, registry=make_registry())

    assert results[0].is_valid


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


def test_validate_rows_skips_checks_but_keeps_row_numbers_for_skip_identifiers():
    rows = [
        {
            # would otherwise fail every check - already validated + uploaded
            # by a prior run, so re-checking it on --resume-from is wasted work
            "identifier": "lcps-astoriaphotos-00001",
            "file": "does-not-exist.jpg",
            "mediatype": "",
            "title": "",
            "date": "",
        },
        {
            "identifier": "lcps-astoriaphotos-00002",
            "file": "does-not-exist.jpg",
            "mediatype": "",
            "title": "",
            "date": "",
        },
    ]

    results = validate_rows(
        rows, files_dir="/tmp", registry=make_registry(), skip_identifiers=frozenset({"lcps-astoriaphotos-00001"})
    )

    assert results[0].is_valid
    assert results[0].row_number == 2
    assert not results[1].is_valid
    assert results[1].row_number == 3


def valid_row(**overrides) -> dict:
    row: dict = {
        "identifier": "lcps-astoriaphotos-00001",
        "file": "photo1.jpg",
        "mediatype": "image",
        "title": "First photo",
    }
    row.update(overrides)
    return row


def test_validate_rows_flags_a_row_with_more_fields_than_the_header(tmp_path):
    # csv.DictReader collects surplus fields under the None restkey. Left
    # unchecked this crashes upload_row with "'list' object has no attribute
    # 'strip'" partway through a run.
    (tmp_path / "photo1.jpg").write_bytes(b"x")
    row = valid_row()
    row[None] = ["surplus value"]

    results = validate_rows([row], files_dir=tmp_path, registry=make_registry())

    assert not results[0].is_valid
    assert any("more fields than the header" in e for e in results[0].errors)


def test_validate_rows_flags_a_row_with_fewer_fields_than_the_header(tmp_path):
    # A short row means the header and the data disagree about column
    # positions, so every value after the gap is attributed to the wrong
    # field. This is what a comma inside an unquoted header produces.
    (tmp_path / "photo1.jpg").write_bytes(b"x")
    rows = [valid_row(addresses=None)]

    results = validate_rows(rows, files_dir=tmp_path, registry=make_registry())

    assert not results[0].is_valid
    assert any("fewer fields than the header" in e for e in results[0].errors)


def test_validate_rows_names_the_column_a_short_row_is_missing(tmp_path):
    (tmp_path / "photo1.jpg").write_bytes(b"x")
    rows = [valid_row(addresses=None)]

    results = validate_rows(rows, files_dir=tmp_path, registry=make_registry())

    assert any("addresses" in e for e in results[0].errors)


def test_validate_rows_accepts_a_row_whose_trailing_cell_is_merely_empty(tmp_path):
    # An empty cell is "" and is fine; only None means the field was absent.
    (tmp_path / "photo1.jpg").write_bytes(b"x")
    rows = [valid_row(addresses="")]

    results = validate_rows(rows, files_dir=tmp_path, registry=make_registry())

    assert results[0].is_valid


def test_validate_rows_default_required_columns_still_requires_identifier_and_file(tmp_path):
    """Pins validate_rows' required_columns default at REQUIRED_UPLOAD_COLUMNS
    - the CSV path, unchanged - since introducing that parameter for the
    Sheet path means the CSV path's own correctness now depends on a
    default value rather than on hardcoded behavior. If the default were
    ever flipped to SHEET_REQUIRED_COLUMNS (which excludes identifier
    only), a CSV row with both blank would become "valid" for identifier,
    and effective_identifier("", live=False, stamp) returns just
    "zztest-<stamp>-" - not a real identifier."""
    rows = [
        {
            "identifier": "",
            "file": "",
            "mediatype": "image",
            "title": "First photo",
            "date": "1958",
        }
    ]

    results = validate_rows(rows, files_dir=tmp_path, registry=make_registry())

    assert not results[0].is_valid
    assert "missing required column 'identifier'" in results[0].errors
    assert "missing required column 'file'" in results[0].errors


def test_validate_rows_identifier_column_lets_the_sheet_path_read_ia_identifier(tmp_path):
    """After Task 9 the Sheet's own 'identifier' column holds a donor
    reference like 'CD 1 01 53 58 1 Central SS', not a minted IA
    identifier - running check_identifier's COLLECTIONKEY-PROJECTID-NUMBER
    regex against it would fail every row for the wrong reason.
    identifier_column lets the Sheet path point validate_rows at
    'ia_identifier' instead; the CSV path's default ('identifier') is
    pinned separately above and is untouched by this parameter existing."""
    (tmp_path / "photo1.jpg").write_bytes(b"x")
    rows = [
        {
            "identifier": "CD 1 01 53 58 1 Central SS",
            "ia_identifier": "",
            "file": "photo1.jpg",
            "mediatype": "image",
            "title": "First photo",
        }
    ]

    results = validate_rows(
        rows,
        files_dir=tmp_path,
        registry=make_registry(),
        required_columns=("mediatype", "title", "file"),
        identifier_column="ia_identifier",
    )

    assert results[0].is_valid
    assert results[0].identifier == ""


def test_check_header_accepts_a_clean_header():
    from ia_bulk import check_header

    assert check_header(["identifier", "file", "mediatype", "title", "date", "Theme"]) == []


def test_check_header_rejects_a_column_with_surrounding_whitespace():
    # "Place " uploads a metadata field literally named "Place ".
    from ia_bulk import check_header

    errors = check_header(["identifier", "file", "mediatype", "title", "Place "])

    assert any("Place " in e and "whitespace" in e for e in errors)


def test_check_header_rejects_duplicate_columns():
    from ia_bulk import check_header

    errors = check_header(["identifier", "file", "mediatype", "title", "Theme", "Theme"])

    assert any("duplicate" in e and "Theme" in e for e in errors)


def test_check_header_rejects_a_capitalized_variant_of_a_known_column():
    # A "Date" column is passed through as an arbitrary field while the
    # lowercase "date" upload_row reads stays empty, so the item gets both
    # Date=1958 and date=[n.d.].
    from ia_bulk import check_header

    errors = check_header(["identifier", "file", "mediatype", "title", "Date"])

    assert any("Date" in e and "date" in e for e in errors)


def test_check_header_rejects_an_empty_header():
    from ia_bulk import check_header

    assert check_header([]) != []


def test_check_header_does_not_object_to_unknown_columns():
    from ia_bulk import check_header

    assert check_header(["identifier", "file", "mediatype", "title", "Notes (LCPS Internal)"]) == []


def test_read_csv_returns_fieldnames_alongside_rows(tmp_path):
    from ia_bulk import read_csv

    csv_path = tmp_path / "items.csv"
    csv_path.write_text(
        "identifier,file,mediatype,title\nlcps-astoriaphotos-00001,a.jpg,image,First\n",
        encoding="utf-8",
    )

    data = read_csv(csv_path)

    assert data.fieldnames == ["identifier", "file", "mediatype", "title"]
    assert data.rows == [
        {
            "identifier": "lcps-astoriaphotos-00001",
            "file": "a.jpg",
            "mediatype": "image",
            "title": "First",
        }
    ]


def write_raw_csv(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_cmd_validate_fails_on_a_header_with_a_capitalized_known_column(tmp_path, capsys):
    (tmp_path / "a.jpg").write_bytes(b"x")
    csv_path = write_raw_csv(
        tmp_path / "items.csv",
        "identifier,file,mediatype,title,Date\nlcps-astoriaphotos-00001,a.jpg,image,First,1958\n",
    )
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(make_registry()), encoding="utf-8")

    from ia_bulk import cmd_validate

    exit_code = cmd_validate(
        Namespace(csv=str(csv_path), files_dir=str(tmp_path), registry=str(registry_path))
    )

    assert exit_code == 1
    assert "must be lowercase 'date'" in capsys.readouterr().out


def test_cmd_validate_fails_on_an_unquoted_comma_in_the_header(tmp_path, capsys):
    # The real failure from data/upload.csv: "Names (Last, First M.)" splits
    # into two header cells, so the header is one field longer than the row
    # and every later column is attributed to the wrong field.
    (tmp_path / "a.jpg").write_bytes(b"x")
    csv_path = write_raw_csv(
        tmp_path / "items.csv",
        "identifier,file,mediatype,title,Names (Last, First M.),addresses\n"
        "lcps-astoriaphotos-00001,a.jpg,image,First,,600 Marine Dr.\n",
    )
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(make_registry()), encoding="utf-8")

    from ia_bulk import cmd_validate

    exit_code = cmd_validate(
        Namespace(csv=str(csv_path), files_dir=str(tmp_path), registry=str(registry_path))
    )

    assert exit_code == 1
    assert "fewer fields than the header" in capsys.readouterr().out


def test_cmd_upload_refuses_a_bad_header_before_touching_the_network(tmp_path, monkeypatch):
    (tmp_path / "a.jpg").write_bytes(b"x")
    csv_path = write_raw_csv(
        tmp_path / "items.csv",
        "identifier,file,mediatype,title,Date\nlcps-astoriaphotos-00001,a.jpg,image,First,1958\n",
    )
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(make_registry()), encoding="utf-8")

    # run_rows catches broad Exception, so a raising fake would be swallowed
    # and logged as a row failure - recording the call is the only way to
    # prove the network was never reached.
    calls = []

    def record_call(*args, **kwargs):
        calls.append(args)
        return []

    monkeypatch.setattr(internetarchive, "upload", record_call)

    from ia_bulk import cmd_upload

    exit_code = cmd_upload(
        Namespace(
            csv=str(csv_path),
            files_dir=str(tmp_path),
            registry=str(registry_path),
            live=False,
            collection="lcps",
            log_dir=str(tmp_path / "logs"),
            resume_from=None,
        )
    )

    assert exit_code == 1
    assert calls == []


def test_cmd_sync_metadata_refuses_a_bad_header_before_touching_the_network(tmp_path, monkeypatch):
    csv_path = write_raw_csv(
        tmp_path / "updates.csv",
        "identifier,Title\nlcps-astoriaphotos-00001,Renamed\n",
    )
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(make_registry()), encoding="utf-8")

    calls = []

    def record_call(*args, **kwargs):
        calls.append(args)
        return FakeResponse(ok=True)

    monkeypatch.setattr(internetarchive, "modify_metadata", record_call)

    from ia_bulk import cmd_sync_metadata

    exit_code = cmd_sync_metadata(
        Namespace(
            csv=str(csv_path),
            registry=str(registry_path),
            live=False,
            log_dir=str(tmp_path / "logs"),
            resume_from=None,
        )
    )

    assert exit_code == 1
    assert calls == []


def test_format_report_attributes_header_errors_to_row_1():
    from ia_bulk import format_report

    report = format_report([RowValidation(row_number=1, identifier="", errors=["CSV has no header row"])])

    assert "row 1" in report
    assert "CSV has no header row" in report


def test_format_report_shows_pass_and_fail_with_summary():
    from ia_bulk import format_report

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


def test_format_report_does_not_duplicate_the_row_number_for_a_blank_identifier():
    """A blank identifier is the normal state of an unassigned Sheet row
    (RowState.UNASSIGNED), not a special case worth restating the row
    number for - "[PASS] row 2 (row 2)" said nothing "[PASS] row 2" didn't
    already say, and reads as a bug on every passing Sheet row."""
    from ia_bulk import format_report

    report = format_report([RowValidation(row_number=2, identifier="", errors=[])])

    assert "[PASS] row 2" in report
    assert "(row 2)" not in report


def test_cmd_validate_returns_zero_when_all_rows_valid(tmp_path, capsys):
    from ia_bulk import cmd_validate

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
    from ia_bulk import cmd_validate

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


def test_field_receipt_lists_uploadable_fields_and_held_back_ones():
    column_map = build_column_map(
        ["Title", "Genre / Form", "Notes (LCPS Internal)", "identifier"]
    )

    receipt = format_field_receipt(column_map)

    assert "title" in receipt
    assert "genre_form" in receipt
    assert "held back" in receipt
    assert "Notes (LCPS Internal)" in receipt
    # reserved columns are never uploaded, so they must not read as fields
    assert "identifier," not in receipt


def test_sheet_structure_validation_files_a_grid_shape_error_under_its_own_row_not_row_1():
    """check_grid_shape's message already names the real row number (e.g.
    "row 3 has..."); filing it under row 1 regardless - which an earlier
    version of this function did - puts a row-3 problem under the heading a
    volunteer reads as "the header row"."""
    from ia_bulk import sheet_structure_validation

    grid = [
        ["Title", "file"],
        ["First", "photo1.jpg"],
        ["Second", "photo2.jpg", "unexpected extra cell"],
    ]
    column_map = build_column_map(grid[0])

    results = sheet_structure_validation(column_map, grid)

    assert len(results) == 1
    assert results[0].row_number == 3
    assert "more field(s) than the header" in results[0].errors[0]


def test_sheet_structure_validation_files_a_header_collision_under_row_1():
    """Two headers colliding is genuinely a header-level problem - not
    about any one data row - so it stays under row 1."""
    from ia_bulk import sheet_structure_validation

    grid = [["Genre / Form", "Genre_ Form"], ["a", "b"]]
    column_map = build_column_map(grid[0])

    results = sheet_structure_validation(column_map, grid)

    assert len(results) == 1
    assert results[0].row_number == 1
    assert "genre_form" in results[0].errors[0]


def test_sheet_structure_validation_files_a_header_problem_and_a_shape_problem_separately():
    """Both defects can be present in the same Sheet at once, and must be
    filed under their own distinct rows rather than merged into a single
    row-1 entry."""
    from ia_bulk import sheet_structure_validation

    grid = [
        ["Genre / Form", "Genre_ Form", "Title"],
        ["a", "b", "First"],
        ["c", "d", "Second", "unexpected extra cell"],
    ]
    column_map = build_column_map(grid[0])

    results = sheet_structure_validation(column_map, grid)

    by_row = {result.row_number: result.errors[0] for result in results}
    assert set(by_row) == {1, 3}
    assert "genre_form" in by_row[1]
    assert "more field(s) than the header" in by_row[3]


def test_lifecycle_summary_counts_each_state():
    rows = [
        {"ia_identifier": "", "ia_uploaded": ""},
        {"ia_identifier": "", "ia_uploaded": ""},
        {"ia_identifier": "lcps-astoriaphotos-00001", "ia_uploaded": ""},
        {"ia_identifier": "lcps-astoriaphotos-00002", "ia_uploaded": "2026-08-08T10:00:00"},
    ]
    # row_results must line up 1:1 with rows, in order; all pass here so
    # this test pins pure lifecycle counting, independent of the
    # fails-validation case pinned separately below.
    row_results = [RowValidation(row_number=i + 2, identifier="") for i in range(len(rows))]

    summary = format_lifecycle_summary(rows, row_results)

    assert "2 rows ready to upload" in summary
    assert "1 already uploaded" in summary
    assert "1 reserved but unconfirmed" in summary


def test_lifecycle_summary_uses_singular_row_for_a_count_of_one():
    rows = [{"ia_identifier": "", "ia_uploaded": ""}]
    row_results = [RowValidation(row_number=2, identifier="")]

    summary = format_lifecycle_summary(rows, row_results)

    assert "1 row ready to upload" in summary
    assert "1 rows ready to upload" not in summary


def test_lifecycle_summary_does_not_count_a_failed_unassigned_row_as_ready():
    """The bug the coordinator caught: classify_row() alone can't see
    validation results, so a row with a blank identifier that actually
    failed validation (missing title, say) was counted as "ready to
    upload" right next to a report saying that same row failed. Counts
    must be cross-referenced against row_results, and a failed-but-
    unassigned row must be called out separately rather than folded into
    either bucket silently."""
    rows = [{"ia_identifier": "", "ia_uploaded": ""}]
    row_results = [
        RowValidation(row_number=2, identifier="", errors=["missing required column 'title'"])
    ]

    summary = format_lifecycle_summary(rows, row_results)

    assert "0 rows ready to upload" in summary
    assert "1 row" in summary and "failed validation" in summary


def test_lifecycle_summary_does_not_count_a_failed_done_row_as_already_uploaded():
    """Same contradiction as the UNASSIGNED case, in the DONE bucket: a row
    classify_row() calls DONE (has both identifier and ia_uploaded) but that
    now fails validation (a duplicate identifier, say) is not cleanly
    "already uploaded" - it needs a human to look, not silent inclusion in
    a bucket that implies everything is fine."""
    rows = [{"ia_identifier": "lcps-astoriaphotos-00001", "ia_uploaded": "2026-08-08T10:00:00"}]
    row_results = [
        RowValidation(
            row_number=2,
            identifier="lcps-astoriaphotos-00001",
            errors=["identifier 'lcps-astoriaphotos-00001' duplicates row 5"],
        )
    ]

    summary = format_lifecycle_summary(rows, row_results)

    assert "0 already uploaded" in summary
    assert "1 row already uploaded but now fail validation" in summary


def test_lifecycle_summary_does_not_count_a_failed_reserved_row_as_will_retry():
    """The coordinator's exact example: rows with a duplicate identifier or
    an unregistered project prefix were reported as "3 reserved but
    unconfirmed - will retry under existing identifier" - a forward-looking
    promise a row failing identifier validation cannot keep."""
    rows = [{"ia_identifier": "lcps-astoriaphotos-00001", "ia_uploaded": ""}]
    row_results = [
        RowValidation(
            row_number=2,
            identifier="lcps-astoriaphotos-00001",
            errors=["identifier 'lcps-astoriaphotos-00001' duplicates row 5"],
        )
    ]

    summary = format_lifecycle_summary(rows, row_results)

    assert "0 reserved but unconfirmed" in summary
    assert "1 row reserved but invalid" in summary
    assert "will NOT retry automatically" in summary


def test_lifecycle_summary_counts_always_sum_to_the_total_row_count():
    """Every row falls into exactly one of the six buckets (three
    classify_row() states, each split into valid/invalid), so their counts
    must always add up to len(rows) - a regression that double-counts or
    drops a row on some branch would break this without necessarily
    breaking any single-bucket assertion."""
    rows = [
        {"ia_identifier": "", "ia_uploaded": ""},
        {"ia_identifier": "", "ia_uploaded": ""},
        {"ia_identifier": "lcps-astoriaphotos-00001", "ia_uploaded": ""},
        {"ia_identifier": "lcps-astoriaphotos-00002", "ia_uploaded": "2026-08-08T10:00:00"},
    ]
    row_results = [
        RowValidation(row_number=2, identifier="", errors=[]),
        RowValidation(row_number=3, identifier="", errors=["missing required column 'title'"]),
        RowValidation(
            row_number=4,
            identifier="lcps-astoriaphotos-00001",
            errors=["identifier 'lcps-astoriaphotos-00001' duplicates row 9"],
        ),
        RowValidation(row_number=5, identifier="lcps-astoriaphotos-00002", errors=[]),
    ]

    summary = format_lifecycle_summary(rows, row_results)

    counts = [int(match) for match in re.findall(r"^(\d+)", summary, flags=re.MULTILINE)]
    assert sum(counts) == len(rows)


def test_lifecycle_summary_raises_on_mismatched_lengths_instead_of_silently_truncating():
    """zip(rows, row_results) truncates to the shorter list without
    raising - a caller passing the wrong list (e.g. the combined report
    instead of just the row results) would silently get wrong-but-
    plausible-looking counts instead of an obvious failure. That is exactly
    the kind of silent wrongness this function exists to prevent, so a
    length mismatch must raise rather than zip quietly."""
    with pytest.raises(ValueError, match="same length"):
        format_lifecycle_summary(
            rows=[{"identifier": ""}, {"identifier": ""}],
            row_results=[RowValidation(row_number=2, identifier="")],
        )


class _RecordingSheetsValues:
    """Records the exact spreadsheetId/range passed to values().get(), so a
    test can tell a client reading the correct Sheet apart from one reading
    a hardcoded-wrong one - the gap the Task 7 review found (all 7 of that
    task's original tests stayed green even with the wrong spreadsheetId
    hardcoded into both SheetClient methods)."""

    def __init__(self, response):
        self.get_calls = []
        self._response = response

    def get(self, spreadsheetId, range):
        self.get_calls.append((spreadsheetId, range))
        return _RecordingExecutable(self._response)


class _RecordingExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _RecordingSheetsService:
    def __init__(self, response):
        self.values_api = _RecordingSheetsValues(response)

    def spreadsheets(self):
        return self

    def values(self):
        return self.values_api


def test_build_sheet_client_reads_the_real_sheet_id_when_live(monkeypatch):
    fake_service = _RecordingSheetsService({"values": [["Title"]]})
    monkeypatch.setattr("ia_bulk.google_auth.load_credentials", lambda *a, **k: "FAKE_CREDS")
    monkeypatch.setattr("ia_bulk.googleapiclient.discovery.build", lambda *a, **k: fake_service)
    config = ProjectConfig(
        project_id="astoriaphotos",
        collection_key="lcps",
        mediatype="image",
        ia_collection="lcpsociety",
        sheet_id="REAL_SHEET_ID",
        test_sheet_id="TEST_SHEET_ID",
        sheet_tab="Donor Photos",
        files_dir=".",
        file_template="{file}",
        required_for_upload=("title",),
    )

    client = build_sheet_client(config, live=True)
    client.read_grid()

    assert fake_service.values_api.get_calls == [("REAL_SHEET_ID", "Donor Photos")]


def test_build_sheet_client_reads_the_test_sheet_id_when_not_live(monkeypatch):
    fake_service = _RecordingSheetsService({"values": [["Title"]]})
    monkeypatch.setattr("ia_bulk.google_auth.load_credentials", lambda *a, **k: "FAKE_CREDS")
    monkeypatch.setattr("ia_bulk.googleapiclient.discovery.build", lambda *a, **k: fake_service)
    config = ProjectConfig(
        project_id="astoriaphotos",
        collection_key="lcps",
        mediatype="image",
        ia_collection="lcpsociety",
        sheet_id="REAL_SHEET_ID",
        test_sheet_id="TEST_SHEET_ID",
        sheet_tab="Donor Photos",
        files_dir=".",
        file_template="{file}",
        required_for_upload=("title",),
    )

    client = build_sheet_client(config, live=False)
    client.read_grid()

    assert fake_service.values_api.get_calls == [("TEST_SHEET_ID", "Donor Photos")]


def test_build_sheet_client_passes_credentials_through_to_discovery_build(monkeypatch):
    captured = {}

    def fake_load_credentials(token_path, client_secrets_path, interactive):
        captured["token_path"] = token_path
        captured["client_secrets_path"] = client_secrets_path
        captured["interactive"] = interactive
        return "FAKE_CREDS"

    def fake_build(api, version, credentials):
        captured["api"] = api
        captured["version"] = version
        captured["credentials"] = credentials
        return _RecordingSheetsService({"values": []})

    monkeypatch.setattr("ia_bulk.google_auth.load_credentials", fake_load_credentials)
    monkeypatch.setattr("ia_bulk.googleapiclient.discovery.build", fake_build)
    config = ProjectConfig(
        project_id="astoriaphotos",
        collection_key="lcps",
        mediatype="image",
        ia_collection="lcpsociety",
        sheet_id="REAL_SHEET_ID",
        test_sheet_id="TEST_SHEET_ID",
        sheet_tab="Sheet1",
        files_dir=".",
        file_template="{file}",
        required_for_upload=("title",),
    )

    build_sheet_client(config, live=True)

    assert captured["credentials"] == "FAKE_CREDS"
    assert captured["api"] == "sheets"
    assert captured["version"] == "v4"


def test_cmd_validate_reads_the_sheet_and_injects_mediatype_when_csv_is_omitted(
    tmp_path, monkeypatch, capsys
):
    """Proves mandatory addition #2: mediatype is never a Sheet column, so
    without injecting it from the registry this row would fail with
    "missing required column 'mediatype'" and the exit code/pass-count
    assertions below would flip. Phase 2 (Task 9) requires the file to
    actually resolve on disk, so files_dir points at a real directory
    containing the named file - a regression that broke resolution would
    fail this test too."""
    from ia_bulk import cmd_validate

    (tmp_path / "photo1.jpg").write_bytes(b"x")
    grid = [["Title", "file"], ["First photo", "photo1.jpg"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "1/1 rows passed" in out
    assert "will upload these metadata fields:" in out
    assert "1 row ready to upload" in out
    assert "suggestions (advisory - nothing is changed automatically):" in out


def test_cmd_validate_does_not_treat_a_blank_ia_identifier_as_an_error(tmp_path, monkeypatch, capsys):
    """A blank ia_identifier is the normal starting state of every new
    Sheet row under minting, not an error like a pre-assigned CSV
    identifier - this is the behavior SHEET_REQUIRED_COLUMNS (which
    excludes ia_identifier) exists to produce. Uses 'ia_identifier', not
    'identifier': after Task 9 the latter is ordinary donor metadata."""
    from ia_bulk import cmd_validate

    (tmp_path / "photo1.jpg").write_bytes(b"x")
    grid = [["Title", "file", "ia_identifier"], ["First photo", "photo1.jpg", ""]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "missing required column" not in out


def test_cmd_validate_does_not_scheme_check_a_donor_identifier_column_and_says_it_will_not_ship(
    tmp_path, monkeypatch, capsys
):
    """The real Sheet's own 'Identifier' column holds the donor's archival
    reference (e.g. 'CD 1 01 53 58 1 Central SS'), not a minted IA identifier,
    so it must NOT be checked against the COLLECTIONKEY-PROJECTID-NUMBER
    scheme, which it would never match.

    It also must not be advertised as a field that will upload. It never
    does - upload_row strips the `identifier` key because that name is
    Internet Archive's own item identifier - and this test previously asserted
    the receipt listed it among the uploadable fields, which pinned the lie in
    place. A receipt an operator learns to disbelieve is worse than none."""
    from ia_bulk import cmd_validate

    (tmp_path / "photo1.jpg").write_bytes(b"x")
    grid = [
        ["Title", "file", "Identifier"],
        ["First photo", "photo1.jpg", "CD 1 01 53 58 1 Central SS"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "1/1 rows passed" in out
    assert "does not match scheme" not in out
    assert "will upload these metadata fields:\n  title\n" in out
    assert "NOT uploaded - Internet Archive reserves these names:\n  identifier" in out


def test_cmd_validate_names_ia_identifier_not_identifier_in_a_duplicate_error(
    tmp_path, monkeypatch, capsys
):
    """A Sheet with BOTH a donor 'Identifier' column (distinct values,
    ordinary metadata) and a duplicated 'ia_identifier' (the tool's minted
    one) must say which column the duplicate is actually in - a bare
    'identifier' would be ambiguous between the two and send a volunteer to
    edit the wrong cell."""
    from ia_bulk import cmd_validate

    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"x")
    grid = [
        ["Title", "file", "Identifier", "ia_identifier"],
        ["First", "a.jpg", "Donor Ref A", "lcps-astoriaphotos-00001"],
        ["Second", "b.jpg", "Donor Ref B", "lcps-astoriaphotos-00001"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "ia_identifier 'lcps-astoriaphotos-00001' duplicates row 2" in out


def test_cmd_validate_fails_a_row_whose_file_cannot_be_resolved_with_the_resolvers_message(
    tmp_path, monkeypatch, capsys
):
    """Phase 2's reversal of Task 8's Phase 1 exemption: the disk check is
    back, and a row whose file_template candidate matches nothing on disk
    must fail with the resolver's OWN message (naming the folder and the
    name it looked for), not the generic disk-check 'file not found' or
    'missing required column' that would say the same thing without
    naming what was actually searched for.

    Distinguishing resolve_file()'s own wording ("no file found in ...
    matching ...") from the generic checks matters: without it, this test
    would pass even if cmd_validate silently discarded the resolver's
    message and fell back to validate_rows' plain disk-existence check,
    which would produce a superficially similar-looking failure by
    accident (the raw, never-verified Sheet cell value happens to embed
    the same folder/name substrings) rather than by design."""
    from ia_bulk import cmd_validate

    (tmp_path / "SOP CD1").mkdir()  # folder exists, the named file does not
    grid = [["Title", "file"], ["First photo", "SOP CD1/Nothing Here"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "[FAIL] row 2" in out
    assert "no file found in" in out
    assert "SOP CD1" in out and "Nothing Here" in out
    assert "file not found:" not in out


def test_cmd_validate_fails_on_an_ambiguous_file_naming_both_candidates(tmp_path, monkeypatch, capsys):
    """Two files sharing a stem (a JPEG and a TIFF master, say) must never be
    picked between silently - an item's identifier is permanent - so the
    row fails, and the report names both candidates."""
    from ia_bulk import cmd_validate

    folder = tmp_path / "SOP CD5"
    folder.mkdir()
    (folder / "Liberty.jpg").write_bytes(b"x")
    (folder / "Liberty.tif").write_bytes(b"x")

    grid = [["Title", "file"], ["Liberty", "SOP CD5/Liberty"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "Liberty.jpg" in out and "Liberty.tif" in out


def test_cmd_validate_fails_fast_when_file_template_names_a_column_the_sheet_lacks(
    tmp_path, monkeypatch, capsys
):
    """A file_template referencing a column absent from the Sheet's actual
    header row (a registry typo, or a Sheet whose columns changed) must be
    caught once at startup via check_file_template, not surfaced as the
    same resolution failure repeated on every single row."""
    from ia_bulk import cmd_validate

    grid = [["Title"], ["First photo"]]  # no "file" column at all
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "file_template" in err
    assert "'file'" in err


def test_cmd_validate_resolves_a_filename_missing_its_extension_and_writes_identifier_bib(
    tmp_path, monkeypatch
):
    """225 of 234 real rows have no extension in the Sheet. resolve_file()
    finds the actual file on disk, and both row['file'] and
    row['ia_identifier_bib'] must end up holding the RESOLVED name (with
    its real extension) - not the Sheet's literal cell value."""
    from ia_bulk import cmd_validate

    (tmp_path / "Alderbrook Hall.jpg").write_bytes(b"x")
    captured_rows = []

    def fake_validate_rows(rows, files_dir, registry, **kwargs):
        captured_rows.extend(rows)
        return [RowValidation(row_number=i + 2, identifier="") for i in range(len(rows))]

    monkeypatch.setattr("ia_bulk.validate_rows", fake_validate_rows)
    grid = [["Title", "file"], ["Alderbrook Hall", "Alderbrook Hall"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    cmd_validate(args)

    assert captured_rows[0]["file"] == "Alderbrook Hall.jpg"
    assert captured_rows[0]["ia_identifier_bib"] == "Alderbrook Hall.jpg"


def test_cmd_validate_resolves_using_a_two_column_file_template_like_the_real_registry(
    tmp_path, monkeypatch, capsys
):
    """projects_registry.json's real file_template joins two Sheet columns
    ('{file_on_array}/{identifier}'), not one - proves cmd_validate's
    wiring isn't accidentally hardcoded to a single-placeholder template."""
    from ia_bulk import cmd_validate

    folder = tmp_path / "SOP CD1"
    folder.mkdir()
    (folder / "CD 1 01 53 58 1 Central SS.jpg").write_bytes(b"x")

    grid = [
        ["Title", "File on Array", "Identifier"],
        ["Central School", "SOP CD1", "CD 1 01 53 58 1 Central SS"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            make_sheet_registry(files_dir=str(tmp_path), file_template="{file_on_array}/{identifier}")
        ),
        encoding="utf-8",
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "1/1 rows passed" in out


def test_cmd_validate_flags_colliding_sheet_headers(tmp_path, monkeypatch, capsys):
    """Proves mandatory addition #1: check_column_map is wired into the
    report. mediatype/title are satisfied and the Sheet path never checks
    file existence, so the ONLY possible source of a failure is the header
    collision itself."""
    from ia_bulk import cmd_validate

    grid = [
        ["Genre / Form", "Genre_ Form", "file", "Title"],
        ["a", "b", "photo1.jpg", "First"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "both normalize to field name 'genre_form'" in out


def test_cmd_validate_flags_a_sheet_row_longer_than_the_header(tmp_path, monkeypatch, capsys):
    """Proves mandatory addition #1: check_grid_shape is wired into the
    report - AND that the problem is filed under the row it's actually
    about (row 2, the single data row here), not under row 1. A volunteer
    reads "row 1" as the header row; a row-2 problem filed there is
    confusing even though the message text itself already names row 2."""
    from ia_bulk import cmd_validate

    grid = [
        ["Title", "file"],
        ["First", "photo1.jpg", "unexpected extra cell"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "more field(s) than the header" in out
    assert "[FAIL] row 2" in out
    assert "[FAIL] row 1" not in out


def test_cmd_validate_reports_a_header_collision_and_a_shape_error_under_their_own_rows(
    tmp_path, monkeypatch, capsys
):
    """Both a genuinely header-level defect (a collision) and a specific
    row's shape defect can be present in the same Sheet at once, and must
    be filed under their own distinct rows rather than merged into a single
    row-1 entry."""
    from ia_bulk import cmd_validate

    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"x")
    grid = [
        ["Genre / Form", "Genre_ Form", "Title", "file"],
        ["a", "b", "First", "a.jpg"],
        ["c", "d", "Second", "b.jpg", "unexpected extra cell"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "[FAIL] row 1" in out
    assert "[FAIL] row 3" in out
    assert "both normalize to field name 'genre_form'" in out
    assert "more field(s) than the header" in out


def status_lines(out: str) -> list[str]:
    """The "[PASS]/[FAIL] row N" headings of a report, in printed order -
    without their indented error lines. Lets a test assert the exact set,
    order and verdict of the rows reported, which a substring check on any
    single heading cannot: a row reported twice with opposite verdicts
    satisfies both `"[FAIL] row 3" in out` and `"[PASS] row 3" in out`."""
    return [line for line in out.splitlines() if line.startswith("[")]


def test_cmd_validate_reports_a_long_row_once_as_failed_not_twice_with_opposite_verdicts(
    tmp_path, monkeypatch, capsys
):
    """A structural problem with a specific data row must be folded into
    that row's own result, not reported as a second, parallel entry beside
    it. Filing it separately made a long row print twice with contradicting
    verdicts ("[FAIL] row 3" from check_grid_shape, "[PASS] row 3" from
    validate_rows), inflated the "N/M rows passed" denominator past the
    number of data rows the Sheet actually has, and left the row counted as
    "ready to upload" in the lifecycle summary - a failing row promised as
    uploadable, printed directly beneath the report failing it.

    Row 3 is long but otherwise complete (title present, identifier blank
    which is normal, no header defect anywhere), so the shape error is the
    only possible source of a failure and the only possible source of an
    entry outside the three data rows."""
    from ia_bulk import cmd_validate

    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"x")
    (tmp_path / "c.jpg").write_bytes(b"x")
    grid = [
        ["Title", "Date", "file"],
        ["First photo", "1912", "a.jpg"],
        ["Second photo", "1913", "b.jpg", "unexpected extra cell"],
        ["Third photo", "1914", "c.jpg"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    # each row exactly once, in order, under its own number - no duplicate
    # row 3 and no phantom row-1 entry standing in for a data row's problem
    assert status_lines(out) == ["[PASS] row 2", "[FAIL] row 3", "[PASS] row 4"]
    # the error is filed under row 3's heading, not merely mentioned somewhere
    assert "[FAIL] row 3\n    - row 3 has 1 more field(s) than the header" in out
    # the denominator is the real number of data rows (3), not 3 + one
    # parallel structural entry
    assert "2/3 rows passed" in out
    # and the failing row is counted as failed, not as ready to upload
    assert "2 rows ready to upload (no identifier yet)" in out
    assert "1 row not yet assigned an identifier but failed validation" in out


def test_cmd_validate_keeps_a_header_level_error_under_row_1_when_the_last_data_row_is_also_long(
    tmp_path, monkeypatch, capsys
):
    """The companion hazard to folding per-row structural errors into
    row_results: a header-level entry is row 1, and `row_results[1 - 2]` is
    `row_results[-1]` - Python's negative indexing would quietly file a
    header collision under the LAST data row and drop the row-1 heading
    entirely. Here the last data row is ALSO long, so a wrong fold lands the
    collision on a row that already fails for its own reason and would
    otherwise look plausible."""
    from ia_bulk import cmd_validate

    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"x")
    grid = [
        ["Genre / Form", "Genre_ Form", "Title", "file"],  # colliding headers -> a header-level error
        ["a", "b", "First photo", "a.jpg"],
        ["c", "d", "Second photo", "b.jpg", "unexpected extra cell"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert status_lines(out) == ["[FAIL] row 1", "[PASS] row 2", "[FAIL] row 3"]
    assert "[FAIL] row 1\n    - columns 'Genre / Form' and 'Genre_ Form' both normalize" in out
    assert "[FAIL] row 3\n    - row 3 has 1 more field(s) than the header" in out


@pytest.mark.parametrize("live", [True, False])
def test_cmd_validate_passes_live_flag_and_project_config_through_to_build_sheet_client(
    tmp_path, monkeypatch, live
):
    """Coordinator-flagged CRITICAL gap: only the live=True case was ever
    exercised, so cmd_validate could have been hardcoded to
    build_sheet_client(config, True) - always reading the REAL Sheet
    regardless of --live - and every one of the 167 tests at the time would
    still have passed, because every other Sheet test's stub discards
    `live` entirely. Parametrizing over both values is what pins the
    argument actually flows through, in both directions, rather than one
    direction happening to be right by coincidence."""
    from ia_bulk import cmd_validate

    captured = {}

    def fake_build_sheet_client(config, live):
        captured["live"] = live
        captured["project_id"] = config.project_id
        return FakeSheetClient([["Title", "file"], []])

    monkeypatch.setattr("ia_bulk.build_sheet_client", fake_build_sheet_client)

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=live)

    cmd_validate(args)

    assert captured["live"] is live
    assert captured["project_id"] == "astoriaphotos"


def test_cmd_validate_rejects_an_unknown_project_before_touching_the_sheet(tmp_path, monkeypatch):
    """load_project_config's ConfigError must not be swallowed - an unknown
    --project has to fail loudly rather than silently reading nothing."""
    from ia_bulk import cmd_validate
    from project_config import ConfigError

    calls = []
    monkeypatch.setattr(
        "ia_bulk.build_sheet_client", lambda config, live: calls.append(config) or FakeSheetClient([])
    )

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(make_sheet_registry()), encoding="utf-8")
    args = Namespace(csv=None, project="nosuchproject", registry=str(registry_path), live=False)

    with pytest.raises(ConfigError, match="nosuchproject"):
        cmd_validate(args)

    assert calls == []


def test_cmd_validate_rejects_an_unreplaced_placeholder_sheet_id_before_touching_the_network(
    tmp_path, monkeypatch, capsys
):
    """projects_registry.json ships sheet_id/test_sheet_id as
    REPLACE_WITH_* until a human edits in the real Google Sheet ID. Asking
    Google about a placeholder produces an opaque 404/permission error that
    doesn't say what to fix; catching it before the network call and naming
    the registry file directly is what makes the first-run failure
    actionable."""
    from ia_bulk import cmd_validate

    def _must_not_reach_the_network(config, live):
        raise AssertionError("build_sheet_client must not be called for an unreplaced placeholder")

    monkeypatch.setattr("ia_bulk.build_sheet_client", _must_not_reach_the_network)

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(test_sheet_id="REPLACE_WITH_TEST_SHEET_ID")), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "REPLACE_WITH_TEST_SHEET_ID" in err
    assert str(registry_path) in err


def test_cmd_validate_turns_an_http_error_reading_the_sheet_into_an_actionable_message(
    tmp_path, monkeypatch, capsys
):
    """A wrong tab name (or an unshared/deleted Sheet) surfaces from the API
    as googleapiclient.errors.HttpError, e.g. "Unable to parse range:
    Sheet1" - which by itself doesn't tell anyone to go edit 'sheet_tab' in
    the registry. This must be caught and turned into a message naming the
    spreadsheet ID, the tab, and the registry file, not left as a raw
    traceback."""
    from ia_bulk import cmd_validate

    monkeypatch.setattr(
        "ia_bulk.build_sheet_client",
        lambda config, live: RaisingSheetClient(make_http_error("Unable to parse range: Sheet1")),
    )

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(sheet_tab="Sheet1")), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "TEST_SHEET_ID" in err
    assert "Sheet1" in err
    assert str(registry_path) in err
    assert "sheet_tab" in err


def test_cmd_validate_flags_a_header_only_sheet_as_an_error_not_a_false_green(
    tmp_path, monkeypatch, capsys
):
    """An empty read is far more likely to mean a wrong tab, an unpopulated
    copy of the Sheet, or a Sheet never actually shared with the service
    account than a real project with zero rows - reporting "0/0 rows
    passed" and exiting 0 would be a false green from a command whose
    entire job is catching exactly this kind of problem. And a "0/1 rows
    passed" line must never appear here either - the "1" would be a
    synthetic entry standing in for zero real rows, which reads as
    nonsense arithmetic."""
    from ia_bulk import cmd_validate

    grid = [["Title", "file"]]  # header row only, zero data rows
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "no data rows" in out
    assert "rows passed" not in out


def test_cmd_validate_flags_a_completely_empty_sheet_as_an_error(tmp_path, monkeypatch, capsys):
    from ia_bulk import cmd_validate

    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient([]))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "no data rows" in out
    assert "rows passed" not in out


def test_cmd_validate_still_reports_a_header_collision_when_the_sheet_has_no_data_rows(
    tmp_path, monkeypatch, capsys
):
    """The no-data-rows short-circuit must not swallow a genuine header
    problem - a colliding header is worth surfacing even on an otherwise
    empty Sheet, since fixing it is a prerequisite to populating the Sheet
    correctly in the first place."""
    from ia_bulk import cmd_validate

    grid = [["Genre / Form", "Genre_ Form"]]  # colliding header, zero data rows
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "both normalize to field name 'genre_form'" in out
    assert "no data rows" in out
    assert "rows passed" not in out


def test_cmd_validate_prints_test_mode_and_the_test_sheet_id_by_default(tmp_path, monkeypatch, capsys):
    """Run mode is this project's core safety design, so it - and exactly
    which spreadsheet/tab back it - must be visible in the output, not just
    implied by which flags were passed on the command line."""
    from ia_bulk import cmd_validate

    grid = [["Title"], ["First"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"), sheet_tab="Donor Photos")
        ),
        encoding="utf-8",
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    cmd_validate(args)
    out = capsys.readouterr().out

    assert "test mode" in out
    assert "TEST_SHEET_ID" in out
    assert "REAL_SHEET_ID" not in out
    assert "Donor Photos" in out


def test_cmd_validate_prints_live_mode_and_the_real_sheet_id_when_live(tmp_path, monkeypatch, capsys):
    from ia_bulk import cmd_validate

    grid = [["Title"], ["First"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"), sheet_tab="Donor Photos")
        ),
        encoding="utf-8",
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=True)

    cmd_validate(args)
    out = capsys.readouterr().out

    assert "live mode" in out
    assert "REAL_SHEET_ID" in out
    assert "TEST_SHEET_ID" not in out


def test_cmd_validate_injects_mediatype_from_the_registry_not_a_hardcoded_value(tmp_path, monkeypatch):
    """Every existing fixture happens to use mediatype="image", so a
    hardcoded row["mediatype"] = "image" would pass all of them - proving
    only "some non-empty value", never "from ProjectConfig.mediatype".
    mediatype is permanent on IA once uploaded and is never printed
    anywhere, so nothing else in this suite would catch a wrong source -
    a distinctive fixture value plus capturing the actual row dict passed
    to validate_rows is the only way to pin it."""
    from ia_bulk import cmd_validate

    captured_rows = []

    def fake_validate_rows(rows, files_dir, registry, **kwargs):
        captured_rows.extend(rows)
        return [RowValidation(row_number=i + 2, identifier="") for i in range(len(rows))]

    monkeypatch.setattr("ia_bulk.validate_rows", fake_validate_rows)
    (tmp_path / "photo1.jpg").write_bytes(b"x")
    grid = [["Title", "file"], ["First photo", "photo1.jpg"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path), mediatype="phonorecord")),
        encoding="utf-8",
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    cmd_validate(args)

    assert captured_rows == [
        {
            "title": "First photo",
            "file": "photo1.jpg",
            "mediatype": "phonorecord",
            "ia_identifier_bib": "photo1.jpg",
        }
    ]


def test_cmd_validate_passes_only_the_row_results_to_the_lifecycle_summary_not_the_combined_report(
    tmp_path, monkeypatch
):
    """Coordinator-caught gap (3a): passing the combined `results` list
    (which also carries sheet_structure_validation()'s row-1/shape
    entries) instead of validate_rows()'s own row_results would misalign
    zip(rows, row_results) - silently, since zip() truncates rather than
    raising. A Sheet with BOTH a structural error (a header collision) and
    a normal data row is what makes the combined list a different LENGTH
    than `rows`, so this is pinned by inspecting exactly what cmd_validate
    hands to format_lifecycle_summary, not by relying on the length guard
    added to format_lifecycle_summary itself to happen to fire."""
    from ia_bulk import cmd_validate

    captured = {}

    def fake_format_lifecycle_summary(rows, row_results):
        captured["rows"] = rows
        captured["row_results"] = row_results
        return "captured"

    monkeypatch.setattr("ia_bulk.format_lifecycle_summary", fake_format_lifecycle_summary)

    (tmp_path / "a.jpg").write_bytes(b"x")
    grid = [
        ["Genre / Form", "Genre_ Form", "Title", "file"],  # colliding headers -> a structural error
        ["a", "b", "First photo", "a.jpg"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    cmd_validate(args)

    assert len(captured["rows"]) == 1
    assert len(captured["row_results"]) == 1


def test_cmd_validate_prints_correct_lifecycle_counts_for_a_mix_of_states_end_to_end(
    tmp_path, monkeypatch, capsys
):
    """End-to-end version of 3a/3b: a real Sheet with one row in each of
    the six (state x valid/invalid) combinations, run through the actual
    cmd_validate/validate_rows/format_lifecycle_summary wiring - not just
    format_lifecycle_summary in isolation, which was already correct on its
    own and is exactly why the coordinator's mutations (fabricated
    all-valid results; the combined report instead of row_results) slipped
    past every previous test: the only existing end-to-end summary
    assertion was on an all-passing run, where every mutation happens to
    look identical to correct behavior."""
    from ia_bulk import cmd_validate

    for name in ("ready.jpg", "done.jpg", "reserved.jpg"):
        (tmp_path / name).write_bytes(b"x")

    # Uses "ia_identifier", not "identifier": after Task 9 the latter is
    # ordinary donor metadata and no longer drives classify_row() at all.
    #
    # The "fails validation" row in each pair names a FILE that resolves to
    # nothing (missing*.jpg is deliberately never created above), not a
    # blank title - since the SHEET_REQUIRED_COLUMNS shrink, a blank title
    # is a readiness fact rather than a validation error, so it can no
    # longer stand in for "this row is invalid" here.
    grid = [
        ["Title", "file", "ia_identifier", "ia_uploaded"],
        ["Ready", "ready.jpg", "", ""],
        ["Ready but broken", "missing.jpg", "", ""],  # unresolvable file -> fails, still unassigned
        ["Done", "done.jpg", "lcps-astoriaphotos-00001", "2026-01-01T00:00:00"],
        ["Done but broken", "missing2.jpg", "lcps-astoriaphotos-00002", "2026-01-01T00:00:00"],  # unresolvable file -> fails
        ["Reserved", "reserved.jpg", "lcps-astoriaphotos-00003", ""],
        ["Reserved but broken", "missing3.jpg", "lcps-astoriaphotos-00004", ""],  # unresolvable file -> fails
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "3/6 rows passed" in out
    assert "1 row ready to upload (no identifier yet)" in out
    assert "1 row not yet assigned an identifier but failed validation" in out
    assert "1 already uploaded" in out
    assert "1 row already uploaded but now fail validation" in out
    assert "1 reserved but unconfirmed - will retry under existing identifier" in out
    assert "1 row reserved but invalid" in out


def test_cmd_validate_output_encodes_cleanly_under_a_restrictive_windows_console_codepage(
    tmp_path, monkeypatch, capsys
):
    """Finding 5 (fix round 2) replaced an em dash in ia_fields.py with an
    ASCII hyphen, but nothing pinned it - restoring the em dash would pass
    every other test in this suite. cp437 is the default codepage on many
    non-UTF-8 Windows consoles; encoding the full validate output as plain
    ascii (a stricter test - anything ascii-safe is cp437-safe too) is what
    would have caught it, since a character that can't encode there raises
    UnicodeEncodeError and truncates the human's report mid-run on exactly
    the machine this task hands off to."""
    from ia_bulk import cmd_validate

    (tmp_path / "photo1.jpg").write_bytes(b"x")
    grid = [["Title", "Photographer", "file"], ["First photo", "Jane Doe", "photo1.jpg"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    cmd_validate(args)
    out = capsys.readouterr().out

    # The row must actually reach the suggestions section (not short-circuit
    # on a template/resolution error) for this to be a meaningful check of
    # the FULL report's encoding, not just the banner line's.
    assert "suggestions (advisory - nothing is changed automatically):" in out
    out.encode("ascii")  # raises UnicodeEncodeError if any non-ASCII character slipped in


def test_cmd_validate_prints_an_actual_suggestion_not_just_the_heading(tmp_path, monkeypatch, capsys):
    """Only the "suggestions (advisory ...)" heading was previously
    asserted anywhere - deleting the loop that prints each suggestion,
    keeping only the heading, passed every test. This is one of the three
    things the Phase 1 handoff session exists to exercise."""
    from ia_bulk import cmd_validate

    (tmp_path / "photo1.jpg").write_bytes(b"x")
    grid = [["Title", "Photographer", "file"], ["First photo", "Jane Doe", "photo1.jpg"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    cmd_validate(args)
    out = capsys.readouterr().out

    assert "photographer" in out
    assert "creator" in out


def test_cmd_validate_treats_an_empty_csv_flag_as_an_explicit_csv_path_not_the_sheet_path(monkeypatch):
    """--csv "" must stay on the CSV branch (and fail there, opening a file
    named "") rather than silently falling through to the Sheet path
    because an empty string is falsy - `if csv_path:` was the bug,
    `if csv_path is not None:` is the fix. Guarding build_sheet_client to
    raise AssertionError if reached, and asserting specifically
    FileNotFoundError (not a bare Exception), proves this fails for the
    CSV-branch reason and not by tripping the guard."""
    from ia_bulk import cmd_validate

    def _must_not_reach_the_sheet_path(config, live):
        raise AssertionError("--csv '' must stay on the CSV path, not fall through to the Sheet")

    monkeypatch.setattr("ia_bulk.build_sheet_client", _must_not_reach_the_sheet_path)

    args = Namespace(
        csv="", files_dir=".", registry="projects_registry.json", project="astoriaphotos", live=False
    )

    with pytest.raises(FileNotFoundError):
        cmd_validate(args)


def test_chunk_rows_splits_into_groups_of_chunk_size():
    from ia_bulk import chunk_rows

    rows = [{"n": i} for i in range(1250)]

    chunks = list(chunk_rows(rows, chunk_size=500))

    assert [len(c) for c in chunks] == [500, 500, 250]
    assert chunks[0][0] == {"n": 0}
    assert chunks[2][-1] == {"n": 1249}


def test_chunk_rows_handles_empty_list():
    from ia_bulk import chunk_rows

    assert list(chunk_rows([], chunk_size=500)) == []


def test_open_log_creates_log_dir_and_returns_timestamped_path(tmp_path):
    from ia_bulk import open_log

    log_dir = tmp_path / "logs"

    log_path = open_log(log_dir, "upload")

    assert log_dir.is_dir()
    assert log_path.parent == log_dir
    assert log_path.name.startswith("upload-")
    assert log_path.suffix == ".jsonl"


def test_log_result_appends_one_json_line(tmp_path):
    from ia_bulk import log_result

    log_path = tmp_path / "upload-test.jsonl"

    log_result(log_path, "lcps-astoriaphotos-00001", "photo1.jpg", "success", live=False)
    log_result(log_path, "lcps-astoriaphotos-00002", "photo2.jpg", "failure", live=False, error="timeout")

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
    from ia_bulk import log_result, load_prior_successes

    log_path = tmp_path / "upload-test.jsonl"
    log_result(log_path, "lcps-astoriaphotos-00001", "photo1.jpg", "success", live=False)
    log_result(log_path, "lcps-astoriaphotos-00002", "photo2.jpg", "failure", live=False, error="timeout")

    successes = load_prior_successes(log_path, live=False)

    assert successes == {"lcps-astoriaphotos-00001"}


def test_load_prior_successes_ignores_entries_from_the_other_mode(tmp_path):
    from ia_bulk import log_result, load_prior_successes

    log_path = tmp_path / "upload-test.jsonl"
    log_result(log_path, "lcps-astoriaphotos-00001", "photo1.jpg", "success", live=False)
    log_result(log_path, "lcps-astoriaphotos-00002", "photo2.jpg", "success", live=True)

    assert load_prior_successes(log_path, live=False) == {"lcps-astoriaphotos-00001"}
    assert load_prior_successes(log_path, live=True) == {"lcps-astoriaphotos-00002"}


def test_load_prior_successes_ignores_pre_migration_entries_with_no_live_field(tmp_path):
    from ia_bulk import load_prior_successes

    log_path = tmp_path / "upload-test.jsonl"
    log_path.write_text(
        json.dumps({"identifier": "lcps-astoriaphotos-00001", "status": "success"}) + "\n",
        encoding="utf-8",
    )

    assert load_prior_successes(log_path, live=False) == set()
    assert load_prior_successes(log_path, live=True) == set()


def test_effective_identifier_prepends_zztest_and_the_given_stamp_when_not_live():
    """See docs/DECISIONS.md, "Test identifiers carry a per-run stamp" - a
    bare zztest- prefix made a test run's identifiers a pure function of the
    real ones, so a fresh Sheet (always minting from 00001) reproduced the
    exact identifiers of a prior run and collided with darkened items."""
    assert (
        effective_identifier("lcps-astoriaphotos-00001", live=False, stamp="20260819t144907")
        == "zztest-20260819t144907-lcps-astoriaphotos-00001"
    )


def test_effective_identifier_ignores_the_stamp_and_returns_identifier_unchanged_when_live():
    """The single most important property this task can produce: a --live
    identifier is the permanent public address of an archival item and must
    stay a pure function of the Sheet/CSV. A stamp leaking into it would be
    worse than not doing this task at all - so this is asserted with the
    stamp parameter actually supplied (not omitted), proving the live branch
    receives it and still ignores it, rather than merely never being asked to
    handle it."""
    assert (
        effective_identifier("lcps-astoriaphotos-00001", live=True, stamp="20260819t144907")
        == "lcps-astoriaphotos-00001"
    )


def test_effective_identifier_applies_one_given_stamp_identically_across_different_identifiers():
    """A run mints many identifiers but must compute run_stamp() only once -
    this pins effective_identifier's half of that contract: handed the SAME
    stamp twice, as a real run would, it embeds that exact stamp in both
    results rather than anything call-order-dependent."""
    stamp = "20260819t144907"
    first = effective_identifier("lcps-astoriaphotos-00001", live=False, stamp=stamp)
    second = effective_identifier("lcps-astoriaphotos-00002", live=False, stamp=stamp)
    assert first == "zztest-20260819t144907-lcps-astoriaphotos-00001"
    assert second == "zztest-20260819t144907-lcps-astoriaphotos-00002"


def test_run_stamp_is_lowercase_and_ia_identifier_safe():
    """IA identifiers are restricted to lowercase letters, digits, underscore,
    period and hyphen. run_stamp() is spliced directly between two literal
    hyphens in effective_identifier(), so anything outside [a-z0-9] in the
    stamp itself (uppercase, a colon from a naive isoformat(), whitespace)
    would produce a malformed identifier."""
    assert re.fullmatch(r"[a-z0-9]+", run_stamp())


def test_effective_identifier_requires_stamp_to_be_passed_explicitly():
    """`stamp` is a required parameter, not a defaulted one - see
    docs/DECISIONS.md, "Test identifiers carry a per-run stamp": a default
    would leave every call site free to silently fall back to it, which
    reopens exactly the collision this task exists to close. Calling with
    only `identifier` and `live` must raise TypeError rather than quietly
    succeeding."""
    with pytest.raises(TypeError):
        effective_identifier("lcps-astoriaphotos-00001", live=False)  # type: ignore[call-arg]


def test_upload_row_succeeds_when_library_returns_ok_responses(tmp_path, monkeypatch):
    from ia_bulk import upload_row

    (tmp_path / "photo1.jpg").write_bytes(b"data")
    row = {
        "identifier": "lcps-astoriaphotos-00001",
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
        captured["kwargs"] = kwargs
        return [FakeResponse(ok=True)]

    monkeypatch.setattr(internetarchive, "upload", fake_upload)

    upload_row(row, target_identifier="zztest-lcps-astoriaphotos-00001", collection="test_collection", files_dir=tmp_path)

    assert captured["identifier"] == "zztest-lcps-astoriaphotos-00001"
    assert captured["files"] == [str(tmp_path / "photo1.jpg")]
    assert captured["metadata"]["mediatype"] == "image"
    assert captured["metadata"]["collection"] == "test_collection"
    assert "identifier" not in captured["metadata"]
    assert captured["kwargs"]["verbose"] is True
    assert captured["kwargs"]["checksum"] is True


def test_upload_row_raises_when_library_returns_failed_response(tmp_path, monkeypatch):
    from ia_bulk import upload_row

    (tmp_path / "photo1.jpg").write_bytes(b"data")
    row = {
        "identifier": "lcps-astoriaphotos-00001",
        "file": "photo1.jpg",
        "mediatype": "image",
        "title": "First photo",
        "date": "1958",
    }

    def fake_upload(identifier, files, metadata, **kwargs):
        return [FakeResponse(ok=False, status_code=503, text="Service Unavailable")]

    monkeypatch.setattr(internetarchive, "upload", fake_upload)

    with pytest.raises(RuntimeError, match="503"):
        upload_row(row, target_identifier="zztest-lcps-astoriaphotos-00001", collection="test_collection", files_dir=tmp_path)


def test_upload_row_defaults_blank_date_to_undated_placeholder(tmp_path, monkeypatch):
    from ia_bulk import upload_row

    (tmp_path / "photo1.jpg").write_bytes(b"data")
    row = {
        "identifier": "lcps-astoriaphotos-00001",
        "file": "photo1.jpg",
        "mediatype": "image",
        "title": "First photo",
        "date": "",
    }
    captured = {}

    def fake_upload(identifier, files, metadata, **kwargs):
        captured["metadata"] = metadata
        return [FakeResponse(ok=True)]

    monkeypatch.setattr(internetarchive, "upload", fake_upload)

    upload_row(row, target_identifier="zztest-lcps-astoriaphotos-00001", collection="test_collection", files_dir=tmp_path)

    assert captured["metadata"]["date"] == "[n.d.]"


def test_upload_row_defaults_missing_date_key_to_undated_placeholder(tmp_path, monkeypatch):
    """csv.DictReader sets a trailing column to None (not "") when a data
    row is short that column entirely, e.g. a ragged hand-edited CSV row
    that ends before the optional trailing 'date' cell."""
    from ia_bulk import upload_row

    (tmp_path / "photo1.jpg").write_bytes(b"data")
    row = {
        "identifier": "lcps-astoriaphotos-00001",
        "file": "photo1.jpg",
        "mediatype": "image",
        "title": "First photo",
        "date": None,
    }
    captured = {}

    def fake_upload(identifier, files, metadata, **kwargs):
        captured["metadata"] = metadata
        return [FakeResponse(ok=True)]

    monkeypatch.setattr(internetarchive, "upload", fake_upload)

    upload_row(row, target_identifier="zztest-lcps-astoriaphotos-00001", collection="test_collection", files_dir=tmp_path)

    assert captured["metadata"]["date"] == "[n.d.]"


def test_upload_row_preserves_free_form_date_when_present(tmp_path, monkeypatch):
    from ia_bulk import upload_row

    (tmp_path / "photo1.jpg").write_bytes(b"data")
    row = {
        "identifier": "lcps-astoriaphotos-00001",
        "file": "photo1.jpg",
        "mediatype": "image",
        "title": "First photo",
        "date": "circa 1930",
    }
    captured = {}

    def fake_upload(identifier, files, metadata, **kwargs):
        captured["metadata"] = metadata
        return [FakeResponse(ok=True)]

    monkeypatch.setattr(internetarchive, "upload", fake_upload)

    upload_row(row, target_identifier="zztest-lcps-astoriaphotos-00001", collection="test_collection", files_dir=tmp_path)

    assert captured["metadata"]["date"] == "circa 1930"


def test_update_metadata_row_succeeds_when_library_returns_ok_response(monkeypatch):
    from ia_bulk import update_metadata_row

    row = {"identifier": "lcps-astoriaphotos-00001", "title": "Updated title"}
    captured = {}

    def fake_modify_metadata(identifier, metadata, **kwargs):
        captured["identifier"] = identifier
        captured["metadata"] = metadata
        return FakeResponse(ok=True)

    monkeypatch.setattr(internetarchive, "modify_metadata", fake_modify_metadata)

    update_metadata_row(row, target_identifier="zztest-lcps-astoriaphotos-00001")

    assert captured["identifier"] == "zztest-lcps-astoriaphotos-00001"
    assert captured["metadata"] == {"title": "Updated title"}


def test_update_metadata_row_drops_blank_cells_instead_of_clearing_the_field(monkeypatch):
    """A blank cell must mean 'leave this field alone', not 'clear it',
    since a sync-metadata CSV only lists the columns that changed."""
    from ia_bulk import update_metadata_row

    row = {"identifier": "lcps-astoriaphotos-00001", "title": "Updated title", "description": ""}
    captured = {}

    def fake_modify_metadata(identifier, metadata, **kwargs):
        captured["metadata"] = metadata
        return FakeResponse(ok=True)

    monkeypatch.setattr(internetarchive, "modify_metadata", fake_modify_metadata)

    update_metadata_row(row, target_identifier="zztest-lcps-astoriaphotos-00001")

    assert "description" not in captured["metadata"]


def test_update_metadata_row_passes_remove_tag_through_to_clear_a_field(monkeypatch):
    """REMOVE_TAG is the internetarchive library's (and the official `ia`
    CLI's) sentinel value for deleting an existing metadata field - it must
    not be filtered out the way a blank cell is."""
    from ia_bulk import update_metadata_row

    row = {"identifier": "lcps-astoriaphotos-00001", "description": "REMOVE_TAG"}
    captured = {}

    def fake_modify_metadata(identifier, metadata, **kwargs):
        captured["metadata"] = metadata
        return FakeResponse(ok=True)

    monkeypatch.setattr(internetarchive, "modify_metadata", fake_modify_metadata)

    update_metadata_row(row, target_identifier="zztest-lcps-astoriaphotos-00001")

    assert captured["metadata"]["description"] == "REMOVE_TAG"


def test_update_metadata_row_raises_when_library_returns_failed_response(monkeypatch):
    from ia_bulk import update_metadata_row

    row = {"identifier": "lcps-astoriaphotos-00001", "title": "Updated title"}

    def fake_modify_metadata(identifier, metadata, **kwargs):
        return FakeResponse(ok=False, status_code=400, text="Bad Request")

    monkeypatch.setattr(internetarchive, "modify_metadata", fake_modify_metadata)

    with pytest.raises(RuntimeError, match="400"):
        update_metadata_row(row, target_identifier="zztest-lcps-astoriaphotos-00001")


def test_update_metadata_row_raises_metadata_unchanged_when_ia_reports_no_changes(monkeypatch):
    from ia_bulk import update_metadata_row, MetadataUnchanged

    row = {"identifier": "lcps-astoriaphotos-00001", "title": "Same title"}

    def fake_modify_metadata(identifier, metadata, **kwargs):
        return FakeResponse(
            ok=False,
            status_code=400,
            text=json.dumps({"success": False, "error": "no changes to _meta.xml"}),
        )

    monkeypatch.setattr(internetarchive, "modify_metadata", fake_modify_metadata)

    with pytest.raises(MetadataUnchanged):
        update_metadata_row(row, target_identifier="zztest-lcps-astoriaphotos-00001")


def test_validate_identifiers_passes_valid_unique_identifiers():
    from ia_bulk import validate_identifiers

    rows = [
        {"identifier": "lcps-astoriaphotos-00001", "title": "New title"},
        {"identifier": "lcps-astoriaphotos-00002", "title": "Another title"},
    ]

    results = validate_identifiers(rows, registry=make_registry())

    assert all(r.is_valid for r in results)


def test_validate_identifiers_skips_checks_but_keeps_row_numbers_for_skip_identifiers():
    from ia_bulk import validate_identifiers

    rows = [
        {"identifier": "lcps-unregisteredproject-00001", "title": "New title"},
        {"identifier": "lcps-astoriaphotos-00002", "title": "Another title"},
    ]

    results = validate_identifiers(
        rows, registry=make_registry(), skip_identifiers=frozenset({"lcps-unregisteredproject-00001"})
    )

    assert results[0].is_valid
    assert results[0].row_number == 2
    assert results[1].is_valid
    assert results[1].row_number == 3


def test_validate_identifiers_does_not_require_file_or_mediatype():
    from ia_bulk import validate_identifiers

    rows = [{"identifier": "lcps-astoriaphotos-00001", "title": "New title"}]

    results = validate_identifiers(rows, registry=make_registry())

    assert results[0].is_valid


def test_validate_identifiers_flags_bad_scheme():
    from ia_bulk import validate_identifiers

    rows = [{"identifier": "not-a-valid-id", "title": "New title"}]

    results = validate_identifiers(rows, registry=make_registry())

    assert not results[0].is_valid


def test_cmd_upload_prints_per_row_progress_and_summary(tmp_path, monkeypatch, capsys):
    from ia_bulk import cmd_upload

    (tmp_path / "photo1.jpg").write_bytes(b"data")
    (tmp_path / "photo2.jpg").write_bytes(b"data")
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
            },
            {
                "identifier": "lcps-astoriaphotos-00002",
                "file": "photo2.jpg",
                "mediatype": "image",
                "title": "Second photo",
                "date": "1958",
            },
        ],
    )
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "lcps", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )

    def fake_upload_row(row, target_identifier, collection, files_dir):
        if row["identifier"].strip() == "lcps-astoriaphotos-00002":
            raise RuntimeError("boom")

    monkeypatch.setattr("ia_bulk.upload_row", fake_upload_row)
    monkeypatch.setattr("ia_bulk.run_stamp", lambda: FIXED_STAMP)

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

    out = capsys.readouterr().out
    assert exit_code == 1
    assert f"[1/2] uploading zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001 (photo1.jpg)" in out
    assert f"[2/2] uploading zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002 (photo2.jpg)" in out
    assert "1 file(s) uploaded successfully, 1 error(s)" in out


def test_cmd_upload_writes_success_log_with_test_prefixed_target_when_not_live(tmp_path, monkeypatch):
    from ia_bulk import cmd_upload

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
    log_dir = tmp_path / "logs"

    monkeypatch.setattr("ia_bulk.upload_row", lambda row, target_identifier, collection, files_dir: None)
    monkeypatch.setattr("ia_bulk.run_stamp", lambda: FIXED_STAMP)

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
    assert entry["identifier"] == "lcps-astoriaphotos-00001"
    assert entry["uploaded_as"] == f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"
    assert entry["status"] == "success"


def test_cmd_upload_csv_path_stamps_every_row_with_one_run_stamp_not_a_fresh_one_per_row(
    tmp_path, monkeypatch
):
    """The CSV path shares run_rows/effective_identifier with the Sheet path
    and has the same defect - see docs/DECISIONS.md, "Test identifiers carry
    a per-run stamp". run_stamp() is faked to return a NEW value on every
    call; if the implementation regressed to computing the stamp once per row
    instead of once per run, row 2 would be logged under the second call's
    value and this test would catch that mismatch instead of merely
    confirming 'some stamp' is present."""
    from ia_bulk import cmd_upload

    (tmp_path / "photo1.jpg").write_bytes(b"data")
    (tmp_path / "photo2.jpg").write_bytes(b"data")
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
            },
            {
                "identifier": "lcps-astoriaphotos-00002",
                "file": "photo2.jpg",
                "mediatype": "image",
                "title": "Second photo",
                "date": "1958",
            },
        ],
    )
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "lcps", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )
    log_dir = tmp_path / "logs"

    monkeypatch.setattr("ia_bulk.upload_row", lambda row, target_identifier, collection, files_dir: None)
    stamp_calls = []

    def fake_run_stamp():
        stamp_calls.append(len(stamp_calls))
        return f"stamp{stamp_calls[-1]}"

    monkeypatch.setattr("ia_bulk.run_stamp", fake_run_stamp)

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
    assert stamp_calls == [0]
    log_files = list(log_dir.glob("upload-*.jsonl"))
    entries = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").strip().splitlines()]
    assert [entry["uploaded_as"] for entry in entries] == [
        "zztest-stamp0-lcps-astoriaphotos-00001",
        "zztest-stamp0-lcps-astoriaphotos-00002",
    ]


def test_cmd_upload_uses_real_identifier_as_target_when_live(tmp_path, monkeypatch):
    """Doubles as the CSV path's live-purity guard: run_stamp() is faked to
    return an obviously-wrong value, so if a stamp ever leaked into the live
    branch it would show up verbatim in this exact-match assertion instead of
    being masked by a stamp that happens to look plausible."""
    from ia_bulk import cmd_upload

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
    log_dir = tmp_path / "logs"

    monkeypatch.setattr("ia_bulk.upload_row", lambda row, target_identifier, collection, files_dir: None)
    monkeypatch.setattr("ia_bulk.run_stamp", lambda: "shouldneverappear")

    args = Namespace(
        csv=str(csv_path),
        files_dir=str(tmp_path),
        registry=str(registry_path),
        live=True,
        collection="lcps",
        log_dir=str(log_dir),
        resume_from=None,
    )

    exit_code = cmd_upload(args)

    assert exit_code == 0
    entry = json.loads(list(log_dir.glob("upload-*.jsonl"))[0].read_text(encoding="utf-8").strip())
    assert entry["uploaded_as"] == "lcps-astoriaphotos-00001"


def test_cmd_upload_fails_validation_before_touching_network(tmp_path, monkeypatch):
    from ia_bulk import cmd_upload

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

    upload_calls = []
    monkeypatch.setattr(
        "ia_bulk.upload_row", lambda row, target_identifier, collection, files_dir: upload_calls.append(row)
    )

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
    from ia_bulk import cmd_upload

    (tmp_path / "photo1.jpg").write_bytes(b"data")
    (tmp_path / "photo2.jpg").write_bytes(b"data")
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
            },
            {
                "identifier": "lcps-astoriaphotos-00002",
                "file": "photo2.jpg",
                "mediatype": "image",
                "title": "Second photo",
                "date": "1958",
            },
        ],
    )
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "lcps", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )
    prior_log = tmp_path / "prior.jsonl"
    log_result(prior_log, "lcps-astoriaphotos-00001", "photo1.jpg", "success", live=False)

    uploaded = []
    monkeypatch.setattr(
        "ia_bulk.upload_row",
        lambda row, target_identifier, collection, files_dir: uploaded.append(row["identifier"]),
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
    assert uploaded == ["lcps-astoriaphotos-00002"]


def test_cmd_upload_resume_from_skips_a_row_whose_prior_log_entry_carries_a_different_stamp(
    tmp_path, monkeypatch
):
    """load_prior_successes() matches on the real `identifier`, never on
    `uploaded_as` - see docs/DECISIONS.md, "Test identifiers carry a per-run
    stamp". This run's own stamp (FIXED_STAMP) deliberately differs from the
    stamp already recorded in the prior log's `uploaded_as`
    (zztest-19990101t000000-...), so if --resume-from ever started matching
    on uploaded_as instead of identifier, row 1 would look "not yet done"
    under this run's different stamp and get re-uploaded."""
    from ia_bulk import cmd_upload

    (tmp_path / "photo1.jpg").write_bytes(b"data")
    (tmp_path / "photo2.jpg").write_bytes(b"data")
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
            },
            {
                "identifier": "lcps-astoriaphotos-00002",
                "file": "photo2.jpg",
                "mediatype": "image",
                "title": "Second photo",
                "date": "1958",
            },
        ],
    )
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "lcps", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )
    prior_log = tmp_path / "prior.jsonl"
    log_result(
        prior_log,
        "lcps-astoriaphotos-00001",
        "photo1.jpg",
        "success",
        live=False,
        uploaded_as="zztest-19990101t000000-lcps-astoriaphotos-00001",
    )

    uploaded = []
    monkeypatch.setattr(
        "ia_bulk.upload_row",
        lambda row, target_identifier, collection, files_dir: uploaded.append(row["identifier"]),
    )
    monkeypatch.setattr("ia_bulk.run_stamp", lambda: FIXED_STAMP)

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
    assert uploaded == ["lcps-astoriaphotos-00002"]


def test_cmd_upload_resume_from_a_test_mode_log_does_not_skip_a_live_run(tmp_path, monkeypatch):
    """A --resume-from log written by a non-live (test_collection) run only
    proves the zztest-prefixed item landed in the sandbox, never the real
    one - it must not be able to make a later --live run silently skip a
    real upload."""
    from ia_bulk import cmd_upload

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
    prior_log = tmp_path / "prior.jsonl"
    log_result(prior_log, "lcps-astoriaphotos-00001", "photo1.jpg", "success", live=False)

    uploaded = []
    monkeypatch.setattr(
        "ia_bulk.upload_row",
        lambda row, target_identifier, collection, files_dir: uploaded.append(row["identifier"]),
    )

    args = Namespace(
        csv=str(csv_path),
        files_dir=str(tmp_path),
        registry=str(registry_path),
        live=True,
        collection="lcps",
        log_dir=str(tmp_path / "logs"),
        resume_from=str(prior_log),
    )

    exit_code = cmd_upload(args)

    assert exit_code == 0
    assert uploaded == ["lcps-astoriaphotos-00001"]


def test_cmd_sync_metadata_writes_success_log_with_test_prefixed_target_when_not_live(tmp_path, monkeypatch):
    from ia_bulk import cmd_sync_metadata

    csv_path = tmp_path / "updates.csv"
    write_csv(csv_path, ["identifier", "title"], [{"identifier": "lcps-astoriaphotos-00001", "title": "Corrected title"}])
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "lcps", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )
    log_dir = tmp_path / "logs"

    monkeypatch.setattr("ia_bulk.update_metadata_row", lambda row, target_identifier: None)
    monkeypatch.setattr("ia_bulk.run_stamp", lambda: FIXED_STAMP)

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
    assert entry["uploaded_as"] == f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"


def test_cmd_sync_metadata_treats_no_changes_as_unchanged_not_failure(tmp_path, monkeypatch, capsys):
    from ia_bulk import cmd_sync_metadata, MetadataUnchanged

    csv_path = tmp_path / "updates.csv"
    write_csv(
        csv_path,
        ["identifier", "title"],
        [
            {"identifier": "lcps-astoriaphotos-00001", "title": "Already correct"},
            {"identifier": "lcps-astoriaphotos-00002", "title": "New title"},
        ],
    )
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "lcps", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )
    log_dir = tmp_path / "logs"

    def fake_update_metadata_row(row, target_identifier):
        if row["identifier"].strip() == "lcps-astoriaphotos-00001":
            raise MetadataUnchanged(target_identifier)

    monkeypatch.setattr("ia_bulk.update_metadata_row", fake_update_metadata_row)

    args = Namespace(
        csv=str(csv_path),
        registry=str(registry_path),
        live=False,
        log_dir=str(log_dir),
        resume_from=None,
    )

    exit_code = cmd_sync_metadata(args)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "1 item(s) updated successfully, 1 unchanged, 0 error(s)" in out

    entries = [
        json.loads(line)
        for line in list(log_dir.glob("sync-metadata-*.jsonl"))[0].read_text(encoding="utf-8").strip().splitlines()
    ]
    statuses = {entry["identifier"]: entry["status"] for entry in entries}
    assert statuses["lcps-astoriaphotos-00001"] == "unchanged"
    assert statuses["lcps-astoriaphotos-00002"] == "success"


def test_cmd_sync_metadata_does_not_require_file_or_mediatype_columns(tmp_path, monkeypatch):
    from ia_bulk import cmd_sync_metadata

    csv_path = tmp_path / "updates.csv"
    write_csv(csv_path, ["identifier", "title"], [{"identifier": "lcps-astoriaphotos-00001", "title": "Corrected title"}])
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "lcps", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )

    monkeypatch.setattr("ia_bulk.update_metadata_row", lambda row, target_identifier: None)

    args = Namespace(
        csv=str(csv_path),
        registry=str(registry_path),
        live=False,
        log_dir=str(tmp_path / "logs"),
        resume_from=None,
    )

    exit_code = cmd_sync_metadata(args)

    assert exit_code == 0


def test_cmd_sync_metadata_fails_identifier_validation_before_touching_network(tmp_path, monkeypatch):
    from ia_bulk import cmd_sync_metadata

    csv_path = tmp_path / "updates.csv"
    write_csv(
        csv_path,
        ["identifier", "title"],
        [{"identifier": "lcps-unregisteredproject-00001", "title": "Corrected title"}],
    )
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "lcps", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )

    update_calls = []
    monkeypatch.setattr("ia_bulk.update_metadata_row", lambda row, target_identifier: update_calls.append(row))

    args = Namespace(
        csv=str(csv_path),
        registry=str(registry_path),
        live=False,
        log_dir=str(tmp_path / "logs"),
        resume_from=None,
    )

    exit_code = cmd_sync_metadata(args)

    assert exit_code == 1
    assert update_calls == []


def test_build_parser_validate_subcommand_defaults():
    parser = build_parser()
    args = parser.parse_args(["validate", "--project", "astoriaphotos", "--csv", "items.csv"])
    assert args.command == "validate"
    assert args.project == "astoriaphotos"
    assert args.csv == "items.csv"
    assert args.files_dir == "."
    assert args.registry == "projects_registry.json"
    assert args.live is False


def test_build_parser_validate_subcommand_omits_csv_to_select_the_sheet_path():
    """--csv is optional and mutually exclusive with reading the Sheet -
    omitting it must leave args.csv as None rather than requiring a path,
    since that's what cmd_validate checks to decide which source to read."""
    parser = build_parser()
    args = parser.parse_args(["validate", "--project", "astoriaphotos"])
    assert args.csv is None


@pytest.mark.parametrize(
    "subcommand,extra_args",
    [
        ("validate", []),
        ("upload", ["--csv", "items.csv"]),
        ("sync-metadata", ["updates.csv"]),
    ],
)
def test_build_parser_requires_project_on_every_subcommand(subcommand, extra_args):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([subcommand, *extra_args])


def test_build_parser_upload_subcommand_defaults_to_the_sheet_and_no_collection():
    """`--collection` no longer defaults to "lcps" (not a real Internet
    Archive collection) and `--files-dir` no longer defaults to "." - both are
    --csv-path overrides now, and on the Sheet path they come from the
    registry. `csv` is likewise no longer a required positional, so
    `upload --project X` selects the Sheet rather than failing."""
    parser = build_parser()
    args = parser.parse_args(["upload", "--project", "astoriaphotos"])
    assert args.command == "upload"
    assert args.project == "astoriaphotos"
    assert args.csv is None
    assert args.live is False
    assert args.collection is None
    assert args.files_dir is None
    assert args.write_identifier is False
    assert args.dry_run is False
    assert args.resume_from is None


def test_build_parser_upload_subcommand_accepts_live_and_resume_from():
    parser = build_parser()
    args = parser.parse_args(
        [
            "upload",
            "--csv",
            "items.csv",
            "--project",
            "astoriaphotos",
            "--live",
            "--resume-from",
            "logs/upload-x.jsonl",
        ]
    )
    assert args.csv == "items.csv"
    assert args.live is True
    assert args.resume_from == "logs/upload-x.jsonl"


def test_build_parser_upload_subcommand_accepts_write_identifier_and_dry_run():
    parser = build_parser()
    args = parser.parse_args(
        ["upload", "--project", "astoriaphotos", "--write-identifier", "--dry-run"]
    )
    assert args.write_identifier is True
    assert args.dry_run is True


def test_build_parser_sync_metadata_subcommand_defaults():
    parser = build_parser()
    args = parser.parse_args(["sync-metadata", "updates.csv", "--project", "astoriaphotos"])
    assert args.command == "sync-metadata"
    assert args.project == "astoriaphotos"
    assert args.csv == "updates.csv"
    assert args.live is False


def test_main_dispatches_to_cmd_validate(monkeypatch, tmp_path):
    csv_path = tmp_path / "items.csv"
    csv_path.write_text("identifier,file,mediatype,title,date\n", encoding="utf-8")

    calls = []
    monkeypatch.setattr("ia_bulk.cmd_validate", lambda args: calls.append(args.csv) or 0)

    exit_code = main(["validate", "--project", "astoriaphotos", "--csv", str(csv_path)])

    assert exit_code == 0
    assert calls == [str(csv_path)]


# ---------------------------------------------------------------------------
# Task 10: the Sheet path's reserve -> upload -> confirm protocol.
#
# Every assertion below that matters is an ORDERED SEQUENCE, not a membership
# or substring check. The safety argument for this whole command is that the
# reserve write lands BEFORE the upload, and `"write" in kinds` cannot tell
# [read, write, upload] from [read, upload, write].
# ---------------------------------------------------------------------------

_A1_RE = re.compile(r"^([A-Z]+)(\d+)$")


def _a1_to_indexes(a1):
    match = _A1_RE.match(a1)
    assert match, f"not an A1 reference: {a1!r}"
    column = 0
    for character in match.group(1):
        column = column * 26 + (ord(character) - ord("A") + 1)
    return column - 1, int(match.group(2)) - 1


class SheetUploadRecorder:
    """One ordered event log spanning BOTH the Sheet client and upload_row.

    Keeping reads, writes and uploads in a single list is the point: the
    reserve/confirm protocol is a claim about ORDER across two different
    collaborators, and two separate per-collaborator lists cannot express it."""

    def __init__(self):
        self.events = []

    @property
    def kinds(self):
        return [event[0] for event in self.events]

    @property
    def writes(self):
        return [event[1] for event in self.events if event[0] == "write"]

    @property
    def uploads(self):
        return [event[1] for event in self.events if event[0] == "upload"]


class RecordingSheetClient:
    """Stands in for SheetClient on the upload path. Unlike FakeSheetClient it
    also accepts writes, and APPLIES them to its own grid - so the confirm
    step's re-read sees what the reserve step wrote, exactly as a real Sheet
    would. `before_read` is the hook a mid-run-edit test uses to change the
    grid out from under the run between two reads."""

    def __init__(self, grid, recorder, before_read=None, raise_on_write=None, raise_on_read=None):
        self.grid = [list(row) for row in grid]
        self._recorder = recorder
        self._before_read = before_read
        self._raise_on_write = raise_on_write
        self._raise_on_read = raise_on_read
        self.read_count = 0
        self.write_count = 0

    def read_grid(self):
        self.read_count += 1
        if self._before_read is not None:
            self._before_read(self.grid, self.read_count)
        self._recorder.events.append(("read", None))
        if self._raise_on_read == self.read_count:
            raise RuntimeError("Sheets API returned 503")
        return [list(row) for row in self.grid]

    def write_cells(self, updates):
        pairs = [(update.a1, update.value) for update in updates]
        assert pairs, "write_cells must never be called with an empty batch"
        self.write_count += 1
        self._recorder.events.append(("write", pairs))
        if self._raise_on_write == self.write_count:
            raise RuntimeError("Sheets API returned 503")
        for a1, value in pairs:
            column, row_index = _a1_to_indexes(a1)
            while len(self.grid) <= row_index:
                self.grid.append([])
            row = self.grid[row_index]
            while len(row) <= column:
                row.append("")
            row[column] = value


def make_upload_stub(recorder, fail_for=(), captured=None):
    def fake_upload_row(row, target_identifier, collection, files_dir):
        recorder.events.append(("upload", target_identifier))
        if captured is not None:
            captured.append(
                {"row": dict(row), "collection": collection, "files_dir": str(files_dir)}
            )
        if target_identifier in fail_for:
            raise RuntimeError("boom")

    return fake_upload_row


# A=Title, B=file, C=ia_identifier, D=ia_uploaded, E=ia_url, F=ia_identifier_bib
SHEET_HEADER = ["Title", "file", "ia_identifier", "ia_uploaded", "ia_url", "ia_identifier_bib"]
FIXED_TIMESTAMP = "2026-08-19T09:00:00"


def make_upload_args(tmp_path, registry_path, **overrides):
    args = Namespace(
        csv=None,
        project="astoriaphotos",
        registry=str(registry_path),
        files_dir=None,
        collection=None,
        live=False,
        write_identifier=False,
        dry_run=False,
        log_dir=str(tmp_path / "logs"),
        resume_from=None,
    )
    for name, value in overrides.items():
        setattr(args, name, value)
    return args


def setup_sheet_upload(
    tmp_path,
    monkeypatch,
    grid,
    files=("photo1.jpg",),
    fail_for=(),
    captured=None,
    before_read=None,
    registry=None,
    raise_on_write=None,
    raise_on_read=None,
    timestamps=None,
):
    """Builds the whole Sheet-upload world: files on disk, a registry, a
    recording client monkeypatched over build_sheet_client (the single seam),
    and upload_row stubbed so nothing touches the network. Also pins the
    confirm timestamp so a confirm batch can be asserted as an exact ordered
    sequence rather than 'a cell whose value is some string', and pins
    run_stamp() to FIXED_STAMP for the same reason - a caller that needs to
    prove the stamp is computed once per run rather than once per row/chunk
    re-monkeypatches ia_bulk.run_stamp itself after calling this."""
    for name in files:
        (tmp_path / name).write_bytes(b"x")

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(registry or make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )

    recorder = SheetUploadRecorder()
    client = RecordingSheetClient(
        grid,
        recorder,
        before_read=before_read,
        raise_on_write=raise_on_write,
        raise_on_read=raise_on_read,
    )
    build_calls = []

    def fake_build_sheet_client(config, live):
        build_calls.append((config, live))
        return client

    # A list of timestamps hands out one per call, so a multi-chunk test can
    # tell "stamped when its own chunk ran" from "stamped once for the whole
    # run" - on a full-collection run those differ by hours.
    remaining = list(timestamps or [])

    def fake_upload_timestamp():
        return remaining.pop(0) if remaining else FIXED_TIMESTAMP

    monkeypatch.setattr("ia_bulk.build_sheet_client", fake_build_sheet_client)
    monkeypatch.setattr("ia_bulk.upload_row", make_upload_stub(recorder, fail_for, captured))
    monkeypatch.setattr("ia_bulk.upload_timestamp", fake_upload_timestamp)
    monkeypatch.setattr("ia_bulk.run_stamp", lambda: FIXED_STAMP)

    return recorder, client, registry_path, build_calls


def test_cmd_upload_default_mode_writes_nothing_to_the_sheet_and_only_uploads_prefixed_identifiers(
    tmp_path, monkeypatch, capsys
):
    """The single most important property of this command: with neither
    --live nor --write-identifier, the operator's Sheet is untouched and
    nothing reaches Internet Archive under a real, permanent identifier.
    Asserted as 'the recorded call list holds exactly one read and no writes
    at all', not as 'no write of the wrong thing' - the latter still passes if
    a write happens with contents the test did not think to check."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg", "photo2.jpg")
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.kinds == ["read", "upload", "upload"]
    assert recorder.writes == []
    assert recorder.uploads == [
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001",
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002",
    ]
    assert client.grid == [list(row) for row in grid]


def test_cmd_upload_with_write_identifier_reserves_then_uploads_then_confirms(
    tmp_path, monkeypatch, capsys
):
    """Reserve BEFORE upload is the entire ordering argument: uploading first
    would let a crash strand an item on IA the Sheet has no record of, and the
    next run's max+1 would mint that same number onto a different photograph.
    The exact event sequence is asserted, so a reordering fails here even
    though every individual call would still be 'present'. The read before the
    reserve write is the mid-run-edit guard: row numbers come from the initial
    read and are re-verified against a fresh one before every write."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.kinds == ["read", "read", "write", "upload", "read", "write"]
    assert recorder.writes == [
        [("C2", "lcps-astoriaphotos-00001")],
        [
            ("D2", FIXED_TIMESTAMP),
            ("E2", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"),
            ("F2", "photo1.jpg"),
        ],
    ]
    assert recorder.uploads == [f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"]


def test_cmd_upload_reserved_row_uploads_under_its_existing_identifier_and_mints_nothing(
    tmp_path, monkeypatch, capsys
):
    """RESERVED is the crash-recovery state: a number reached the Sheet but
    the upload never confirmed. Re-minting would burn a second permanent
    number on the same photograph, so the run must reuse the existing one and
    issue NO reserve write at all - which is why the event sequence has no
    write before the upload."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "lcps-astoriaphotos-00042", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.uploads == [f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00042"]
    # The pre-reserve read still happens (the guard runs whether or not there
    # is anything to reserve); the reserve WRITE does not.
    assert recorder.kinds == ["read", "read", "upload", "read", "write"]
    assert recorder.writes == [
        [
            ("D2", FIXED_TIMESTAMP),
            ("E2", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00042"),
            ("F2", "photo1.jpg"),
        ]
    ]
    assert client.grid[1][2] == "lcps-astoriaphotos-00042"


def test_cmd_upload_skips_a_done_row_entirely_and_mints_above_its_number(
    tmp_path, monkeypatch, capsys
):
    """A DONE row is not re-uploaded, and its number still counts when the
    next one is minted - otherwise the run would hand an already-used
    permanent identifier to a different photograph."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["Done photo", "photo1.jpg", "lcps-astoriaphotos-00007", "2026-01-01", "u", "photo1.jpg"],
        ["New photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg", "photo2.jpg")
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.uploads == [f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00008"]
    assert recorder.writes == [
        [("C3", "lcps-astoriaphotos-00008")],
        [
            ("D3", FIXED_TIMESTAMP),
            ("E3", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00008"),
            ("F3", "photo2.jpg"),
        ],
    ]


def test_cmd_upload_skips_an_invalid_row_uploads_the_valid_ones_and_exits_non_zero(
    tmp_path, monkeypatch, capsys
):
    """The Sheet path's deliberate opposite of the CSV path: one typo in row
    9,000 must not block the other 9,999, but a partial run must never be
    mistaken for a clean one. See docs/DECISIONS.md, "On the Sheet path,
    `upload` uploads the valid rows and reports the rest".

    Row 2's file is broken (present but resolves to nothing) rather than
    its title being blank - a blank required_for_upload column is now a
    readiness fact, not a validation error (see the SHEET_REQUIRED_COLUMNS
    shrink), so a blank title no longer produces the genuinely INVALID row
    this test needs."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "does-not-exist.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo2.jpg",)
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    out = capsys.readouterr().out

    assert exit_code == 1
    assert recorder.uploads == [f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"]
    assert recorder.writes == [
        [("C3", "lcps-astoriaphotos-00001")],
        [
            ("D3", FIXED_TIMESTAMP),
            ("E3", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"),
            ("F3", "photo2.jpg"),
        ],
    ]
    assert "[FAIL] row 2" in out
    assert _unresolved_message(tmp_path, "does-not-exist.jpg") in out


def test_cmd_upload_dry_run_writes_nothing_uploads_nothing_and_prints_what_it_would_mint(
    tmp_path, monkeypatch, capsys
):
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(
        make_upload_args(tmp_path, registry_path, write_identifier=True, dry_run=True)
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert recorder.kinds == ["read"]
    assert recorder.writes == []
    assert recorder.uploads == []
    assert "lcps-astoriaphotos-00001" in out
    assert "C2 = lcps-astoriaphotos-00001" in out
    # The real run would upload under the STAMPED identifier, not the bare
    # permanent one - a dry run that only ever showed the real identifier
    # would misrepresent what --live's absence actually means. See
    # docs/DECISIONS.md, "Test identifiers carry a per-run stamp".
    assert (
        f"would mint 'lcps-astoriaphotos-00001' and upload it as "
        f"'zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001'"
    ) in out
    assert not (tmp_path / "logs").exists()


def test_cmd_upload_dry_run_without_write_identifier_says_it_would_write_nothing(
    tmp_path, monkeypatch, capsys
):
    """Pins the false branch of --write-identifier under --dry-run too: a
    rehearsal that describes writes it would never actually make is worse
    than no rehearsal."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, dry_run=True))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert recorder.writes == []
    assert "would write nothing to the Sheet" in out
    assert "C2 = " not in out


@pytest.mark.parametrize(
    "live,expected_sheet_id,expected_collection,expected_target",
    [
        (True, "REAL_SHEET_ID", "lcpsociety", "lcps-astoriaphotos-00001"),
        (False, "TEST_SHEET_ID", "test_collection", f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"),
    ],
)
def test_cmd_upload_passes_the_live_flag_through_and_picks_the_matching_sheet_and_collection(
    tmp_path, monkeypatch, capsys, live, expected_sheet_id, expected_collection, expected_target
):
    """Both directions, deliberately: Task 8 shipped a version that hardcoded
    build_sheet_client(config, True) - always the production Sheet - and
    passed all 167 tests because only live=True was ever exercised."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    captured = []
    recorder, client, registry_path, build_calls = setup_sheet_upload(
        tmp_path, monkeypatch, grid, captured=captured
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, live=live))
    capsys.readouterr()

    assert exit_code == 0
    assert len(build_calls) == 1
    config, passed_live = build_calls[0]
    assert passed_live is live
    assert config.sheet_id_for(passed_live) == expected_sheet_id
    assert [call["collection"] for call in captured] == [expected_collection]
    assert recorder.uploads == [expected_target]


def test_cmd_upload_live_writes_back_even_without_write_identifier(tmp_path, monkeypatch, capsys):
    """--live always records: an item that exists on Internet Archive under a
    permanent identifier the Sheet does not know about is the one outcome
    this protocol exists to prevent."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, live=True))
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.kinds == ["read", "read", "write", "upload", "read", "write"]
    assert recorder.writes[0] == [("C2", "lcps-astoriaphotos-00001")]
    assert recorder.writes[1] == [
        ("D2", FIXED_TIMESTAMP),
        ("E2", "https://archive.org/details/lcps-astoriaphotos-00001"),
        ("F2", "photo1.jpg"),
    ]


def test_cmd_upload_confirm_skips_a_row_whose_identifier_changed_underneath_it(
    tmp_path, monkeypatch, capsys
):
    """If a human inserts or deletes rows mid-run the indices shift, and the
    confirm batch would otherwise stamp one photograph's URL onto another's
    row. The identifier at the target row is re-read and compared before
    anything is written there."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]

    def edit_between_reserve_and_confirm(live_grid, read_count):
        # Read 1 is the initial grid read, read 2 the pre-reserve guard, read 3
        # the pre-confirm guard - so editing on 3 lands in the reserve->confirm
        # window specifically.
        if read_count == 3:
            live_grid[1][2] = "lcps-astoriaphotos-99999"

    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg"),
        before_read=edit_between_reserve_and_confirm,
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    # Row 2 uploaded fine; only its Sheet write-back is withheld. Row 3 is
    # untouched by the edit and is confirmed normally. The confirm batch is
    # asserted BEFORE the exit code deliberately: "it wrote row 2's URL onto
    # whatever now sits at row 2" is the failure that matters, and an
    # exit-code assertion firing first would hide which one went wrong.
    assert recorder.uploads == [
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001",
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002",
    ]
    assert recorder.writes[1] == [
        ("D3", FIXED_TIMESTAMP),
        ("E3", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002"),
        ("F3", "photo2.jpg"),
    ]
    assert "row 2 is no longer the row this run planned for" in captured.err
    assert "IS on Internet Archive" in captured.err
    assert exit_code == 1

    log_files = list((tmp_path / "logs").glob("upload-*.jsonl"))
    entries = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]
    statuses = {entry["identifier"]: entry["status"] for entry in entries}
    assert statuses["lcps-astoriaphotos-00001"] == "unconfirmed"
    assert statuses["lcps-astoriaphotos-00002"] == "success"


def test_cmd_upload_writes_the_resolved_path_to_ia_identifier_bib_not_the_sheets_raw_cell(
    tmp_path, monkeypatch, capsys
):
    """225 of 234 filenames in the real Sheet carry no extension, so the
    reference that was actually uploaded differs from what the Sheet says.
    ia_identifier_bib must record what was uploaded, and a stale value already
    sitting in that column must not survive."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "Liberty", "", "", "", "STALE/whatever"]]
    captured = []
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("Liberty.tif",), captured=captured
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.writes[1][2] == ("F2", "Liberty.tif")
    assert captured[0]["row"]["identifier-bib"] == "Liberty.tif"


def test_cmd_upload_with_no_csv_runs_against_the_sheet_instead_of_erroring(
    tmp_path, monkeypatch, capsys
):
    """`upload --project X` used to fail with "the following arguments are
    required: csv". Driven through main() so the parser change is what is
    under test, not just cmd_upload."""
    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = main(
        [
            "upload",
            "--project",
            "astoriaphotos",
            "--registry",
            str(registry_path),
            "--log-dir",
            str(tmp_path / "logs"),
        ]
    )
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.uploads == [f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"]


def test_cmd_upload_never_sends_tool_owned_or_held_back_columns_as_ia_metadata(
    tmp_path, monkeypatch, capsys
):
    """upload_row turns every key it is handed into an Internet Archive
    metadata field, and IA metadata is permanent. The tool's own ia_ columns
    and anything marked (LCPS Internal) must be filtered out before it ever
    sees them; identifier-bib and mediatype are generated instead of read from
    a column. See docs/DECISIONS.md."""
    from ia_bulk import cmd_upload

    header = SHEET_HEADER + ["Donor notes (LCPS Internal)"]
    grid = [header, ["First photo", "photo1.jpg", "", "", "", "", "do not publish"]]
    captured = []
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, captured=captured
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    capsys.readouterr()

    assert exit_code == 0
    metadata_row = captured[0]["row"]
    assert sorted(metadata_row) == ["file", "identifier-bib", "mediatype", "title"]
    assert metadata_row["identifier-bib"] == "photo1.jpg"
    assert metadata_row["mediatype"] == "image"
    # files_dir must come from the registry, not a "." default - upload_row
    # joins it to row['file'] to find the bytes it sends.
    assert captured[0]["files_dir"] == str(tmp_path)


def test_cmd_upload_refuses_a_sheet_without_the_ia_write_back_columns(
    tmp_path, monkeypatch, capsys
):
    from ia_bulk import cmd_upload

    grid = [["Title", "file"], ["First photo", "photo1.jpg"]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert recorder.uploads == []
    assert recorder.writes == []
    assert "ia_identifier" in captured.err
    assert "ia_uploaded" in captured.err


def test_cmd_upload_refuses_a_header_collision_before_uploading_anything(
    tmp_path, monkeypatch, capsys
):
    """A header-level defect silently overwrites one column's data on EVERY
    row, so unlike a bad row it cannot be routed around by skipping."""
    from ia_bulk import cmd_upload

    header = SHEET_HEADER + ["Title!"]
    grid = [header, ["First photo", "photo1.jpg", "", "", "", "", "collides"]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert recorder.uploads == []
    assert recorder.writes == []
    assert "normalize to field name 'title'" in captured.out


def test_cmd_upload_sheet_path_refuses_a_collection_override(tmp_path, monkeypatch, capsys):
    """--collection used to default to "lcps", which is not a real Internet
    Archive collection - a --live run would have pushed real photographs at a
    non-existent collection and reported success. On the Sheet path the value
    comes from the registry, and silently ignoring an explicit --collection
    would be its own trap."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, collection="lcps"))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert recorder.events == []
    assert "--collection" in captured.err


def test_cmd_upload_sheet_path_refuses_resume_from(tmp_path, monkeypatch, capsys):
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(
        make_upload_args(tmp_path, registry_path, resume_from=str(tmp_path / "prior.jsonl"))
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert recorder.events == []
    assert "--resume-from" in captured.err


def test_cmd_upload_sheet_path_refuses_an_unreplaced_placeholder_sheet_id(
    tmp_path, monkeypatch, capsys
):
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    registry = make_sheet_registry(files_dir=str(tmp_path), sheet_id="REPLACE_WITH_REAL_SHEET_ID")
    recorder, client, registry_path, build_calls = setup_sheet_upload(
        tmp_path, monkeypatch, grid, registry=registry
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, live=True))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert build_calls == []
    assert recorder.events == []
    assert "placeholder" in captured.err


def test_cmd_upload_csv_path_refuses_live_without_an_explicit_collection(tmp_path, monkeypatch):
    """The "lcps" default is gone; --live on the CSV path must now name the
    collection rather than silently inventing one."""
    from ia_bulk import cmd_upload

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
        json.dumps({"collection_key": "lcps", "projects": {"astoriaphotos": {}}}), encoding="utf-8"
    )

    uploaded = []
    monkeypatch.setattr(
        "ia_bulk.upload_row",
        lambda row, target_identifier, collection, files_dir: uploaded.append(target_identifier),
    )

    args = Namespace(
        csv=str(csv_path),
        files_dir=str(tmp_path),
        registry=str(registry_path),
        live=True,
        collection=None,
        log_dir=str(tmp_path / "logs"),
        resume_from=None,
    )

    assert cmd_upload(args) == 1
    assert uploaded == []


def test_cmd_upload_records_a_failed_upload_without_confirming_it(tmp_path, monkeypatch, capsys):
    """A row whose upload raised is reserved (the number is spent - gaps are
    harmless, collisions are not) but never confirmed, so the next run sees it
    as RESERVED and retries under the same identifier."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg"),
        fail_for=(f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001",),
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 1
    assert recorder.writes == [
        [("C2", "lcps-astoriaphotos-00001"), ("C3", "lcps-astoriaphotos-00002")],
        [
            ("D3", FIXED_TIMESTAMP),
            ("E3", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002"),
            ("F3", "photo2.jpg"),
        ],
    ]


def test_cmd_upload_does_not_reserve_onto_a_row_that_shifted_after_the_initial_read(
    tmp_path, monkeypatch, capsys
):
    """The read->reserve window, which the confirm guard cannot cover.

    `read_grid()` fires once, then chunk N's reserve write fires after chunks
    1..N-1 have finished uploading - hours later on a full-collection run. If
    someone deletes a row in between, every row below shifts up and the reserve
    write lands on a different photograph. Checking `ia_identifier` at confirm
    time cannot catch it: reserve wrote that value at that index moments
    earlier, so the check would be verifying its own write.

    Here the human deletes row 2. Row 4 (already DONE, holding the permanent
    identifier ...-00007 and its live archive.org URL) slides up into row 3,
    which is the row this run planned to reserve ...-00008 onto. Unguarded,
    that overwrites a completed row's permanent identifier and URL and exits
    0. The fingerprint - the file_template columns, which this tool never
    writes - is what notices."""
    from ia_bulk import cmd_upload

    done_row = [
        "Done photo",
        "photo3.jpg",
        "lcps-astoriaphotos-00007",
        "2026-01-01T00:00:00",
        "https://archive.org/details/lcps-astoriaphotos-00007",
        "photo3.jpg",
    ]
    grid = [
        SHEET_HEADER,
        ["Filler photo", "photo1.jpg", "lcps-astoriaphotos-00006", "2026-01-01T00:00:00", "u", "photo1.jpg"],
        ["Photo one", "photo2.jpg", "", "", "", ""],
        list(done_row),
    ]

    def human_deletes_row_two(live_grid, read_count):
        if read_count == 2:
            del live_grid[1]

    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg", "photo3.jpg"),
        before_read=human_deletes_row_two,
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    # The completed row is untouched: same permanent identifier, same URL.
    assert client.grid[2] == done_row
    assert recorder.writes == []
    assert recorder.uploads == []
    assert "row 3 is no longer the row this run planned for" in captured.err
    assert exit_code == 1


def test_cmd_upload_catches_a_shift_that_an_identifier_check_alone_cannot_see(
    tmp_path, monkeypatch, capsys
):
    """The de-tautologising case, isolated.

    Deleting row 2 slides two *unassigned* rows up one. Every candidate row
    still has a blank `ia_identifier`, so a guard that only looked at that
    column would wave both through and reserve ...-00007 onto the photograph
    that was supposed to get ...-00008. Only the fingerprint - the
    file_template columns, which this tool never writes - notices that row 3
    now describes a different photograph."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["Filler", "photo1.jpg", "lcps-astoriaphotos-00006", "2026-01-01T00:00:00", "u", "photo1.jpg"],
        ["Photo one", "photo2.jpg", "", "", "", ""],
        ["Photo two", "photo3.jpg", "", "", "", ""],
    ]

    def human_deletes_row_two(live_grid, read_count):
        if read_count == 2:
            del live_grid[1]

    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg", "photo3.jpg"),
        before_read=human_deletes_row_two,
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    assert recorder.writes == []
    assert recorder.uploads == []
    assert "row 3 is no longer the row this run planned for" in captured.err
    assert "row 4 is no longer the row this run planned for" in captured.err
    assert exit_code == 1


def test_cmd_upload_aborts_the_whole_run_when_a_column_is_inserted_mid_run(
    tmp_path, monkeypatch, capsys
):
    """Column positions are cached from the initial read too. A column
    inserted mid-run shifts every write-back column one to the right, so every
    cell this run would write lands in the wrong column - not just the wrong
    row.

    That is equally true of every remaining chunk, so the run stops rather
    than re-reading and re-reporting the whole Sheet on the way to the same
    conclusion (20 reads and 10,000 stderr lines on a full run). Two rows and
    CHUNK_SIZE 1: the sequence must stop at the first verify read.

    The message must also say a COLUMN moved - "row N is no longer the row
    this run planned for" is wrong in kind here and sends the operator to look
    at the wrong thing."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]

    def human_inserts_a_column(live_grid, read_count):
        if read_count == 2:
            for row in live_grid:
                row.insert(1, "Photographer" if row is live_grid[0] else "unknown")

    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg"),
        before_read=human_inserts_a_column,
    )
    monkeypatch.setattr("ia_bulk.CHUNK_SIZE", 1)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    assert recorder.kinds == ["read", "read"]
    assert recorder.writes == []
    assert recorder.uploads == []
    assert "the Sheet's columns moved" in captured.err
    assert "no longer the row this run planned for" not in captured.err
    # The filename is the only durable handle left: nothing was uploaded, so
    # the identifier exists nowhere, and the row number is what went stale.
    assert "File: 'photo1.jpg'" in captured.err
    assert "2 rows not attempted" in captured.out
    assert "log written to" in captured.out
    assert exit_code == 1


def test_cmd_upload_reports_a_sheets_read_failure_during_verify_instead_of_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """The same defect as the write-failure one, on the other half of the same
    step: the guard added two Sheets READS per chunk - roughly 80 across a
    full-collection run spanning hours - and one transient 503 among them must
    not end a run that has already created thousands of permanent Internet
    Archive items with a stack trace.

    Read 3 is the pre-confirm verify, so both items are on Internet Archive by
    the time it fails."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg", "photo2.jpg"), raise_on_read=3
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    assert exit_code == 1
    # Reserve was written; the confirm batch never was.
    assert recorder.writes == [
        [("C2", "lcps-astoriaphotos-00001"), ("C3", "lcps-astoriaphotos-00002")]
    ]
    assert "the Sheet could not be re-read" in captured.err
    assert "Sheets API returned 503" in captured.err
    assert "2 file(s) uploaded successfully" in captured.out
    assert "uploaded but NOT recorded in the Sheet" in captured.out
    assert "stderr" in captured.out
    assert "log written to" in captured.out

    log_files = list((tmp_path / "logs").glob("upload-*.jsonl"))
    entries = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]
    assert [entry["status"] for entry in entries] == [
        "success",
        "success",
        "unconfirmed",
        "unconfirmed",
    ]


def test_cmd_upload_reports_a_deleted_ia_column_instead_of_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """Renaming one of the four ia_ columns mid-run makes
    locate_write_back_columns raise inside the guard. Nothing is written, so it
    is fail-safe on data, but an unhandled MissingWriteBackColumns is still a
    bare traceback - and README promises a mismatch is *reported*."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]

    def human_renames_the_url_column(live_grid, read_count):
        if read_count == 2:
            live_grid[0][4] = "Notes"

    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, before_read=human_renames_the_url_column
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert recorder.writes == []
    assert recorder.uploads == []
    assert "a column this run writes to is gone" in captured.err
    assert "ia_url" in captured.err
    assert "log written to" in captured.out


def test_cmd_upload_reserves_and_confirms_once_per_chunk_not_once_per_run(
    tmp_path, monkeypatch, capsys
):
    """Every other Sheet test runs 1-3 rows against CHUNK_SIZE = 500, so none
    of them ever reaches a second chunk - and both "hoist the reserve into one
    batch for the whole run" and "defer every confirm to the end" survive them
    untouched. The second is a shippable defect: one confirm batch of 10,000
    rows x 3 cells would likely exceed the Sheets request limit and lose the
    entire run's write-back.

    The full ordered sequence is asserted, chunk boundaries included."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg"),
        timestamps=["2026-08-19T09:00:00", "2026-08-19T11:30:00"],
    )
    monkeypatch.setattr("ia_bulk.CHUNK_SIZE", 1)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.kinds == [
        "read",
        "read", "write", "upload", "read", "write",
        "read", "write", "upload", "read", "write",
    ]
    assert recorder.writes == [
        [("C2", "lcps-astoriaphotos-00001")],
        [
            ("D2", "2026-08-19T09:00:00"),
            ("E2", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"),
            ("F2", "photo1.jpg"),
        ],
        [("C3", "lcps-astoriaphotos-00002")],
        [
            # Its own chunk's time, not the time chunk 1 started.
            ("D3", "2026-08-19T11:30:00"),
            ("E3", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002"),
            ("F3", "photo2.jpg"),
        ],
    ]


def test_cmd_upload_sheet_path_computes_run_stamp_once_for_the_whole_run_not_once_per_chunk(
    tmp_path, monkeypatch, capsys
):
    """run_stamp() must be computed once per run and threaded through, never
    recomputed per row or per chunk - see docs/DECISIONS.md, "Test
    identifiers carry a per-run stamp". setup_sheet_upload's default
    run_stamp fake always returns the same FIXED_STAMP, which cannot tell
    'computed once' from 'computed twice and happened to agree' - a fake that
    returns a NEW value on every call can. With CHUNK_SIZE forced to 1, this
    run spans two chunks; if plan_upload_targets ever moved the run_stamp()
    call from upload_from_sheet (once, before chunking starts) down into a
    per-row or per-chunk position, chunk 2 would be stamped with the second
    call's value and this test's ordered-sequence assertion would catch the
    mismatch, not just note 'a stamp was present'."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg", "photo2.jpg")
    )
    monkeypatch.setattr("ia_bulk.CHUNK_SIZE", 1)

    stamp_calls = []

    def fake_run_stamp():
        stamp_calls.append(len(stamp_calls))
        return f"stamp{stamp_calls[-1]}"

    monkeypatch.setattr("ia_bulk.run_stamp", fake_run_stamp)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 0
    assert stamp_calls == [0]
    assert recorder.uploads == [
        "zztest-stamp0-lcps-astoriaphotos-00001",
        "zztest-stamp0-lcps-astoriaphotos-00002",
    ]


def test_cmd_upload_treats_a_number_in_an_invalid_row_as_spent(tmp_path, monkeypatch, capsys):
    """A number that appears anywhere in the Sheet is spent, whatever the state
    of the row holding it. Minting from valid rows only would re-issue a number
    an invalid row already holds - which is the permanent collision the whole
    reserve-first design exists to prevent.

    The row's file is broken (present but resolves to nothing), not its
    title blank - a blank required_for_upload column is now a readiness
    fact rather than a validation error (see the SHEET_REQUIRED_COLUMNS
    shrink), so this test needs a different way to make the row genuinely
    INVALID while it still holds ...-00050."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        # File does not resolve, so it fails validation - but it still holds
        # ...-00050.
        ["First photo", "does-not-exist.jpg", "lcps-astoriaphotos-00050", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo2.jpg",)
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 1  # the invalid row was skipped
    assert recorder.uploads == [f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00051"]
    assert recorder.writes[0] == [("C3", "lcps-astoriaphotos-00051")]


def test_cmd_upload_reports_a_sheets_write_failure_instead_of_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """A 503, an expired token or a revoked share during the confirm write is
    an ordinary operational event. The operator still needs the summary and the
    log path - recovery is correct either way (the row stays RESERVED and the
    next run reuses the identifier), but they should not have to derive that
    from a stack trace."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, raise_on_write=2
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Sheets API returned 503" in captured.err
    assert "1 file(s) uploaded successfully" in captured.out
    assert "log written to" in captured.out

    log_files = list((tmp_path / "logs").glob("upload-*.jsonl"))
    entries = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]
    assert [entry["status"] for entry in entries] == ["success", "unconfirmed"]


def test_cmd_upload_prints_the_field_receipt_before_uploading(tmp_path, monkeypatch, capsys):
    """`upload` is where something permanent happens, so it must show the same
    receipt `validate` does rather than assume the operator ran validate first
    and remembers what it said."""
    from ia_bulk import cmd_upload

    header = SHEET_HEADER + ["Donor notes (LCPS Internal)", "Identifier"]
    grid = [header, ["First photo", "photo1.jpg", "", "", "", "", "private", "CD 1 01"]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "will upload these metadata fields:\n  title\n" in out
    assert "NOT uploaded - Internet Archive reserves these names:\n  identifier" in out
    assert "held back (LCPS Internal):\n  Donor notes (LCPS Internal)" in out


def test_validate_sheet_rows_and_validate_csv_rows_keep_their_two_different_answers(tmp_path):
    """Part B split validate_rows' path-mode parameters into two named
    wrappers. The wrappers must keep disagreeing in exactly the way the two
    paths need: a blank ia_identifier is the normal start of a Sheet row,
    while a blank identifier is a defect in a hand-prepared CSV."""
    from ia_bulk import validate_csv_rows, validate_sheet_rows

    (tmp_path / "photo1.jpg").write_bytes(b"x")
    row = {
        "identifier": "",
        "ia_identifier": "",
        "file": "photo1.jpg",
        "mediatype": "image",
        "title": "T",
    }

    sheet_results = validate_sheet_rows([dict(row)], tmp_path, make_registry())
    csv_results = validate_csv_rows([dict(row)], tmp_path, make_registry())

    assert sheet_results[0].errors == []
    assert csv_results[0].errors == ["missing required column 'identifier'"]


def test_row_validation_is_ready_when_no_fields_missing():
    result = RowValidation(row_number=2, identifier="")
    assert result.readiness is Readiness.READY
    assert result.missing_fields == []


def test_row_validation_is_not_ready_when_fields_missing():
    result = RowValidation(row_number=2, identifier="", missing_fields=["title"])
    assert result.readiness is Readiness.NOT_READY


def test_readiness_is_independent_of_validity():
    """The combination that motivates the whole design: a row nobody has
    catalogued yet whose filename is also wrong."""
    result = RowValidation(
        row_number=2,
        identifier="",
        errors=["no file found in '/data' matching 'Finnis.jpg'"],
        missing_fields=["title"],
    )
    assert result.readiness is Readiness.NOT_READY
    assert result.is_valid is False


def test_required_for_upload_naming_a_missing_column_is_an_error():
    column_map = build_column_map(["Title", "Theme", "File Name"])
    config = _sheet_config(required_for_upload=("titel",))
    errors = check_required_for_upload(config, column_map)
    assert errors == [
        "required_for_upload names 'titel', which is not a column in this Sheet. "
        "Known columns: file_name, theme, title"
    ]


def test_required_for_upload_matching_every_column_passes():
    column_map = build_column_map(["Title", "Theme"])
    config = _sheet_config(required_for_upload=("title", "theme"))
    assert check_required_for_upload(config, column_map) == []


def test_every_missing_name_is_reported_not_just_the_first():
    column_map = build_column_map(["Title"])
    config = _sheet_config(required_for_upload=("titel", "thmee"))
    errors = check_required_for_upload(config, column_map)
    assert len(errors) == 2


def _unresolved_message(directory: Path, name: str) -> str:
    """resolve_file()'s own wording, rebuilt here so the tests below can
    assert the message character-for-character instead of settling for a
    substring. The message embeds an absolute path that varies with
    tmp_path, which is the only reason it has to be interpolated rather
    than written out literally - `.resolve()` because that is what
    resolve_file() itself formats into the message."""
    return (
        f"no file found in '{directory.resolve()}' matching '{name}' (looked for an "
        "exact filename match, then a case-insensitive match ignoring extension)"
    )


def test_a_blank_candidate_is_recorded_as_blank_not_as_an_error(tmp_path):
    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [{"folder": "", "name": ""}]
    outcomes = resolve_sheet_files(rows, config)
    assert outcomes.errors == {}
    assert outcomes.blank == {2: ["folder", "name"]}


def test_a_blank_filename_never_reaches_the_resolver(tmp_path):
    """The cryptic "matching ''" message must be unreachable, not merely
    unlikely. Folder present and filename blank is the shape that produced
    it: pre-change the candidate reached resolve_file() and it complained
    about matching '', so this asserts errors is EMPTY rather than asserting
    over an empty collection, which would pass unconditionally.

    The folder has to exist for this to test what it claims - without the
    mkdir the resolver would fail on the missing folder before it ever got
    as far as matching a name, and the test would pass for the wrong
    reason."""
    (tmp_path / "SOP CD 1").mkdir()
    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [{"folder": "SOP CD 1", "name": ""}]
    outcomes = resolve_sheet_files(rows, config)
    assert outcomes.errors == {}
    assert outcomes.blank == {2: ["name"]}


def test_a_blank_folder_beside_a_filled_filename_is_still_not_ready(tmp_path):
    """A half-catalogued row: the operator named a file but not its folder.
    It routes to `blank` naming only the folder cell, so the report can say
    which cell is missing rather than lumping it in with untouched rows -
    and NOT to `errors`, because a blank cell is not a wrong answer."""
    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [{"folder": "", "name": "Finnis Meat Market.jpg"}]
    outcomes = resolve_sheet_files(rows, config)
    assert outcomes.blank == {2: ["folder"]}
    assert outcomes.errors == {}


def test_a_present_candidate_that_does_not_resolve_is_an_error_not_blank(tmp_path):
    """THE reclassification guard. A broken row silently downgraded to
    not-ready is a row nobody ever fixes."""
    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Finnish Meat Market.jpg").write_bytes(b"")
    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [{"folder": "SOP CD 1", "name": "Finnis Meat Market.jpg"}]
    outcomes = resolve_sheet_files(rows, config)
    assert outcomes.blank == {}
    assert 2 in outcomes.errors
    assert outcomes.errors[2] == _unresolved_message(folder, "Finnis Meat Market.jpg")


def test_a_broken_filename_stays_an_error_even_beside_a_genuinely_blank_row(tmp_path):
    """Both errors here are real rows from a rehearsal against the actual
    drive: 'Finnis' for 'Finnish' (a typo in the Sheet) and "Roy's" for
    'Roy_s' (the apostrophe sanitized away when the files were copied).
    Each names a file the operator believes exists, so each is a defect to
    fix - and neither may be filed as "nobody has got to this row yet"
    just because the blank row 2 in the same run legitimately is.

    Run together on purpose: the reclassification failure is a routing
    bug, and routing is only observable when both destinations are in
    play at once. Exact dict equality on BOTH buckets, so a row leaking
    from one to the other fails the assertion from either side."""
    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Finnish Meat Market.jpg").write_bytes(b"")
    (folder / "Roy_s Shell.jpg").write_bytes(b"")
    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [
        {"folder": "", "name": ""},
        {"folder": "SOP CD 1", "name": "Finnis Meat Market.jpg"},
        {"folder": "SOP CD 1", "name": "Roy's Shell.jpg"},
    ]

    outcomes = resolve_sheet_files(rows, config)

    assert outcomes.blank == {2: ["folder", "name"]}
    assert outcomes.errors == {
        3: _unresolved_message(folder, "Finnis Meat Market.jpg"),
        4: _unresolved_message(folder, "Roy's Shell.jpg"),
    }
    # The unverified candidate must not survive as row['file'] for ANY
    # failing row, blank or broken - left in place it would resolve as a
    # literal path for the later disk check and mask the failure.
    assert [row["file"] for row in rows] == ["", "", ""]


def _sheet_row(**cells: str) -> dict[str, str]:
    """One Sheet row for the readiness tests below. `folder`/`name` are
    short aliases for the two file_template columns _validate's on-disk
    layout below uses (folder_on_lacie_drive/file_name - the real names
    from projects_registry.json), so a test reads as "which cell is
    missing or wrong" rather than repeating the long column names
    throughout. mediatype is always present: SHEET_REQUIRED_COLUMNS still
    checks it structurally, and these tests are about
    required_for_upload/missing_fields, not that check."""
    folder = cells.pop("folder", "SOP CD1")
    name = cells.pop("name", "Finnish Meat Market.jpg")
    row = {
        "mediatype": "image",
        "title": "A Title",
        "theme": "Townscape",
        "folder_on_lacie_drive": folder,
        "file_name": name,
    }
    row.update(cells)
    return row


def _validate(
    rows: list[dict[str, str]], required_for_upload: tuple[str, ...], tmp_path: Path
) -> tuple[list[RowValidation], list[RowValidation]]:
    """Wires resolve_sheet_files into validate_sheet_grid exactly as
    cmd_validate's own two call sites do - so these tests exercise the
    same path FileOutcomes actually travels, not a hand-assembled
    substitute for it.

    Takes the calling test's own tmp_path rather than managing a hidden
    directory of its own: a test asserting the resolver's exact message
    (via _unresolved_message(), per this plan's exact-assertion rule)
    needs a directory it can name back, which a directory this helper
    created and never exposed would not allow.

    Sets up one real file, 'SOP CD1/Finnish Meat Market.jpg', matching
    _sheet_row's defaults, so an unmodified row resolves cleanly and a
    test only has to override the one cell it wants broken or blank."""
    (tmp_path / "SOP CD1").mkdir()
    (tmp_path / "SOP CD1" / "Finnish Meat Market.jpg").write_bytes(b"x")

    config = _sheet_config(
        files_dir=str(tmp_path),
        file_template="{folder_on_lacie_drive}/{file_name}",
        required_for_upload=required_for_upload,
    )
    outcomes = resolve_sheet_files(rows, config)
    return validate_sheet_grid(rows, make_registry(), config, [], outcomes)


def test_a_blank_required_field_is_not_an_error(tmp_path):
    rows = [_sheet_row(title="", theme="Townscape")]
    _, results = _validate(rows, required_for_upload=("title", "theme"), tmp_path=tmp_path)
    assert results[0].errors == []
    assert results[0].missing_fields == ["title"]
    assert results[0].readiness is Readiness.NOT_READY


def test_a_broken_filename_produces_exactly_one_error(tmp_path):
    """The duplicate 'missing required column file' line must be gone. Count
    the errors - asserting 'contains the resolver message' passes just as
    well with the redundant line still there. Exact message equality (not
    a substring) per this plan's rule: a prior review found weakening
    exact assertions to substrings dropped a mutation's catch rate."""
    rows = [_sheet_row(title="Finnish Meat Market", theme="Townscape", name="Finnis.jpg")]
    _, results = _validate(rows, required_for_upload=("title", "theme"), tmp_path=tmp_path)
    assert len(results[0].errors) == 1
    assert results[0].errors[0] == _unresolved_message(tmp_path / "SOP CD1", "Finnis.jpg")


def test_a_broken_filename_in_an_uncatalogued_row_is_both(tmp_path):
    rows = [_sheet_row(title="", theme="", name="Finnis.jpg")]
    _, results = _validate(rows, required_for_upload=("title", "theme"), tmp_path=tmp_path)
    assert results[0].readiness is Readiness.NOT_READY
    assert results[0].is_valid is False


def test_missing_fields_names_the_blank_template_cells_too(tmp_path):
    rows = [_sheet_row(title="A Title", theme="Townscape", folder="", name="")]
    _, results = _validate(rows, required_for_upload=("title", "theme"), tmp_path=tmp_path)
    assert results[0].missing_fields == ["folder_on_lacie_drive", "file_name"]
