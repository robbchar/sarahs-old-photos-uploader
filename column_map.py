"""Turns a Google Sheet grid into the same row shape read_csv() produces, so
everything downstream cannot tell where its rows came from. See
docs/DECISIONS.md, "The Sheet is read live"."""
from __future__ import annotations

import re

# Hyphens and underscores are preserved because Internet Archive field names use them
# (e.g., "identifier-bib"). Other punctuation is removed.
_PUNCTUATION = re.compile(r"[^a-z0-9\s_-]")
_WHITESPACE = re.compile(r"\s+")
_REPEATED_UNDERSCORE = re.compile(r"_+")


def normalize_header(header: str) -> str:
    """A Sheet header becomes an IA metadata field name by this rule and no
    other. Typos survive on purpose: the Sheet is authoritative, and silently
    correcting a header would decide which field a value lands in."""
    text = _PUNCTUATION.sub("", header.strip().lower())
    text = _WHITESPACE.sub("_", text)
    text = _REPEATED_UNDERSCORE.sub("_", text)
    return text.strip("_")
