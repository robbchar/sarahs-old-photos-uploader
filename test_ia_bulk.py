import json
import csv
import re
from argparse import Namespace

import internetarchive
import pytest
from googleapiclient.errors import HttpError

from column_map import build_column_map
from ia_bulk import (
    read_csv,
    load_registry,
    check_identifier,
    validate_rows,
    RowValidation,
    effective_identifier,
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
    }
    project.update(project_overrides)
    return {"collection_key": "lcps", "projects": {"astoriaphotos": project}}


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
    ever flipped to SHEET_REQUIRED_COLUMNS (which excludes identifier and
    file), a CSV row with both blank would become "valid", and
    effective_identifier("", live=False) returns just "zztest-" - not a
    real identifier."""
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
        {"identifier": "", "ia_uploaded": ""},
        {"identifier": "", "ia_uploaded": ""},
        {"identifier": "lcps-astoriaphotos-00001", "ia_uploaded": ""},
        {"identifier": "lcps-astoriaphotos-00002", "ia_uploaded": "2026-08-08T10:00:00"},
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
    rows = [{"identifier": "", "ia_uploaded": ""}]
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
    rows = [{"identifier": "", "ia_uploaded": ""}]
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
    rows = [{"identifier": "lcps-astoriaphotos-00001", "ia_uploaded": "2026-08-08T10:00:00"}]
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
    rows = [{"identifier": "lcps-astoriaphotos-00001", "ia_uploaded": ""}]
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
        {"identifier": "", "ia_uploaded": ""},
        {"identifier": "", "ia_uploaded": ""},
        {"identifier": "lcps-astoriaphotos-00001", "ia_uploaded": ""},
        {"identifier": "lcps-astoriaphotos-00002", "ia_uploaded": "2026-08-08T10:00:00"},
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
    assertions below would flip. files_dir points at a directory that does
    not exist - Phase 1 runs with no files on disk (see
    test_cmd_validate_does_not_check_file_existence_on_the_sheet_path) - so
    a regression that reintroduced a file check would fail this test too."""
    from ia_bulk import cmd_validate

    grid = [["Title", "file"], ["First photo", "photo1.jpg"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "1/1 rows passed" in out
    assert "will upload these metadata fields:" in out
    assert "1 row ready to upload" in out
    assert "suggestions (advisory - nothing is changed automatically):" in out


def test_cmd_validate_does_not_treat_a_blank_identifier_as_an_error(tmp_path, monkeypatch, capsys):
    """A blank identifier is the normal starting state of every new Sheet
    row under minting, not an error like it is for a pre-assigned CSV row -
    this is the behavior SHEET_REQUIRED_COLUMNS exists to produce."""
    from ia_bulk import cmd_validate

    grid = [["Title", "file", "identifier"], ["First photo", "photo1.jpg", ""]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "missing required column 'identifier'" not in out


def test_cmd_validate_does_not_check_file_existence_on_the_sheet_path(tmp_path, monkeypatch, capsys):
    """Phase 1's explicit contract (see the plan) is 'no IA credentials and
    not a single file on disk' - a human validates a copy of the real Sheet
    on a machine that does not have the ~10,000 photos. This row names a
    file, and files_dir points at a directory that does not even exist, so
    if the disk check were reintroduced this would fail with
    "file not found" instead of passing. `file` itself isn't required
    either (Task 9 supplies it via file_template, not this task)."""
    from ia_bulk import cmd_validate

    grid = [["Title", "file"], ["First photo", "photo-that-does-not-exist.jpg"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "1/1 rows passed" in out
    assert "file not found" not in out


def test_cmd_validate_does_not_require_a_file_column_on_the_sheet_path(tmp_path, monkeypatch, capsys):
    """Distinct from the disk-existence check above: 'file' must not even be
    in SHEET_REQUIRED_COLUMNS. A Sheet has no 'file' column at all in Phase
    1 - Task 9 is what builds one via file_template - so a row lacking it
    entirely (not just an unchecked one) must still pass."""
    from ia_bulk import cmd_validate

    grid = [["Title"], ["First photo"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "missing required column 'file'" not in out


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

    grid = [
        ["Genre / Form", "Genre_ Form", "Title"],
        ["a", "b", "First"],
        ["c", "d", "Second", "unexpected extra cell"],
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
    assert "[FAIL] row 1" in out
    assert "[FAIL] row 3" in out
    assert "both normalize to field name 'genre_form'" in out
    assert "more field(s) than the header" in out


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
    grid = [["Title"], ["First photo"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path), mediatype="phonorecord")),
        encoding="utf-8",
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    cmd_validate(args)

    assert captured_rows == [{"title": "First photo", "mediatype": "phonorecord"}]


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

    grid = [
        ["Genre / Form", "Genre_ Form", "Title"],  # colliding headers -> a structural error
        ["a", "b", "First photo"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
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

    grid = [
        ["Title", "identifier", "ia_uploaded"],
        ["Ready", "", ""],
        ["", "", ""],  # blank title -> fails validation, still unassigned
        ["Done", "lcps-astoriaphotos-00001", "2026-01-01T00:00:00"],
        ["", "lcps-astoriaphotos-00002", "2026-01-01T00:00:00"],  # blank title -> fails
        ["Reserved", "lcps-astoriaphotos-00003", ""],
        ["", "lcps-astoriaphotos-00004", ""],  # blank title -> fails
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

    grid = [["Title", "Photographer"], ["First photo", "Jane Doe"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
    )
    args = Namespace(csv=None, project="astoriaphotos", registry=str(registry_path), live=False)

    cmd_validate(args)
    out = capsys.readouterr().out

    out.encode("ascii")  # raises UnicodeEncodeError if any non-ASCII character slipped in


def test_cmd_validate_prints_an_actual_suggestion_not_just_the_heading(tmp_path, monkeypatch, capsys):
    """Only the "suggestions (advisory ...)" heading was previously
    asserted anywhere - deleting the loop that prints each suggestion,
    keeping only the heading, passed every test. This is one of the three
    things the Phase 1 handoff session exists to exercise."""
    from ia_bulk import cmd_validate

    grid = [["Title", "Photographer"], ["First photo", "Jane Doe"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
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


def test_effective_identifier_prepends_zztest_when_not_live():
    assert effective_identifier("lcps-astoriaphotos-00001", live=False) == "zztest-lcps-astoriaphotos-00001"


def test_effective_identifier_returns_identifier_unchanged_when_live():
    assert effective_identifier("lcps-astoriaphotos-00001", live=True) == "lcps-astoriaphotos-00001"


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
    assert "[1/2] uploading zztest-lcps-astoriaphotos-00001 (photo1.jpg)" in out
    assert "[2/2] uploading zztest-lcps-astoriaphotos-00002 (photo2.jpg)" in out
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
    assert entry["uploaded_as"] == "zztest-lcps-astoriaphotos-00001"
    assert entry["status"] == "success"


def test_cmd_upload_uses_real_identifier_as_target_when_live(tmp_path, monkeypatch):
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
    assert entry["uploaded_as"] == "zztest-lcps-astoriaphotos-00001"


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
        ("upload", ["items.csv"]),
        ("sync-metadata", ["updates.csv"]),
    ],
)
def test_build_parser_requires_project_on_every_subcommand(subcommand, extra_args):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([subcommand, *extra_args])


def test_build_parser_upload_subcommand_defaults_to_not_live():
    parser = build_parser()
    args = parser.parse_args(["upload", "items.csv", "--project", "astoriaphotos"])
    assert args.command == "upload"
    assert args.project == "astoriaphotos"
    assert args.live is False
    assert args.collection == "lcps"
    assert args.resume_from is None


def test_build_parser_upload_subcommand_accepts_live_and_resume_from():
    parser = build_parser()
    args = parser.parse_args(
        [
            "upload",
            "items.csv",
            "--project",
            "astoriaphotos",
            "--live",
            "--resume-from",
            "logs/upload-x.jsonl",
        ]
    )
    assert args.live is True
    assert args.resume_from == "logs/upload-x.jsonl"


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
