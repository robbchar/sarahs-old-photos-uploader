"""Matching a Sheet's filename cell against what is actually on the drive.

Pure: no filesystem, no Sheets, no I/O. ia_bulk.py's reconcile-files command
supplies the candidates and acts on the result."""
from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_DIGITS = re.compile(r"\d+")


def normalize_name(name: str) -> str:
    """Casefold, replace every run of non-alphanumeric characters with a
    single space, and strip.

    This is what makes the copy artifacts compare equal without any fuzzy
    matching at all: apostrophes become underscores and single spaces become
    double when files are copied off source media, so `Roy's Shell` on the
    drive is ` Roy_s  Shell`. Both normalize to `roy s shell`."""
    return _NON_ALNUM.sub(" ", name.casefold()).strip()


def digit_runs(name: str) -> tuple[str, ...]:
    """Every run of digits, in order.

    Pass B's guard. In `SOP CD 2 COE` the filenames are `001_seaside_beach`,
    `002_westport_tunnel_by_Ford`, ... - two DIFFERENT photographs one edit
    apart. Without this, edit distance would confidently propose one for the
    other. Requiring identical digit runs keeps `finnis`/`finnish` (same
    numbers, different letters) while refusing `001`/`002`."""
    return tuple(_DIGITS.findall(name))


def levenshtein(a: str, b: str) -> int:
    """Edit distance. Implemented rather than depended upon: fifteen lines
    against a package this project would otherwise not need."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,          # deletion
                    current[j - 1] + 1,       # insertion
                    previous[j - 1] + (ca != cb),  # substitution
                )
            )
        previous = current
    return previous[-1]
