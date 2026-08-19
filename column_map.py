"""Turns a Google Sheet grid into the same row shape read_csv() produces, so
everything downstream cannot tell where its rows came from. See
docs/DECISIONS.md, "The Sheet is read live"."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Hyphens and underscores are preserved because Internet Archive field names use them
# (e.g., "identifier-bib"). Other punctuation is removed.
_PUNCTUATION = re.compile(r"[^a-z0-9\s_-]")
_WHITESPACE = re.compile(r"\s+")
_REPEATED_UNDERSCORE = re.compile(r"_+")


def normalize_header(header: str) -> str:
    """A Sheet header becomes an IA metadata field name by this rule and no
    other. Typos survive on purpose: the Sheet is authoritative, and silently
    correcting a header would decide which field a value lands in. This rule is
    applied consistently so that column collisions (two headers normalizing to
    the same field name) can be detected before they silently destroy data."""
    text = _PUNCTUATION.sub("", header.strip().lower())
    text = _WHITESPACE.sub("_", text)
    text = _REPEATED_UNDERSCORE.sub("_", text)
    return text.strip("_")


HELD_BACK_MARKER = "(lcps internal)"
# `identifier` is deliberately NOT here: the real Sheet's own `Identifier`
# column holds the donor's original archival reference (e.g.
# "CD 1 01 53 58 1 Central SS"), not a minted IA identifier - that lives in
# `ia_identifier` instead. Every column the tool itself writes carries the
# `ia_` prefix so it cannot collide with whatever a Sheet author already
# named a column. See docs/DECISIONS.md, "Tool-owned Sheet columns are all
# `ia_`-prefixed".
RESERVED_FIELDS = frozenset({"file", "ia_identifier", "ia_identifier_bib", "ia_uploaded", "ia_url"})


def is_held_back(header: str) -> bool:
    """A header marked (LCPS Internal) is never uploaded. The rule lives in the
    header text so the people writing the data can see it, rather than in a
    config file only the maintainer can read."""
    return HELD_BACK_MARKER in header.lower()


@dataclass(frozen=True)
class ColumnMap:
    headers: list[str]
    field_names: dict[str, str]
    held_back: list[str]

    def uploadable_fields(self) -> list[str]:
        return [
            self.field_names[header]
            for header in self.headers
            if header not in self.held_back
            and self.field_names[header] not in RESERVED_FIELDS
        ]


def build_column_map(headers: list[str]) -> ColumnMap:
    """Construct a ColumnMap from a header row. The map records which columns
    are held back from upload and which normalized field name each header maps
    to. This is called before validation, so colliding headers or empty field
    names will not raise here; use check_column_map() to detect and report
    those defects."""
    return ColumnMap(
        headers=list(headers),
        field_names={header: normalize_header(header) for header in headers},
        held_back=[header for header in headers if is_held_back(header)],
    )


def grid_to_rows(grid: list[list[str]]) -> tuple[ColumnMap, list[dict[str, str]]]:
    """Convert a Sheet grid (headers + data rows) into a ColumnMap and row
    dictionaries. Short rows are padded with empty strings because the Sheets
    API omits trailing empty cells - a row genuinely can be shorter than the
    header without being corrupt data. Long rows silently drop excess cells
    without raising; use check_grid_shape() to detect those defects before
    upload. This mirrors the approach check_row_shape() takes for CSV rows: no
    validation in the converter, errors reported separately so users see every
    problem at once instead of one per run."""
    if not grid:
        return build_column_map([]), []

    headers, *data_rows = grid
    column_map = build_column_map(headers)
    rows = [
        {
            column_map.field_names[header]: (row[index] if index < len(row) else "")
            for index, header in enumerate(headers)
        }
        for row in data_rows
    ]
    return column_map, rows


def check_column_map(column_map: ColumnMap) -> list[str]:
    """Detect defects in a ColumnMap that would silently destroy data. Two
    headers normalizing to the same field name collide, silently overwriting
    one value with the other in the output dict. A header normalizing to the
    empty string is a similar defect: multiple decorative/divider columns would
    all collide on an empty key. This function reports each collision and the
    empty-string field name defect so all defects are visible to the user at
    once.

    Headers that normalize to "" are reported as a single combined message
    naming all of them, not one message per header plus a pairwise collision
    message for every pair - three blank/decorative columns used to produce
    five overlapping messages for what is really one root cause, which reads
    as noise rather than a clear signal."""
    errors: list[str] = []

    blank_headers = [header for header in column_map.headers if column_map.field_names[header] == ""]
    if blank_headers:
        header_list = ", ".join(f"'{header}'" for header in blank_headers)
        errors.append(
            f"column(s) {header_list} normalize to an empty field name (no alphanumeric "
            "characters remain after removing punctuation) - any columns that do this "
            "collide with each other and silently overwrite one another's value"
        )

    # Check for collisions among the remaining (non-blank) headers: two
    # different headers mapping to the same non-empty field name. Blank
    # headers are excluded here since they're already covered, together,
    # above - without this exclusion N blank headers would also produce
    # N-1 pairwise "both normalize to ''" collision messages against each
    # other.
    seen: dict[str, str] = {}
    for header in column_map.headers:
        field_name = column_map.field_names[header]
        if field_name == "":
            continue
        if field_name in seen:
            errors.append(
                f"columns '{seen[field_name]}' and '{header}' both normalize to field name "
                f"'{field_name}' - one value will silently overwrite the other"
            )
        else:
            seen[field_name] = header

    return errors


def check_grid_shape(grid: list[list[str]]) -> list[str]:
    """Detect data rows longer than the header row. When a data row has more
    cells than the header has columns, the excess cells silently vanish from
    the row dict output without error, making it impossible to tell which values
    are missing data and which are present but attributed to the wrong field.
    This mirrors check_row_shape() for CSV rows: short rows (handled by the
    Sheets API omitting trailing empty cells) are not an error and produce no
    message, but long rows are flagged here with row number and count of excess
    cells."""
    if not grid:
        return []

    errors: list[str] = []
    headers = grid[0]
    header_count = len(headers)

    for offset, row in enumerate(grid[1:]):
        row_number = offset + 2  # header is row 1, first data row is row 2
        if len(row) > header_count:
            extra_count = len(row) - header_count
            errors.append(
                f"row {row_number} has {extra_count} more field(s) than the header "
                f"({len(row)} vs {header_count}) - every value after column {header_count} "
                "will be silently dropped"
            )

    return errors


class TemplateError(Exception):
    """A `file_template` names a column the Sheet's header row does not
    have. Checked once, at startup, via check_file_template() - the
    alternative is every single row failing file resolution with the same
    root cause, which buries a one-line registry typo under (at real scale)
    thousands of identical-looking errors."""


class FileResolutionError(Exception):
    """A row's candidate file could not be resolved to exactly one file on
    disk: either nothing matched, or more than one file did. resolve_file()
    never picks on the operator's behalf - see docs/DECISIONS.md, "A file
    is found by resolution, not by constructing a path"."""


_TEMPLATE_FIELD_RE = re.compile(r"\{([^{}]*)\}")


def check_file_template(template: str, column_map: ColumnMap) -> None:
    """Fails fast if `file_template` (from the project registry) references
    a column the Sheet's actual header row doesn't have - a mistyped
    placeholder or a Sheet whose columns changed since the registry was
    written. Deliberately checked against every normalized field name the
    Sheet has, not just uploadable ones: a template is allowed to reference
    a reserved or held-back column (the folder/filename columns commonly
    are)."""
    known_fields = set(column_map.field_names.values())
    referenced = _TEMPLATE_FIELD_RE.findall(template)
    missing = [name for name in referenced if name not in known_fields]
    if missing:
        column_list = ", ".join(f"'{name}'" for name in missing)
        available = ", ".join(sorted(known_fields))
        # The overwhelmingly likely cause is raw header text in the template
        # ("{File Name}") where a normalized field name is required
        # ("{file_name}"), so spell that out and list what is actually
        # available rather than leaving the operator to guess the form.
        raise TemplateError(
            f"file_template references column(s) {column_list}, which the Sheet's header "
            "row does not have. Note that file_template uses NORMALIZED column names, not "
            "the header text as typed: a header 'File Name' becomes 'file_name'. Available "
            f"columns are: {available}"
        )


def candidate_path(template: str, row: dict[str, str]) -> str:
    """Builds a CANDIDATE relative path by substituting the row's field
    values into `file_template` - a plain string join, nothing more. This is
    not a path guaranteed to exist: see resolve_file(), which is what turns
    a candidate into a real, disk-verified path. Kept separate from
    resolve_file() so the two failure modes (a malformed template vs. a
    file that isn't actually there) stay distinguishable.

    Each field value is stripped before substitution. A folder or filename
    cell with a stray trailing space is otherwise a landmine on Windows:
    `Path.is_dir()` normalizes a trailing space away when it stats the
    path, but `Path.iterdir()` on the very same path can still raise
    `FileNotFoundError` - two checks in resolve_file() disagreeing about
    whether the same folder exists. Stripping here removes the space
    before either check ever sees it, rather than working around the
    disagreement after the fact."""
    stripped_row = {key: value.strip() for key, value in row.items()}
    return template.format(**stripped_row)


def _unique_match_or_none(matches: list[str], directory: Path, name_part: str) -> str | None:
    """Zero matches means "try the next, looser rule" to the caller (a
    `None` return); more than one is never ambiguous-and-ignorable, so it
    raises immediately regardless of which rule produced it."""
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    raise FileResolutionError(
        f"'{name_part}' in '{directory}' matches more than one file, refusing to pick "
        f"between them: {', '.join(matches)}"
    )


def resolve_file(
    files_dir: str | Path, candidate: str, listing_cache: dict[Path, list[str]]
) -> str:
    """Resolves a candidate relative path against the filesystem rather than
    trusting it as a literal path. In the real Sheet, 225 of 234 filenames
    carry no extension and 9 already carry `.jpg` - constructing
    `name + ".jpg"` would produce `Liberty.jpg.jpg` for the 9 and can't be
    trusted to be right for the other 225 (some are TIFF masters). See
    docs/DECISIONS.md, "A file is found by resolution, not by constructing
    a path".

    `candidate` is split at its last "/" into a directory part and a name
    part. A "/" present with nothing before it (an empty folder segment -
    e.g. a blank cell in file_template's folder column) is refused outright
    rather than treated as "search files_dir's own root": that silently
    turns a missing required cell into a search of the wrong place instead
    of a failure. `files_dir` is a hard boundary, not just a starting
    point - the folder part is resolved and checked to still be under it,
    so an absolute path or a `..` pasted into a Sheet cell cannot escape it
    (`Path(files_dir) / part` would otherwise silently discard `files_dir`
    entirely when `part` is itself absolute).

    Within the resolved directory, matching happens in three passes, each
    one only tried if the previous found nothing:
    1. An exact, case-sensitive filename match (handles the 9 real rows
       that already carry an extension).
    2. A file whose own extension-stripped stem equals the FULL name part,
       case-insensitively, without touching the name part itself - this is
       what lets a name that legitimately contains a dot which isn't an
       extension (an archival reference like "Report.1958") still match
       "Report.1958.tif" instead of being chopped down to "Report" and
       silently matching an unrelated "Report.jpg".
    3. The same comparison but with the name part's OWN trailing segment
       also chopped as if it were an extension - this is the fallback that
       handles the Sheet cell already carrying a real extension in a
       different case than disk (e.g. ".jpg" in the cell, ".JPEG" on disk).
    Zero matches (after all three passes) or more than one at any pass both
    raise FileResolutionError rather than guess - two files sharing a stem
    (a JPEG and a TIFF master, say) is exactly the case an operator must
    resolve by hand, never automatically.

    `listing_cache` maps a resolved directory to its file listing, built at
    most once per directory - the real Sheet is 234 rows across 5 folders,
    and the full collection is ~10,000 rows across a similar handful, so
    this keeps the cost at one scan per folder rather than one per row."""
    directory_part, separator, name_part = candidate.rpartition("/")

    if separator and not directory_part.strip():
        raise FileResolutionError(
            f"'{candidate}' has an empty folder segment before the last '/' - refusing to "
            "search files_dir's own root on behalf of what looks like a blank folder cell"
        )

    files_root = Path(files_dir).resolve()
    directory = (files_root / directory_part).resolve()

    if directory != files_root and files_root not in directory.parents:
        raise FileResolutionError(
            f"'{candidate}' resolves to '{directory}', which is outside files_dir "
            f"('{files_root}') - refusing to search there"
        )

    if directory not in listing_cache:
        try:
            if not directory.is_dir():
                raise FileResolutionError(f"no such folder: {directory} (does not exist)")
            listing_cache[directory] = [
                entry.name for entry in directory.iterdir() if entry.is_file()
            ]
        except OSError as exc:
            # Covers real, seen-in-the-field disagreements: a folder cell
            # with a trailing space can leave is_dir() reporting True (the
            # OS normalizes it for stat) while iterdir() on the identical
            # path still raises FileNotFoundError, and a PermissionError or
            # a disconnected external drive mid-scan both land here too.
            # Any of these must become a per-row FileResolutionError, never
            # an unhandled traceback that kills the whole validate run.
            raise FileResolutionError(f"could not read folder '{directory}': {exc}") from exc

    listing = listing_cache[directory]

    if name_part in listing:
        resolved_name = name_part
    else:
        target_lower = name_part.lower()
        full_matches = sorted(
            entry for entry in listing if Path(entry).stem.lower() == target_lower
        )
        resolved_name = _unique_match_or_none(full_matches, directory, name_part)

        if resolved_name is None:
            target_stem = Path(name_part).stem.lower()
            stem_matches = sorted(
                entry for entry in listing if Path(entry).stem.lower() == target_stem
            )
            resolved_name = _unique_match_or_none(stem_matches, directory, name_part)

            if resolved_name is None:
                raise FileResolutionError(
                    f"no file found in '{directory}' matching '{name_part}' (looked for an "
                    "exact filename match, then a case-insensitive match ignoring extension)"
                )

    return f"{directory_part}/{resolved_name}" if directory_part else resolved_name
