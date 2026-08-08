from ia_fields import IA_STANDARD_FIELDS, suggest_standard_fields


def test_standard_fields_include_the_common_archival_ones():
    for field in ("creator", "subject", "description", "rights", "publisher", "coverage"):
        assert field in IA_STANDARD_FIELDS


def test_no_suggestion_for_a_field_already_standard():
    assert suggest_standard_fields(["title", "description"]) == []


def test_suggests_standard_field_by_substring_overlap():
    suggestions = suggest_standard_fields(["subject_terms"])

    assert len(suggestions) == 1
    assert suggestions[0].field_name == "subject_terms"
    assert suggestions[0].standard == "subject"


def test_suggests_standard_field_by_curated_synonym():
    suggestions = suggest_standard_fields(["photographer_studio"])

    assert [s.standard for s in suggestions] == ["creator"]


def test_no_suggestion_when_nothing_resembles_a_standard_field():
    assert suggest_standard_fields(["architectura_style", "theme"]) == []


# Regression tests for safety properties


def test_no_suggestion_targets_pipeline_owned_fields():
    """Fields the pipeline generates itself should never be suggested as rename targets."""
    # collection_notes could suggest "collection", but collection is pipeline-owned.
    suggestions = suggest_standard_fields(["collection_notes"])
    assert len(suggestions) == 0

    # identifier_bib could suggest "identifier", but identifier is pipeline-owned.
    suggestions = suggest_standard_fields(["identifier_bib"])
    assert len(suggestions) == 0

    # mediatype_note could suggest "mediatype", but mediatype is pipeline-owned.
    suggestions = suggest_standard_fields(["mediatype_note"])
    assert len(suggestions) == 0


def test_no_suggestion_targets_field_already_in_input():
    """Never suggest renaming to a field that already exists in the input."""
    # title_alternate could suggest "title", but title is already in the input.
    suggestions = suggest_standard_fields(["title", "title_alternate"])
    assert len(suggestions) == 0

    # coverage_detail could suggest "coverage", but coverage is already in the input.
    suggestions = suggest_standard_fields(["coverage", "coverage_detail"])
    assert len(suggestions) == 0


def test_multiple_inputs_to_one_standard_yield_one_suggestion():
    """When two inputs map to the same standard, emit at most one suggestion."""
    # Both photographer and photographer_studio are synonyms for creator.
    suggestions = suggest_standard_fields(["photographer", "photographer_studio"])
    assert len(suggestions) == 1
    assert suggestions[0].standard == "creator"

    # Both artist and author are synonyms for creator.
    suggestions = suggest_standard_fields(["artist", "author"])
    assert len(suggestions) == 1
    assert suggestions[0].standard == "creator"


def test_whole_word_substring_matching_prevents_false_positives():
    """Substring matching uses word boundaries (split on _) to avoid false positives."""
    # "discover" contains "cover" but "coverage" is split on _, so no match.
    suggestions = suggest_standard_fields(["discover"])
    assert len(suggestions) == 0

    # But "coverage_info" does contain "coverage" as a word.
    suggestions = suggest_standard_fields(["coverage_info"])
    assert len(suggestions) == 1
    assert suggestions[0].standard == "coverage"


def test_deterministic_ordering_names_first_field_in_caller_order():
    """When multiple fields map to one standard, suggestion names the first in caller order."""
    # artist (1st) and author (2nd) both suggest creator; we should cite artist.
    suggestions = suggest_standard_fields(["artist", "author"])
    assert len(suggestions) == 1
    assert suggestions[0].field_name == "artist"
    assert suggestions[0].standard == "creator"

    # Reverse order: author (1st) and artist (2nd); we should cite author.
    suggestions = suggest_standard_fields(["author", "artist"])
    assert len(suggestions) == 1
    assert suggestions[0].field_name == "author"
    assert suggestions[0].standard == "creator"
