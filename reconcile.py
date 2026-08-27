"""Matching a Sheet's filename cell against what is actually on the drive.

Pure: no filesystem, no Sheets, no I/O. ia_bulk.py's reconcile-files command
supplies the candidates and acts on the result."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_DIGITS = re.compile(r"\d+")


def normalize_name(name: str) -> str:
    """Fold accents to their base letters, casefold, replace every run of
    non-alphanumeric characters with a single space, and strip.

    This is what makes the copy artifacts compare equal without any fuzzy
    matching at all: apostrophes become underscores and single spaces become
    double when files are copied off source media, so `Roy's Shell` on the
    drive is ` Roy_s  Shell`. Both normalize to `roy s shell`.

    The accent fold is not decoration in an Astoria Finnish/Scandinavian
    collection. Without it an accented character is simply deleted along with
    the punctuation, so two distinct names differing only in which vowels
    carry diaereses collapse to the same skeleton (`v in`) and are reported
    as an exact match - the tool's MOST certain register - for two different
    photographs, while a stem written entirely in accented characters
    collapses to nothing at all and lands within edit distance of any short
    name on the drive. Decomposing (NFKD) and dropping the
    combining marks instead turns those into ordinary ASCII names, which
    also promotes several real matches from a distance-2 guess to a Pass A
    certainty."""
    decomposed = unicodedata.normalize("NFKD", name)
    unaccented = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_ALNUM.sub(" ", unaccented.casefold()).strip()


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


MAX_EDIT_DISTANCE = 2


class AmbiguousMatch(Exception):
    """More than one candidate matched. Never resolved automatically - see
    resolve_file()'s identical rule and docs/decisions/FILES-AND-METADATA.md,
    "A file is found by resolution, not by constructing a path"."""

    def __init__(self, matches: list[str]) -> None:
        super().__init__(f"{len(matches)} candidates matched: {', '.join(sorted(matches))}")
        self.matches = matches


@dataclass(frozen=True)
class Proposal:
    filename: str
    reason: str


def _stem(name: str) -> str:
    return normalize_name(Path(name).stem)


def propose_match(wanted: str, candidates: list[str]) -> Proposal | None:
    """The single candidate that should be proposed for `wanted`, or None.

    Two passes, most certain first. Pass A is an exact match after
    normalization - the same photograph, written differently. Pass B is a
    guess bounded by edit distance, and only runs when Pass A found nothing,
    so a certainty is never made to compete with a guess for ambiguity.

    Raises AmbiguousMatch if a pass produces more than one candidate."""
    target = _stem(wanted)
    if not target:
        # Nothing survived normalization - a name written entirely in
        # characters this comparison cannot see. An empty target is inside
        # the edit-distance budget of every candidate with a stem of two
        # characters or fewer, so proceeding would build a confident
        # proposal out of no evidence at all. Same reason a candidate whose
        # own stem normalizes to nothing is dropped below.
        return None

    exact = sorted({c for c in candidates if _stem(c) == target})
    if len(exact) == 1:
        return Proposal(filename=exact[0], reason="punctuation and spacing")
    if exact:
        raise AmbiguousMatch(exact)

    # Numbers must match exactly - see digit_runs(). Letters may differ.
    target_digits = digit_runs(target)
    near = sorted(
        {
            c
            for c in candidates
            if _stem(c)
            and digit_runs(_stem(c)) == target_digits
            and levenshtein(_stem(c), target) <= MAX_EDIT_DISTANCE
        }
    )
    if len(near) == 1:
        distance = levenshtein(_stem(near[0]), target)
        return Proposal(filename=near[0], reason=f"edit distance {distance}")
    if near:
        raise AmbiguousMatch(near)
    return None
