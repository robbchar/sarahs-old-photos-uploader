import pytest

from reconcile import AmbiguousMatch, Proposal, digit_runs, levenshtein, normalize_name, propose_match


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


def test_propose_match_pass_a_matches_on_punctuation_and_spacing():
    """The real Roy's Shell case: not a typo at all, a copy artifact."""
    proposal = propose_match("Roy's Shell.jpg", [" Roy_s  Shell.jpg", "Other.jpg"])
    assert proposal == Proposal(filename=" Roy_s  Shell.jpg", reason="punctuation and spacing")


def test_propose_match_pass_b_matches_a_one_character_typo():
    proposal = propose_match(
        "CD 1 01 53 34 2 Finnis Meat Market.jpg",
        ["CD 1 01 53 34 2 Finnish Meat Market.jpg"],
    )
    assert proposal is not None
    assert proposal.filename == "CD 1 01 53 34 2 Finnish Meat Market.jpg"
    assert proposal.reason == "edit distance 1"


def test_propose_match_refuses_when_only_the_numbers_differ():
    """SOP CD 2 COE is named 001_, 002_, ... Two different photographs are one
    edit apart, so edit distance alone would propose the wrong one."""
    assert propose_match("001_seaside_beach.JPG", ["002_seaside_beach.JPG"]) is None


def test_propose_match_refuses_beyond_the_distance_threshold():
    assert propose_match("Liberty.jpg", ["Completely Different.jpg"]) is None


def test_propose_match_returns_none_when_there_are_no_candidates():
    assert propose_match("Liberty.jpg", []) is None


def test_propose_match_raises_when_two_candidates_match():
    """Same rule resolve_file() follows: an identifier is permanent, so
    choosing between near-misses on the operator's behalf is refused."""
    with pytest.raises(AmbiguousMatch) as exc:
        propose_match("Liberty.jpg", ["Liberty.JPG", "liberty.jpeg"])
    assert sorted(exc.value.matches) == ["Liberty.JPG", "liberty.jpeg"]


def test_propose_match_prefers_pass_a_over_pass_b():
    """An exact normalized match is certain; an edit-distance match is a
    guess. If one candidate is certain it wins outright rather than competing
    with the guess for ambiguity."""
    proposal = propose_match("Roy_s Shell.jpg", ["Roy's Shell.jpg", "Roy's Shellx.jpg"])
    assert proposal == Proposal(filename="Roy's Shell.jpg", reason="punctuation and spacing")


def test_propose_match_ignores_extension_differences():
    proposal = propose_match("Liberty.jpg", ["Liberty.JPEG"])
    assert proposal is not None and proposal.filename == "Liberty.JPEG"
