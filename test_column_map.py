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
