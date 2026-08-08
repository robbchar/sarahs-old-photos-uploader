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
