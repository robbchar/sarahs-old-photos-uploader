import pytest

from identifiers import (
    RowState,
    classify_row,
    format_identifier,
    next_identifiers,
    parse_identifier,
    parse_number,
)


def test_format_identifier_zero_pads_to_five_digits():
    assert format_identifier("lcps", "sarasoldphotos", 9) == "lcps-sarasoldphotos-00009"


def test_parse_number_reads_the_trailing_segment():
    assert parse_number("lcps-sarasoldphotos-00042") == 42


def test_parse_number_returns_none_for_a_non_identifier():
    assert parse_number("") is None
    assert parse_number("not-an-identifier") is None


def test_next_identifiers_continues_from_the_highest_existing():
    existing = ["lcps-sarasoldphotos-00001", "lcps-sarasoldphotos-00007"]

    assert next_identifiers(existing, "lcps", "sarasoldphotos", 2) == [
        "lcps-sarasoldphotos-00008",
        "lcps-sarasoldphotos-00009",
    ]


def test_next_identifiers_starts_at_one_when_the_sheet_is_empty():
    assert next_identifiers([], "lcps", "sarasoldphotos", 1) == [
        "lcps-sarasoldphotos-00001"
    ]


def test_next_identifiers_ignores_other_projects():
    """A second LCPS project shares the Sheet's collection key but numbers
    independently, so its identifiers must not advance this project's counter."""
    existing = ["lcps-otherproject-00500", "lcps-sarasoldphotos-00002"]

    assert next_identifiers(existing, "lcps", "sarasoldphotos", 1) == [
        "lcps-sarasoldphotos-00003"
    ]


def test_next_identifiers_ignores_other_projects_multiple_requests():
    """Requesting multiple identifiers confirms project filter is applied
    consistently, not just on the first item."""
    existing = ["lcps-otherproject-00500", "lcps-sarasoldphotos-00002"]

    assert next_identifiers(existing, "lcps", "sarasoldphotos", 3) == [
        "lcps-sarasoldphotos-00003",
        "lcps-sarasoldphotos-00004",
        "lcps-sarasoldphotos-00005",
    ]


def test_next_identifiers_leaves_gaps_alone():
    """A gap means a run reserved a number and died before uploading. Reusing it
    risks colliding with an item that did land on IA."""
    existing = ["lcps-sarasoldphotos-00001", "lcps-sarasoldphotos-00005"]

    assert next_identifiers(existing, "lcps", "sarasoldphotos", 1) == [
        "lcps-sarasoldphotos-00006"
    ]


def test_next_identifiers_leaves_multiple_gaps_alone():
    """Multiple gaps confirm that minting always continues from the highest
    number, never reusing any lower gaps."""
    existing = [
        "lcps-sarasoldphotos-00001",
        "lcps-sarasoldphotos-00003",
        "lcps-sarasoldphotos-00007",
    ]

    assert next_identifiers(existing, "lcps", "sarasoldphotos", 2) == [
        "lcps-sarasoldphotos-00008",
        "lcps-sarasoldphotos-00009",
    ]


def test_classify_row_unassigned():
    assert classify_row({"ia_identifier": "", "ia_uploaded": ""}) is RowState.UNASSIGNED


def test_classify_row_reserved_but_unconfirmed():
    row = {"ia_identifier": "lcps-sarasoldphotos-00001", "ia_uploaded": ""}

    assert classify_row(row) is RowState.RESERVED


def test_classify_row_done():
    row = {"ia_identifier": "lcps-sarasoldphotos-00001", "ia_uploaded": "2026-08-08T10:00:00"}

    assert classify_row(row) is RowState.DONE


def test_classify_row_reads_the_ia_identifier_column():
    assert classify_row({"ia_identifier": "", "ia_uploaded": ""}) is RowState.UNASSIGNED
    assert (
        classify_row({"ia_identifier": "lcps-p-00001", "ia_uploaded": ""})
        is RowState.RESERVED
    )
    assert (
        classify_row({"ia_identifier": "lcps-p-00001", "ia_uploaded": "2026-08-16T10:00:00"})
        is RowState.DONE
    )


def test_classify_row_ignores_a_donor_identifier_column():
    """The real Sheet's `Identifier` column holds donor references like
    'CD 1 01 53 58 1 Central SS'. Reading it as a minted identifier is what
    made all 234 rows report as 'reserved but invalid'."""
    row = {"identifier": "CD 1 01 53 58 1 Central SS", "ia_identifier": "", "ia_uploaded": ""}

    assert classify_row(row) is RowState.UNASSIGNED


def test_format_identifier_succeeds_at_max_5_digit_boundary():
    """Minting exactly at the 5-digit maximum (99999) must succeed; it is the
    last valid identifier before the space is exhausted."""
    assert format_identifier("lcps", "sarasoldphotos", 99999) == "lcps-sarasoldphotos-99999"


def test_format_identifier_raises_when_exceeding_5_digit_limit():
    """Minting past the 5-digit maximum (100000) raises ValueError to prevent
    silent wrapping into a duplicate identifier that parse_number cannot read."""
    with pytest.raises(ValueError, match="exceeds 5-digit maximum"):
        format_identifier("lcps", "sarasoldphotos", 100000)


def test_next_identifiers_raises_when_maximum_exhausted():
    """When the highest existing identifier is 99999 (the maximum for 5 digits),
    attempting to mint any more must raise rather than silently produce an
    unparseable identifier that rounds back to a duplicate."""
    existing = ["lcps-sarasoldphotos-99999"]

    with pytest.raises(ValueError, match="exceeds 5-digit maximum"):
        next_identifiers(existing, "lcps", "sarasoldphotos", 1)


def test_parse_identifier_splits_a_well_formed_identifier():
    assert parse_identifier("lcps-sarasoldphotos-00042") == ("lcps", "sarasoldphotos", 42)


@pytest.mark.parametrize(
    "bad",
    [
        "lcps-sarasoldphotos-42",       # not zero-padded to NUMBER_WIDTH
        "lcps-sarasoldphotos-000042",   # too wide
        "LCPS-sarasoldphotos-00042",    # scheme is lowercase
        "lcps_sarasoldphotos_00042",    # wrong separator
        "CD 1 01 53 58 1 Central SS",   # a donor archival reference
        "",
    ],
)
def test_parse_identifier_returns_none_for_anything_off_scheme(bad):
    assert parse_identifier(bad) is None


def test_parse_identifier_tolerates_surrounding_whitespace():
    """Sheet cells routinely carry a stray space."""
    assert parse_identifier("  lcps-sarasoldphotos-00042  ") == ("lcps", "sarasoldphotos", 42)


def test_parse_number_and_parse_identifier_agree():
    """parse_number is expressed in terms of parse_identifier so the scheme
    is decoded in exactly one place - ia_bulk.py used to carry a second copy
    of the pattern with NUMBER_WIDTH's 5 hardcoded into it."""
    for identifier in ("lcps-sarasoldphotos-00042", "not-an-identifier"):
        parsed = parse_identifier(identifier)
        assert parse_number(identifier) == (parsed[2] if parsed else None)
