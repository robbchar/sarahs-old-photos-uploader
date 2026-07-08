import json
import csv
import time
from argparse import Namespace
from pathlib import Path

import internetarchive
import pytest

from ia_bulk import (
    read_rows,
    load_registry,
    check_identifier,
    validate_rows,
    RowValidation,
    check_live_safety,
    log_result,
    build_parser,
    main,
)


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
    from ia_bulk import log_result, load_prior_successes

    log_path = tmp_path / "upload-test.jsonl"
    log_result(log_path, "lcps-astoriaphotos-00001", "photo1.jpg", "success")
    log_result(log_path, "lcps-astoriaphotos-00002", "photo2.jpg", "failure", error="timeout")

    successes = load_prior_successes(log_path)

    assert successes == {"lcps-astoriaphotos-00001"}


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


def test_upload_row_succeeds_when_library_returns_ok_responses(tmp_path, monkeypatch):
    from ia_bulk import upload_row

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
    from ia_bulk import upload_row

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


def test_update_metadata_row_succeeds_when_library_returns_ok_response(monkeypatch):
    from ia_bulk import update_metadata_row

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
    from ia_bulk import update_metadata_row

    row = {"identifier": "zztest-astoriaphotos-00001", "title": "Updated title"}

    def fake_modify_metadata(identifier, metadata, **kwargs):
        return FakeResponse(ok=False, status_code=400, text="Bad Request")

    monkeypatch.setattr(internetarchive, "modify_metadata", fake_modify_metadata)

    with pytest.raises(RuntimeError, match="400"):
        update_metadata_row(row)


def test_validate_identifiers_passes_valid_unique_identifiers():
    from ia_bulk import validate_identifiers

    rows = [
        {"identifier": "lcps-astoriaphotos-00001", "title": "New title"},
        {"identifier": "lcps-astoriaphotos-00002", "title": "Another title"},
    ]

    results = validate_identifiers(rows, registry=make_registry())

    assert all(r.is_valid for r in results)


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


def test_cmd_upload_writes_success_log_and_returns_zero(tmp_path, monkeypatch):
    from ia_bulk import cmd_upload

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
    from ia_bulk import cmd_upload

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
    from ia_bulk import cmd_upload

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


def test_cmd_sync_metadata_writes_success_log_and_returns_zero(tmp_path, monkeypatch):
    from ia_bulk import cmd_sync_metadata

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
    from ia_bulk import cmd_sync_metadata

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


def test_cmd_sync_metadata_blocks_real_identifiers_without_live_flag(tmp_path, monkeypatch):
    from ia_bulk import cmd_sync_metadata

    csv_path = tmp_path / "updates.csv"
    write_csv(
        csv_path,
        ["identifier", "title"],
        [{"identifier": "lcps-astoriaphotos-00001", "title": "Corrected title"}],
    )
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "lcps", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )

    update_calls = []
    monkeypatch.setattr("ia_bulk.update_metadata_row", lambda row: update_calls.append(row))

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


def test_cmd_sync_metadata_fails_identifier_validation_before_touching_network(tmp_path, monkeypatch):
    from ia_bulk import cmd_sync_metadata

    csv_path = tmp_path / "updates.csv"
    write_csv(
        csv_path,
        ["identifier", "title"],
        [{"identifier": "zztest-unregisteredproject-00001", "title": "Corrected title"}],
    )
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "lcps", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )

    update_calls = []
    monkeypatch.setattr("ia_bulk.update_metadata_row", lambda row: update_calls.append(row))

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
