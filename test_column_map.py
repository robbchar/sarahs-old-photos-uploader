from pathlib import Path
from unittest import mock

import pytest

from column_map import (
    FileResolutionError,
    RESERVED_FIELDS,
    TemplateError,
    build_column_map,
    candidate_path,
    check_column_map,
    check_file_template,
    check_grid_shape,
    grid_to_rows,
    is_held_back,
    normalize_header,
    resolve_file,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Title", "title"),
        ("Physical Format", "physical_format"),
        ("Genre / Form", "genre_form"),
        ("Names (Last, First M.)", "names_last_first_m"),
        ("Subtheme (Optional)", "subtheme_optional"),
        ("Architectura Style", "architectura_style"),
        ("Place ", "place"),
        ("  Date  ", "date"),
        ("File on Array", "file_on_array"),
        ("Genre_ Form", "genre_form"),
        ("Co-op Notes", "co-op_notes"),
    ],
)
def test_normalize_header(raw, expected):
    assert normalize_header(raw) == expected


def test_held_back_marker_is_case_insensitive():
    assert is_held_back("Notes (LCPS Internal)")
    assert is_held_back("notes (lcps internal)")
    assert not is_held_back("Notes")


def test_build_column_map_separates_held_back_columns():
    column_map = build_column_map(["Title", "Genre / Form", "Notes (LCPS Internal)"])

    assert column_map.field_names["Title"] == "title"
    assert column_map.held_back == ["Notes (LCPS Internal)"]
    assert column_map.uploadable_fields() == ["title", "genre_form"]


def test_uploadable_fields_excludes_reserved_names():
    column_map = build_column_map(
        ["Title", "ia_identifier", "ia_identifier_bib", "ia_uploaded", "ia_url"]
    )

    assert column_map.uploadable_fields() == ["title"]


def test_grid_to_rows_keys_rows_by_normalized_name():
    grid = [
        ["Title", "Genre / Form", "Notes (LCPS Internal)"],
        ["Alderbrook Hall", "Photographs", "donor said 1958?"],
    ]

    column_map, rows = grid_to_rows(grid)

    assert rows == [
        {
            "title": "Alderbrook Hall",
            "genre_form": "Photographs",
            "notes_lcps_internal": "donor said 1958?",
        }
    ]
    assert column_map.held_back == ["Notes (LCPS Internal)"]


def test_grid_to_rows_pads_short_rows():
    """The Sheets API omits trailing empty cells, so a row can be shorter than
    the header. That is normal, not a ragged-CSV error."""
    grid = [["Title", "Date"], ["Alderbrook Hall"]]

    _column_map, rows = grid_to_rows(grid)

    assert rows == [{"title": "Alderbrook Hall", "date": ""}]


def test_grid_to_rows_on_empty_grid():
    column_map, rows = grid_to_rows([])

    assert rows == []
    assert column_map.headers == []


def test_check_column_map_collision_different_spellings():
    """Two headers that normalize to the same field name should be detected."""
    column_map = build_column_map(["Genre / Form", "Genre_ Form"])

    errors = check_column_map(column_map)

    assert len(errors) == 1
    assert "Genre / Form" in errors[0] and "Genre_ Form" in errors[0]
    assert "genre_form" in errors[0]


def test_check_column_map_collision_identical_headers():
    """Identical headers should be detected as a collision."""
    column_map = build_column_map(["Title", "Title"])

    errors = check_column_map(column_map)

    assert len(errors) == 1
    assert "Title" in errors[0]
    assert "title" in errors[0]


def test_check_column_map_empty_field_name():
    """Headers that normalize to empty strings should be detected."""
    column_map = build_column_map(["!!!", "Title"])

    errors = check_column_map(column_map)

    assert len(errors) == 1
    assert "!!!" in errors[0]
    assert "empty" in errors[0].lower()


def test_check_column_map_multiple_blank_headers_produce_one_combined_error():
    """Previously: N headers normalizing to "" produced N empty-field-name
    messages plus N-1 pairwise "both normalize to the same field name"
    collision messages against each other - three blank columns produced
    five overlapping messages for one root cause. They must be reported
    once, together, naming each blank header."""
    column_map = build_column_map(["", "!!!", "###", "Title"])

    errors = check_column_map(column_map)

    assert len(errors) == 1
    assert "!!!" in errors[0]
    assert "###" in errors[0]


def test_check_column_map_clean():
    """A clean ColumnMap with no collisions should produce no errors."""
    column_map = build_column_map(["Title", "Date", "Genre / Form"])

    errors = check_column_map(column_map)

    assert errors == []


def test_check_grid_shape_long_row():
    """A data row with more cells than the header should be detected."""
    grid = [["Title", "Date"], ["Alderbrook Hall", "1958", "stray", "extra"]]

    errors = check_grid_shape(grid)

    assert len(errors) == 1
    assert "row 2" in errors[0]
    assert "2 more field(s)" in errors[0]


def test_check_grid_shape_short_row_no_error():
    """Short rows (Sheets API behavior) should not produce an error."""
    grid = [["Title", "Date"], ["Alderbrook Hall"]]

    errors = check_grid_shape(grid)

    assert errors == []


def test_check_grid_shape_empty_grid():
    """An empty grid should produce no errors."""
    grid: list[list[str]] = []

    errors = check_grid_shape(grid)

    assert errors == []


def test_reserved_fields_are_all_ia_prefixed_except_file():
    assert RESERVED_FIELDS == {
        "file",
        "ia_identifier",
        "ia_identifier_bib",
        "ia_uploaded",
        "ia_url",
    }


def test_identifier_is_no_longer_reserved_so_donor_references_upload():
    """The real Sheet's `Identifier` column holds the donor's original
    reference. It must reach IA as ordinary metadata, not be swallowed."""
    column_map = build_column_map(["Title", "Identifier"])

    assert "identifier" in column_map.uploadable_fields()


def test_check_file_template_names_a_missing_column_at_startup():
    column_map = build_column_map(["File on Array"])

    with pytest.raises(TemplateError, match="identifier"):
        check_file_template("{file_on_array}/{identifier}", column_map)


def test_check_file_template_explains_the_normalized_form_and_lists_what_exists():
    """The realistic mistake is writing raw header text in the template. An
    operator hit exactly this with '{Folder on LaCie Drive}/{File Name}' against
    a Sheet that genuinely had both columns, and the message gave them nothing
    to act on."""
    column_map = build_column_map(["Folder on LaCie Drive", "File Name", "Title"])

    with pytest.raises(TemplateError) as excinfo:
        check_file_template("{Folder on LaCie Drive}/{File Name}", column_map)

    message = str(excinfo.value)
    assert "NORMALIZED" in message
    # names the correct form to use...
    assert "folder_on_lacie_drive" in message
    assert "file_name" in message
    # ...and the rest of what is available, so the fix needs no guesswork
    assert "title" in message


def test_candidate_path_joins_the_two_columns():
    row = {"file_on_array": "SOP CD1", "identifier": "CD 1 01 53 58 1 Central SS"}

    assert (
        candidate_path("{file_on_array}/{identifier}", row)
        == "SOP CD1/CD 1 01 53 58 1 Central SS"
    )


def test_resolve_file_prefers_an_exact_match(tmp_path):
    folder = tmp_path / "SOP CD5"
    folder.mkdir()
    (folder / "Liberty.jpg").write_bytes(b"x")

    assert resolve_file(tmp_path, "SOP CD5/Liberty.jpg", {}) == "SOP CD5/Liberty.jpg"


def test_resolve_file_finds_a_stem_match_when_the_sheet_omits_the_extension(tmp_path):
    """225 of 234 real rows look like this."""
    folder = tmp_path / "SOP CD1"
    folder.mkdir()
    (folder / "CD 1 01 53 58 1 Central SS.jpg").write_bytes(b"x")

    resolved = resolve_file(tmp_path, "SOP CD1/CD 1 01 53 58 1 Central SS", {})

    assert resolved == "SOP CD1/CD 1 01 53 58 1 Central SS.jpg"


def test_resolve_file_matches_a_stem_case_insensitively(tmp_path):
    folder = tmp_path / "SOP CD1"
    folder.mkdir()
    (folder / "Alderbrook Hall.JPG").write_bytes(b"x")

    assert resolve_file(tmp_path, "SOP CD1/alderbrook hall", {}) == "SOP CD1/Alderbrook Hall.JPG"


def test_resolve_file_accepts_any_extension_not_just_jpg(tmp_path):
    folder = tmp_path / "SOP CD3"
    folder.mkdir()
    (folder / "Master.tiff").write_bytes(b"x")

    assert resolve_file(tmp_path, "SOP CD3/Master", {}) == "SOP CD3/Master.tiff"


def test_resolve_file_refuses_to_choose_between_two_matching_stems(tmp_path):
    """An item's identifier is permanent; picking between a JPEG and a TIFF on
    the operator's behalf is exactly the silent decision this project rejects."""
    folder = tmp_path / "SOP CD5"
    folder.mkdir()
    (folder / "Liberty.jpg").write_bytes(b"x")
    (folder / "Liberty.tif").write_bytes(b"x")

    with pytest.raises(FileResolutionError) as excinfo:
        resolve_file(tmp_path, "SOP CD5/Liberty", {})

    message = str(excinfo.value)
    assert "Liberty.jpg" in message and "Liberty.tif" in message


def test_resolve_file_reports_a_missing_file_with_the_directory_it_searched(tmp_path):
    (tmp_path / "SOP CD1").mkdir()

    with pytest.raises(FileResolutionError, match="SOP CD1"):
        resolve_file(tmp_path, "SOP CD1/Nothing Here", {})


def test_resolve_file_reports_a_missing_directory_distinctly(tmp_path):
    with pytest.raises(FileResolutionError, match="no such folder|does not exist"):
        resolve_file(tmp_path, "SOP CD9/Anything", {})


def test_resolve_file_scans_each_directory_only_once(tmp_path):
    """10,000 rows across a handful of folders must not mean 10,000 scans."""
    folder = tmp_path / "SOP CD1"
    folder.mkdir()
    for n in range(3):
        (folder / f"photo{n}.jpg").write_bytes(b"x")

    cache: dict = {}
    for n in range(3):
        resolve_file(tmp_path, f"SOP CD1/photo{n}", cache)

    assert len(cache) == 1


# --- Fix round: review findings against the real-drive resolution path ---


def test_candidate_path_strips_field_values_before_joining():
    """A trailing/leading space in a Sheet cell (e.g. a folder column typed
    as 'SOP CD1 ') must not survive into the candidate - on Windows it
    produces a path where is_dir() and iterdir() disagree about whether the
    folder exists (see the OSError-wrapping test below), and even off
    Windows it would just fail to match a real folder named without the
    space."""
    row = {"file_on_array": " SOP CD1 ", "identifier": " Central SS "}

    assert (
        candidate_path("{file_on_array}/{identifier}", row) == "SOP CD1/Central SS"
    )


def test_resolve_file_wraps_a_directory_scan_oserror_as_a_file_resolution_error(tmp_path):
    """On Windows, a folder cell with a trailing space can leave is_dir()
    reporting True (the OS normalizes a trailing space away when it stats
    the path) while iterdir() on the SAME path still raises
    FileNotFoundError - a disagreement between the two checks that
    otherwise reaches cmd_validate as a bare, unhandled traceback instead
    of a per-row FileResolutionError. A disconnected external drive
    mid-scan, or a permissions error, hits the same code path.

    Simulated directly via mock rather than relying on the Windows-specific
    trailing-space quirk to reproduce, so the test is deterministic and
    pins the actual contract - ANY OSError during the scan becomes
    FileResolutionError - rather than one specific trigger for it."""
    folder = tmp_path / "SOP CD1"
    folder.mkdir()

    def raising_iterdir(self):
        raise FileNotFoundError(f"[WinError 3] simulated: '{self}'")

    with mock.patch.object(Path, "iterdir", raising_iterdir):
        with pytest.raises(FileResolutionError):
            resolve_file(tmp_path, "SOP CD1/Anything", {})


def test_resolve_file_does_not_treat_a_non_extension_dot_in_the_target_as_an_extension(tmp_path):
    """'Report.1958' is an archival reference, not a filename with a
    '.1958' extension. Chopping the TARGET's own trailing segment before
    comparing (the old bug) silently resolved this to 'Report.jpg' - the
    only entry whose stem matched 'report' under that wrong comparison -
    with no ambiguity ever reported. Asserts on the resolved file's actual
    BYTES, not just the returned name string, since a wrong-but-plausible-
    looking path is exactly the failure mode this pins."""
    folder = tmp_path / "SOP CD1"
    folder.mkdir()
    (folder / "Report.jpg").write_bytes(b"WRONG-FILE")
    (folder / "Report.1958.tif").write_bytes(b"CORRECT-FILE")

    resolved = resolve_file(tmp_path, "SOP CD1/Report.1958", {})

    assert resolved == "SOP CD1/Report.1958.tif"
    assert (tmp_path / resolved).read_bytes() == b"CORRECT-FILE"


def test_resolve_file_matches_a_target_containing_a_dot_that_is_not_an_extension(tmp_path):
    """The benign variant of the same bug: a target with a dot that isn't
    an extension must still find its file, not merely avoid finding the
    wrong one."""
    folder = tmp_path / "SOP CD1"
    folder.mkdir()
    (folder / "CD 1.01.53.jpg").write_bytes(b"x")

    assert resolve_file(tmp_path, "SOP CD1/CD 1.01.53", {}) == "SOP CD1/CD 1.01.53.jpg"


def test_an_explicit_extension_wins_over_an_ambiguous_stem(tmp_path):
    """The exact-match branch (step 1) must actually be taken, not merely
    happen to agree with the stem-matching fallback: with two files
    sharing a stem, only the exact match correctly picks one without
    raising ambiguity."""
    folder = tmp_path / "SOP CD5"
    folder.mkdir()
    (folder / "Liberty.jpg").write_bytes(b"x")
    (folder / "Liberty.tif").write_bytes(b"x")

    assert resolve_file(tmp_path, "SOP CD5/Liberty.jpg", {}) == "SOP CD5/Liberty.jpg"


def test_resolve_file_still_matches_when_the_sheets_extension_case_differs_from_disk(tmp_path):
    """Pass 3's fallback: the Sheet cell already carries a real extension
    but in a different case than disk (exported as '.jpg', the file is
    '.JPEG') - must still resolve via the target-chopped fallback, not
    just the untouched-full-string comparison in pass 2."""
    folder = tmp_path / "SOP CD1"
    folder.mkdir()
    (folder / "Liberty.JPEG").write_bytes(b"x")

    assert resolve_file(tmp_path, "SOP CD1/Liberty.jpg", {}) == "SOP CD1/Liberty.JPEG"


def test_resolve_file_refuses_to_escape_files_dir_via_an_absolute_folder_cell(tmp_path):
    """files_dir is a boundary, not a suggestion. Path(files_dir) / part
    silently discards files_dir entirely when part is itself an absolute
    path, so a Sheet cell that accidentally holds a full path (someone
    pasted 'C:/Windows/System32' instead of 'SOP CD1') must not be
    honored."""
    escape_target = tmp_path / "outside"
    escape_target.mkdir()
    (escape_target / "Secret.txt").write_bytes(b"x")

    files_dir = tmp_path / "files"
    files_dir.mkdir()

    candidate = f"{escape_target.as_posix()}/Secret"

    with pytest.raises(FileResolutionError, match="files_dir"):
        resolve_file(files_dir, candidate, {})


def test_resolve_file_refuses_to_escape_files_dir_via_dot_dot(tmp_path):
    """A '..' folder segment must not walk back out of files_dir either -
    Path silently normalizes '..' away, so the same containment check that
    catches an absolute path must also catch this."""
    files_dir = tmp_path / "files"
    files_dir.mkdir()
    (tmp_path / "Secret.txt").write_bytes(b"x")

    with pytest.raises(FileResolutionError, match="files_dir"):
        resolve_file(files_dir, "../Secret", {})


def test_resolve_file_refuses_a_blank_folder_segment_rather_than_searching_files_dirs_root(
    tmp_path,
):
    """candidate_path can produce something like '/Liberty' when a
    two-column template's folder cell is blank - directory_part is then
    "", and Path(files_dir) / "" is just files_dir, so this would
    otherwise silently search files_dir's own ROOT (and might resolve
    against an unrelated file living there) instead of failing on what is
    really a missing required cell."""
    (tmp_path / "Liberty.jpg").write_bytes(b"x")  # a decoy at files_dir's own root

    with pytest.raises(FileResolutionError, match="folder"):
        resolve_file(tmp_path, "/Liberty", {})


def test_resolve_file_still_searches_files_dirs_root_for_a_single_segment_template(tmp_path):
    """The blank-folder-segment guard above must not misfire on a
    single-column file_template (e.g. '{file}') that has no folder concept
    at all - there, searching files_dir's own root is correct, not a
    blank-cell defect. Distinguished from the '/Liberty' case by whether a
    '/' was present in the candidate at all, not by directory_part alone
    (both are "" in either case)."""
    (tmp_path / "Liberty.jpg").write_bytes(b"x")

    assert resolve_file(tmp_path, "Liberty", {}) == "Liberty.jpg"
