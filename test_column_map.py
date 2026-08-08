import pytest

from column_map import normalize_header


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
