import pytest

from column_map import (
    normalize_header,
    build_column_map,
    grid_to_rows,
    is_held_back,
    check_column_map,
    check_grid_shape,
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
    column_map = build_column_map(["Title", "identifier", "ia_uploaded", "ia_url"])

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
