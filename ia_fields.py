"""Internet Archive's recognized metadata vocabulary, shipped as reference data.

This is knowledge about IA that applies to any project - deliberately NOT a
per-project column mapping, which was rejected during design as a second source
of truth only the maintainer could see. Nothing here ever rewrites a field:
it produces advice a human acts on by renaming a column in the Sheet."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

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
    for field_name in field_names:
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

        suggestions.append(
            Suggestion(
                field_name=field_name,
                standard=standard,
                reason=(
                    f"standard field; renaming the column to `{standard.title()}` "
                    "makes it searchable on archive.org"
                ),
            )
        )
    return suggestions
