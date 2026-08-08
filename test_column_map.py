import pytest

from column_map import normalize_header, build_column_map, grid_to_rows, is_held_back


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
