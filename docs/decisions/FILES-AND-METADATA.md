# Files and metadata

How a row's file is found on disk, and which of a row's cells become Internet
Archive metadata.

One of the decision records indexed by
[`../DECISIONS.md`](../DECISIONS.md). Section titles here are cited verbatim
from code comments, so they are stable — if you rename one, grep for it
first.

## A file is found by resolution, not by constructing a path

*Decided 2026-08-16, after seeing that 225 of 234 filenames in the real Sheet
carry no extension while 9 do.*

The obvious design — join the folder column, the filename column and a
configured extension — fails on this data twice over. Nine rows already carry
`.jpg`, so appending one would produce `Liberty.jpg.jpg`. And nobody can promise
the remaining files are all `.jpg` rather than `.jpeg` or a TIFF master.

So the tool resolves rather than constructs. Within the directory named by the
folder column it makes **three** passes, each tried only if the previous found
nothing. What it finds is what gets used.

1. **Exact, case-sensitive filename match.** Handles the nine real rows that
   already carry an extension.
2. **A file whose extension-stripped stem equals the FULL name part**,
   case-insensitively, without touching the name part itself.
3. **The same comparison with the name part's own trailing segment also
   chopped** as if it were an extension. This is what handles a cell carrying
   a real extension in a different case than disk — `.jpg` in the Sheet,
   `.JPEG` on the drive.

**Pass 2 is not a refinement of pass 3; it exists to prevent a silent wrong
match.** An archival reference like `Report.1958` has a dot that is not an
extension. Chopping it first — which is all pass 3 does — reduces it to
`Report` and would happily match an unrelated `Report.jpg`. Comparing against
the full name part first means `Report.1958` finds `Report.1958.tif` and
nothing else.

Two rules keep this from guessing:

**Two files sharing a stem is an error naming both, never a pick.** An item's
identifier is permanent; choosing between `Liberty.jpg` and `Liberty.tif` on
the
operator's behalf is the kind of silent decision this project rejects
everywhere else.

**Stem matching is case-insensitive.** The archive lives on a Windows-attached
drive whose filesystem is case-insensitive already, so matching case-
sensitively
would reject files that do exist. A case-only collision surfaces as the
ambiguity error above rather than a silent wrong pick.

Directory listings are cached per folder. The real Sheet has 234 rows across 5
folders, and the full collection is ~10,000 rows across a similar handful —
one
scan per folder rather than one per row.

**`files_dir` is a boundary, not a starting point.** The folder part is
resolved and checked to still be underneath it, so a candidate that *resolves
outside* it — an absolute path, or a `..` climbing past it — is refused rather
than followed. The test is on the resolved directory, not on the text: a `..`
that lands back inside `files_dir` never left, and is allowed. A candidate
whose folder
segment is empty is refused too — searching `files_dir`'s own root would turn
a blank required cell into a search of the wrong place instead of a failure.
`KNOWN-ISSUES.md` §5 describes the `--csv` path, which has neither guard.

**Cell values are stripped before templating.** A folder cell with a stray
trailing space is otherwise a landmine on Windows: `Path.is_dir()` normalizes
the space away when it stats the path, while `Path.iterdir()` on the identical
path can still raise `FileNotFoundError` — two checks disagreeing about
whether the same folder exists. Stripping removes the space before either
check sees it, rather than working around the disagreement afterwards.

Because the resolved name is the real one, `ia_identifier_bib` records
`folder/resolved-filename`, which can differ from what the Sheet says. That is
the point: it records what was uploaded, not what someone typed.

## Sheet metadata is filtered at the upload boundary, not in `upload_row`

*Decided 2026-08-16.*

`upload_row` turns every key it is handed (bar `identifier` and `file`) into an
Internet Archive metadata field. That is correct for the CSV path, where the
CSV's columns are exactly the fields the operator wants uploaded. It is wrong
for the Sheet, which also carries the tool's own `ia_` bookkeeping columns and
whatever a Sheet author marked `(LCPS Internal)` — none of which belong on a
public item, and all of which would be permanent once there.

`sheet_upload_metadata()` therefore builds the dict `upload_row` receives,
keeping only `ColumnMap.uploadable_fields()` (already the single definition of
what may be uploaded, and already excluding both categories) and adding the
generated `identifier-bib` and `mediatype`. Filtering here rather than inside
`upload_row` leaves the CSV path's behavior untouched and keeps the rule next
to the ColumnMap that defines it.

`identifier` and `file` are subtracted too, via `DROPPED_BY_UPLOAD_ROW`. Those
are the two keys `upload_row` strips on its own — `file` is a local path and
`identifier` is Internet Archive's own item identifier, so a Sheet column of
that name cannot ship under it. Naming them in one place lets the field
receipt
say so out loud. It previously listed `identifier` among the fields that would
upload, which was simply untrue, and a test asserted that wording and so
pinned
the lie in place. **The consequence is real and still open**: on a Sheet with
a
donor `Identifier` column, that archival reference now reaches Internet
Archive
in no form at all — the registry's `file_template` names different columns, so
`identifier-bib` does not carry it either. Repairable after the fact with
`sync-metadata`, but it needs a deliberate answer (a non-colliding field name)
rather than a silent drop.

## `identifier-bib` and `mediatype` are generated, not columns

*Decided 2026-08-08.*

`identifier-bib` records the source path of the uploaded file, which is exactly
what `file_template` computes — so the tool generates it rather than reading a
column that would restate it. `mediatype` is a per-project constant in the
registry.

This is not only tidiness. The surviving test item
`zztest-lcps-sarahsoldphotos-00005` carries the field **`indentifier-bib`**,
misspelled, permanently. A header typo ships once per batch; a generated field
name cannot.

## Blank cell means "leave alone", not "clear"

A `sync-metadata` CSV lists only the columns that changed, so a blank cell has
to mean "don't touch this field". `update_metadata_row` drops blank cells from
the request entirely rather than sending `""`.

Deleting a field therefore needs an explicit sentinel: the literal string
`REMOVE_TAG`, which is what the official `ia` CLI's `--modify field:REMOVE_TAG`
uses. Not invented here — matching IA's own convention.

## Blank `date` becomes `[n.d.]` rather than being omitted

*Changed during the build (`bfd6c34`).* `date` started as a required column,
which blocked rows for photos with genuinely unknown dates. It is now
optional,
and `upload_row` fills a blank with `[n.d.]`, the standard archival "no date"
abbreviation — so every item carries a date field and an unknown date is
recorded as a deliberate statement rather than a gap.

## `checksum=True` and `verbose=True` on upload

`checksum=True` makes a re-run skip files already present with a matching MD5,
which avoids re-triggering IA's `derive` task — the expensive part of an
upload. `verbose=True` surfaces the library's own per-file byte-progress bar,
so a long run is never silently quiet.
