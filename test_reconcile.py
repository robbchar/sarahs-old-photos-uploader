import pytest

from reconcile import digit_runs, levenshtein, normalize_name


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Roy's Shell", "roy s shell"),
        (" Roy_s  Shell ", "roy s shell"),          # the real copy artifact
        ("CD 1 01 52 21 1", "cd 1 01 52 21 1"),
        ("001_seaside_beach", "001 seaside beach"),
        ("Finnish Meat Market", "finnish meat market"),
    ],
)
def test_normalize_name_flattens_punctuation_and_case(raw, expected):
    """The two real mismatches differ only in punctuation, spacing and case;
    normalizing makes them compare equal without any fuzzy matching."""
    assert normalize_name(raw) == expected


def test_normalize_name_collapses_runs_not_just_single_characters():
    assert normalize_name("a___b   c") == "a b c"


@pytest.mark.parametrize(
    "a,b,expected",
    [("finnis", "finnish", 1), ("kitten", "sitting", 3), ("same", "same", 0), ("", "abc", 3)],
)
def test_levenshtein(a, b, expected):
    assert levenshtein(a, b) == expected


def test_digit_runs_extracts_numbers_in_order():
    """Pass B compares these: two photographs whose only difference is a
    sequence number are one edit apart and must never be proposed for each
    other."""
    assert digit_runs("cd 1 01 53 34 2 finnis meat market") == ("1", "01", "53", "34", "2")
    assert digit_runs("001 seaside beach") == ("001",)
    assert digit_runs("no numbers here") == ()
