"""Internet Archive's recognized metadata vocabulary, shipped as reference data.

This is knowledge about IA that applies to any project - deliberately NOT a
per-project column mapping, which was rejected during design as a second source
of truth only the maintainer could see. Nothing here ever rewrites a field:
it produces advice a human acts on by renaming a column in the Sheet."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# From https://archive.org/developers/metadata-schema/ - the subset a photo
# archive realistically uses. Extend as needed; a missing entry only costs a
# suggestion that is not made.
IA_STANDARD_FIELDS = frozenset(
    {
        "collection", "contributor", "coverage", "creator", "credits", "date",
        "description", "identifier", "language", "licenseurl", "mediatype",
        "notes", "publisher", "rights", "source", "subject", "title", "volume",
    }
)

# Fields that the upload pipeline generates itself: never suggest renaming a
# column to these names, since a column bearing that name has its values silently
# discarded (or overwritten) on upload. ia_bulk.py sets collection unconditionally;
# identifier and mediatype are derived, not sourced from the Sheet.
PIPELINE_OWNED_FIELDS = frozenset({"identifier", "mediatype", "collection"})

# Curated equivalences for cases substring matching cannot see.
SYNONYMS = {
    "photographer": "creator",
    "photographer_studio": "creator",
    "artist": "creator",
    "author": "creator",
    "place": "coverage",
    "location": "coverage",
    "keywords": "subject",
    "topics": "subject",
}


@dataclass(frozen=True)
class Suggestion:
    field_name: str
    standard: str
    reason: str


def suggest_standard_fields(field_names: Iterable[str]) -> list[Suggestion]:
    suggestions: list[Suggestion] = []
    field_names_dict = dict.fromkeys(field_names)  # Preserves caller order, deduplicates
    field_names_set = set(field_names)  # For O(1) membership checks
    suggested_standards: set[str] = set()

    for field_name in field_names_dict:
        if field_name in IA_STANDARD_FIELDS:
            continue

        standard = SYNONYMS.get(field_name)
        if standard is None:
            standard = next(
                (
                    candidate
                    for candidate in sorted(IA_STANDARD_FIELDS)
                    if candidate in field_name.split("_")
                ),
                None,
            )
        if standard is None:
            continue

        # Skip if this is a pipeline-owned field (values would be silently discarded).
        if standard in PIPELINE_OWNED_FIELDS:
            continue

        # Skip if this target already appears in the input.
        if standard in field_names_set:
            continue

        # Skip if we've already suggested this standard from another field.
        if standard in suggested_standards:
            continue

        suggested_standards.add(standard)
        suggestions.append(
            Suggestion(
                field_name=field_name,
                standard=standard,
                reason=(
                    f"`{standard}` is a standard IA field — rename only if this "
                    "column means the same thing"
                ),
            )
        )
    return suggestions
