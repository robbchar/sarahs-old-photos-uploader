from identifiers import (
    RowState,
    classify_row,
    format_identifier,
    next_identifiers,
    parse_number,
)


def test_format_identifier_zero_pads_to_five_digits():
    assert format_identifier("lcps", "sarahsoldphotos", 9) == "lcps-sarahsoldphotos-00009"


def test_parse_number_reads_the_trailing_segment():
    assert parse_number("lcps-sarahsoldphotos-00042") == 42


def test_parse_number_returns_none_for_a_non_identifier():
    assert parse_number("") is None
    assert parse_number("not-an-identifier") is None


def test_next_identifiers_continues_from_the_highest_existing():
    existing = ["lcps-sarahsoldphotos-00001", "lcps-sarahsoldphotos-00007"]

    assert next_identifiers(existing, "lcps", "sarahsoldphotos", 2) == [
        "lcps-sarahsoldphotos-00008",
        "lcps-sarahsoldphotos-00009",
    ]


def test_next_identifiers_starts_at_one_when_the_sheet_is_empty():
    assert next_identifiers([], "lcps", "sarahsoldphotos", 1) == [
        "lcps-sarahsoldphotos-00001"
    ]


def test_next_identifiers_ignores_other_projects():
    """A second LCPS project shares the Sheet's collection key but numbers
    independently, so its identifiers must not advance this project's counter."""
    existing = ["lcps-otherproject-00500", "lcps-sarahsoldphotos-00002"]

    assert next_identifiers(existing, "lcps", "sarahsoldphotos", 1) == [
        "lcps-sarahsoldphotos-00003"
    ]


def test_next_identifiers_ignores_other_projects_multiple_requests():
    """Requesting multiple identifiers confirms project filter is applied
    consistently, not just on the first item."""
    existing = ["lcps-otherproject-00500", "lcps-sarahsoldphotos-00002"]

    assert next_identifiers(existing, "lcps", "sarahsoldphotos", 3) == [
        "lcps-sarahsoldphotos-00003",
        "lcps-sarahsoldphotos-00004",
        "lcps-sarahsoldphotos-00005",
    ]


def test_next_identifiers_leaves_gaps_alone():
    """A gap means a run reserved a number and died before uploading. Reusing it
    risks colliding with an item that did land on IA."""
    existing = ["lcps-sarahsoldphotos-00001", "lcps-sarahsoldphotos-00005"]

    assert next_identifiers(existing, "lcps", "sarahsoldphotos", 1) == [
        "lcps-sarahsoldphotos-00006"
    ]


def test_next_identifiers_leaves_multiple_gaps_alone():
    """Multiple gaps confirm that minting always continues from the highest
    number, never reusing any lower gaps."""
    existing = [
        "lcps-sarahsoldphotos-00001",
        "lcps-sarahsoldphotos-00003",
        "lcps-sarahsoldphotos-00007",
    ]

    assert next_identifiers(existing, "lcps", "sarahsoldphotos", 2) == [
        "lcps-sarahsoldphotos-00008",
        "lcps-sarahsoldphotos-00009",
    ]


def test_classify_row_unassigned():
    assert classify_row({"identifier": "", "ia_uploaded": ""}) is RowState.UNASSIGNED


def test_classify_row_reserved_but_unconfirmed():
    row = {"identifier": "lcps-sarahsoldphotos-00001", "ia_uploaded": ""}

    assert classify_row(row) is RowState.RESERVED


def test_classify_row_done():
    row = {"identifier": "lcps-sarahsoldphotos-00001", "ia_uploaded": "2026-08-08T10:00:00"}

    assert classify_row(row) is RowState.DONE
