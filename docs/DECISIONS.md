# Decisions

Why the tool is shaped the way it is. Reconstructed from the build history
(`573cfc6`..`460d1b6`), the review notes, and the behavior of the code itself.
Read this before "simplifying" something here — several of these look like
accidents and are not.

## Use the `internetarchive` Python library, not `ia upload --spreadsheet`

The `ia` CLI can take a spreadsheet directly, which was the original plan. It
was dropped because a CLI invocation gives you one exit code for the whole
batch — you cannot tell which of 500 rows failed, or resume from the failure.
Driving the library row by row means every row produces its own logged outcome,
which is what makes `--resume-from` possible.

Cost: the tool re-implements chunking and progress reporting that the CLI would
have handled.

## Safety rail: prefix in code, never in the CSV

*Changed during the build (`bfd6c34`) after real usage — the earlier design was
the opposite.*

Originally, test runs required the CSV itself to contain `zztest-` identifiers,
and a `check_live_safety()` function refused to run without them. That meant
keeping two CSVs, or editing identifiers back and forth between test and live
runs — exactly the kind of manual step that eventually ships a `zztest-`
identifier to production, permanently.

Now the CSV always holds the real, permanent identifier and
`effective_identifier()` prepends `zztest-` at call time unless `--live` is
passed. **The same CSV file is used unchanged for both test and live runs.**
`check_identifier` actively rejects a hand-written `zztest-` identifier, and
there is a test asserting exactly that.

## Test identifiers carry a per-run stamp

*Added 2026-08-19, after a rehearsal failed against the tool's own earlier test
items. Amends "Safety rail: prefix in code, never in the CSV" above.*

`effective_identifier()` prepended a bare `zztest-` to the real identifier. That
made a test run's identifiers a pure function of the real ones — and since a
fresh Sheet always starts minting at `00001`, every test run from a fresh Sheet
produced exactly the same test identifiers as the last one.

Internet Archive never releases an identifier, and `test_collection` darkens its
items after about thirty days. A darkened item cannot be uploaded to: the
attempt fails with `Access Denied - This item has been taken offline`. So the
first rehearsal permanently burned `zztest-lcps-sarahsoldphotos-00001`, and
every later rehearsal of row 1 collided with it. A July run burned `00001`
through `00009`; the next rehearsal, in August, could not upload a single row.

A rehearsal mode you can only use once is not a rehearsal mode. Test identifiers
therefore carry a stamp unique to the invocation:

```
zztest-20260819t144907-lcps-sarahsoldphotos-00001
```

One stamp per run, so a run's items group together in a listing. `zztest-` stays
the leading marker so a test item is still recognisable at a glance. Live
identifiers are untouched — they are the permanent ones and must remain a pure
function of the Sheet.

`--resume-from` is unaffected: `load_prior_successes` matches on the real
`identifier`, not on `uploaded_as`, so resuming still works across runs whose
stamps differ. The log's `uploaded_as` now records which stamped item a row
actually landed on, which is the question you ask when checking a rehearsal.

## `--resume-from` filters on run mode

A test-mode success and a live-mode success are indistinguishable by
identifier — only `uploaded_as` differs. Without a mode check,
`--live --resume-from <test-log>` would skip every row as "already done" and
report a successful live run that uploaded nothing.

So `load_prior_successes()` matches on the log's `live` field. Log lines
written before that field existed record no mode and match **neither**, so old
logs simply never skip anything rather than skipping in the wrong direction.

## Blank cell means "leave alone", not "clear"

A `sync-metadata` CSV lists only the columns that changed, so a blank cell has
to mean "don't touch this field". `update_metadata_row` drops blank cells from
the request entirely rather than sending `""`.

Deleting a field therefore needs an explicit sentinel: the literal string
`REMOVE_TAG`, which is what the official `ia` CLI's `--modify field:REMOVE_TAG`
uses. Not invented here — matching IA's own convention.

## "Unchanged" is a third outcome, not a failure

IA returns HTTP 400 with `{"error": "no changes to _meta.xml"}` when a metadata
update matches what is already on the item. Nothing is wrong; there was just
nothing to do. Treating that as a failure would inflate the error count and
flip the exit code on a re-run of a correct CSV — which matters because
`sync-metadata` is meant to be safe to run repeatedly.

`update_metadata_row` detects that exact error string and raises
`MetadataUnchanged`, which `run_rows` counts and logs in its own bucket.

## Blank `date` becomes `[n.d.]` rather than being omitted

*Changed during the build (`bfd6c34`).* `date` started as a required column,
which blocked rows for photos with genuinely unknown dates. It is now optional,
and `upload_row` fills a blank with `[n.d.]`, the standard archival "no date"
abbreviation — so every item carries a date field and an unknown date is
recorded as a deliberate statement rather than a gap.

## `checksum=True` and `verbose=True` on upload

`checksum=True` makes a re-run skip files already present with a matching MD5,
which avoids re-triggering IA's `derive` task — the expensive part of an
upload. `verbose=True` surfaces the library's own per-file byte-progress bar,
so a long run is never silently quiet.

## Identifiers are minted by `upload` and written back to the Sheet

*Reversed 2026-08-08, before implementation. This section previously recorded
the opposite — "identifiers are assigned in the Sheet, never generated here" —
which did not match how the collection is actually maintained.*

An identifier's `COLLECTIONKEY` and `PROJECTID` halves come from configuration
(`projects_registry.json`). The `NUMBER` half is assigned by `upload` as it
goes and written back to the Sheet, so the Sheet ends up holding the permanent
identifier without anyone typing one by hand.

The batch is uploaded in chunks at different times rather than in one run, so
every run has to establish where the previous one stopped before it can mint
anything. The starting point comes from the Sheet at the start of the run — the
highest existing `NUMBER` for that `PROJECTID`, plus one. Making that reliable
across interrupted and partial runs is not settled yet and is the open question
in this design.

Unchanged by the reversal: identifiers are permanent once uploaded, never
reused and never renamed, which is why minting is worth being careful about at
all. The registry still exists so the prefix half can be checked against a
known list rather than a regex alone. Original filenames and donor folder
structure are still deliberately not part of the identifier; they belong in
`identifier-bib`.

## The Sheet is read live; the CSV becomes the offline path

*Decided 2026-08-08, before implementation.*

`validate` and `upload` read rows from the Google Sheet over the API rather
than from a hand-exported CSV. Two reasons, in order of weight.

`upload` has to hold a Sheet connection anyway in order to write identifiers
back. Reading from the same place removes the "which copy is current" question
outright instead of adding a dependency to answer it.

And the export step was itself a source of defects. The traps documented in
[`CSV-PREPARATION.md`](CSV-PREPARATION.md) — above all a comma inside an
unquoted header splitting one column into two and shifting every field after it
— are artifacts of CSV *parsing*, not of the data. The Sheets API returns cells
as a grid, so a header containing a comma is just a header containing a comma.
That failure mode is structurally absent on this path.

The CSV path stays for offline and dry-run work and keeps its own header
validation, since the traps are real for anything hand-prepared.

## A malformed header is rejected, never auto-corrected

*Decided 2026-08-08, when `validate` was hardened.*

`check_header()` could strip stray whitespace and lowercase a `Date` column
instead of failing. It deliberately does not.

Auto-correcting means quietly deciding which field a value lands in — and
landing values in the wrong field is the exact failure the check exists to
catch. A tool that guesses right nine times teaches you to stop reading its
output before the tenth. Since IA metadata is permanent, the cheap outcome is a
failed `validate` and a two-minute CSV edit; the expensive one is a silent
correction that was wrong across 10,000 items.

The same reasoning is why `check_row_shape()` treats a field-count mismatch as
an error rather than padding the row: `csv.DictReader` will happily fill the gap
with `None`, but the gap means the header and the data disagree about column
positions, and nothing can tell you which one is right.

## Generic to "a project", not hardcoded to photos

A second LCPS project is expected to reuse this pipeline, which is why the
registry has a `projects` map rather than a single hardcoded code, and why the
docs say "items" more than "photos".

## Accepted, not overlooked

The final build review named these and chose to leave them. They are recorded
in [`KNOWN-ISSUES.md`](KNOWN-ISSUES.md) with reproduction details:

- Chunking has no real pacing or checkpoint behavior
- `--files-dir` does not constrain path resolution
- ~~`--collection` keeps its `"lcps"` default and stays unvalidated~~
  **Reversed 2026-08-08** — see "Technical configuration lives in the registry"
  below
- Ragged-CSV handling relies on a broad `except` in `run_rows`

## Technical configuration lives in the registry, not the command line

*Decided 2026-08-08, reversing the "accepted, not overlooked" item above.*

The target IA collection, the files directory, and the template that builds a
row's file path all move into the per-project block in
`projects_registry.json`. The command line keeps only what genuinely varies per
run: `--project`, `--live`, `--limit`.

The reversal is specifically about `--collection`. Leaving it as an unvalidated
flag defaulting to `"lcps"` was defensible when the registry was barely used;
it is not defensible once the tool already reads a per-project registry block
to find the Sheet. A wrong `--collection` on a `--live` run pushes real files
into the wrong collection and reports success — and unlike `collection_key`,
nothing catches it. As a registry value it is confirmed once, in version
control, per project, instead of retyped correctly on every run forever.

The same reasoning puts `files_dir` and `file_template` there. A row's file
path is assembled from a root plus one or more Sheet columns, which is
plumbing; the people maintaining the Sheet should never have to think about it.

## On the Sheet path, `upload` uploads the valid rows and reports the rest

*Decided 2026-08-16. The CSV path keeps the opposite behavior.*

`upload` has always refused to run if any row failed validation. That is right
for a CSV somebody prepared by hand for one batch: the file is small, the
operator just built it, and a bad row means the file is wrong.

It is wrong for the Sheet. The Sheet is a living document of ~10,000 rows filled
in over months, against an archive whose files arrive in instalments. One typo
in row 9,000 blocking the other 9,999 would mean the collection could never go
out incrementally — and during early testing, with only a handful of photos on
the machine, every unresolvable row would block the few that do resolve.

So on the Sheet path a run uploads every row that passes, lists every row that
fails with its errors, and exits non-zero so a failure is never mistaken for a
clean run. The `ia_uploaded` column already makes this safe: a skipped row is
simply one that has no confirmation yet, and the next run picks it up once its
file appears or its metadata is fixed.

The CSV path is unchanged and still refuses. The two sources have genuinely
different shapes and deserve different answers.

## Tool-owned Sheet columns are all `ia_`-prefixed

*Decided 2026-08-16, after the first run against a real copy of the LCPS Sheet.*

The Sheet already had an `Identifier` column, holding the donor's original
reference (`CD 1 01 53 58 1 Central SS`). That normalizes to `identifier` —
which was the tool's reserved column for the minted IA identifier. The first
`upload` would have overwritten every one of those original references with a
freshly minted `lcps-sarasoldphotos-NNNNN`.

The tool's column is therefore renamed `ia_identifier`, joining `ia_uploaded`
and `ia_url`. Every column the tool writes now carries the `ia_` prefix, which
is both a consistent convention and a namespace the Sheet's existing headers do
not collide with. The Sheet keeps `Identifier` meaning exactly what it always
meant.

This was found only by running `validate` against real data. No test would have
caught it: the collision is between the tool's vocabulary and one particular
spreadsheet's, and nothing in the repo knew what that spreadsheet contained.

## `identifier-bib` is written back to the Sheet, not just generated

*Decided 2026-08-16, extending the decision below.*

`identifier-bib` was to be generated at upload time from the resolved file
path and never recorded locally. It is now also written to an `ia_identifier_bib`
column, so the value that went to Internet Archive is reviewable in the Sheet
alongside `ia_uploaded` and `ia_url` rather than being inferable only from a log.

Its value is the donor's original location: the folder column joined to the
filename column (`File on Array` + `/` + `Identifier`). Those are two separate
columns in the real Sheet, which is why the path is assembled rather than read
from one place.

Note this is *not* necessarily the path used to open the file on disk. In the
real Sheet, 225 of 234 filenames carry no extension, so the recorded reference
and the resolvable path differ. See `file_template` for the latter.

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
identifier is permanent; choosing between `Liberty.jpg` and `Liberty.tif` on the
operator's behalf is the kind of silent decision this project rejects
everywhere else.

**Stem matching is case-insensitive.** The archive lives on a Windows-attached
drive whose filesystem is case-insensitive already, so matching case-sensitively
would reject files that do exist. A case-only collision surfaces as the
ambiguity error above rather than a silent wrong pick.

Directory listings are cached per folder. The real Sheet has 234 rows across 5
folders, and the full collection is ~10,000 rows across a similar handful — one
scan per folder rather than one per row.

**`files_dir` is a boundary, not a starting point.** The folder part is
resolved and checked to still be underneath it, so a candidate that *resolves
outside* it — an absolute path, or a `..` climbing past it — is refused rather
than followed. The test is on the resolved directory, not on the text: a `..`
that lands back inside `files_dir` never left, and is allowed. A candidate whose folder
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

## A row's identity is its `file_template` columns, not its `ia_identifier`

*Decided 2026-08-16, after review found the first version of the mid-run-edit
guard was checking its own write.*

The guard's first version compared `ia_identifier` at the target row against
the value the run reserved. That is tautological on the reserve→confirm leg:
reserve had written that exact value at that exact index moments earlier, so
the check could only ever pass. It also ran only before the confirm, leaving
the read→reserve window — which is the *whole run*, since row numbers are
fixed by one initial read and the last chunk's reserve write happens hours
later — entirely unguarded. A row deleted in that window shifts a completed
row up into a target's position, and the reserve write overwrites a permanent
identifier and its live archive.org URL, silently, exit 0.

A row's identity has to be something this tool never writes, or the check is
circular. The `file_template` columns are exactly that: they are the operator's
own data, the tool only ever reads them, and they are already in memory per
row. So the fingerprint is the row's template candidate, captured from the raw
cells before resolution rewrites them, and it is re-checked before **both**
writes. `ia_identifier` is still checked alongside it — blank for a row about
to be reserved, ours once reserved — because it catches a different thing:
somebody else claiming the row.

The residual gap is two rows whose template columns are identical — the same
latent "two rows resolving to the same photograph" case recorded in
`KNOWN-ISSUES.md`. **Its consequence is a misattributed write, not a withheld
one.** With two UNASSIGNED rows pointing at the same file and a row deleted
above them, the fingerprints still match after the shift, so the guard passes:
the item uploads carrying one row's metadata while the identifier, timestamp,
URL and bib land on the *other* row, leaving the first unassigned and due to
be re-minted next run. Bounded — the same bytes reach Internet Archive, and it
cannot touch a DONE or RESERVED row, since those are excluded from the run
before the guard ever sees them — but it is a wrong write, not a refusal, and
the note should not be read as saying otherwise. Fixing it needs a row
identity that does not depend on `file_template` being unique.

## A bad row is skipped; a bad header stops the whole run

*Decided 2026-08-16, alongside "upload uploads the valid rows and reports the
rest" — which is about rows, and says nothing about headers.*

Skipping works because a bad row is contained: it is one photograph, it is
named in the report, and the next run picks it up once fixed. A header defect
is not contained. Two headers normalizing to the same IA field name silently
overwrite one another on *every* row, so there is no subset of rows that is
still trustworthy — skipping the affected rows would mean skipping all of
them, and uploading the rest means uploading data that is already wrong.

So `upload` refuses outright on a header-level error and skips only row-level
ones. The split already existed inside `validate` (`header_results` vs
`row_results`); `upload` reuses that same partition rather than inventing a
second opinion about which is which.

## The four `ia_` columns are required in every mode, including the safe one

*Decided 2026-08-16, when `upload` first wrote to a Sheet.*

`upload` needs somewhere to put `ia_identifier`, `ia_uploaded`, `ia_url` and
`ia_identifier_bib`. The obvious rule — require the columns only when actually
writing — makes the default, no-`--write-identifier` mode succeed against a
Sheet that `--live` would reject. That is worse than useless: the whole point
of the default mode is to be a rehearsal, and a rehearsal that passes where
the performance fails is a false negative on the one run an operator trusts.

So the check does not vary with the mode, and it runs before anything is
uploaded rather than after — a run that uploaded first and only then noticed
it had nowhere to record the identifier would produce exactly the stranded
item the reserve-first ordering exists to prevent.

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
that name cannot ship under it. Naming them in one place lets the field receipt
say so out loud. It previously listed `identifier` among the fields that would
upload, which was simply untrue, and a test asserted that wording and so pinned
the lie in place. **The consequence is real and still open**: on a Sheet with a
donor `Identifier` column, that archival reference now reaches Internet Archive
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

## A blank cell is not an error

*Decided 2026-08-22.* The real Sheet has roughly 3,000 rows: about 2,900 carry
no metadata at all, around 100 are partly filled, and roughly 25 are complete.
Running `validate --live` against it buried the ~150–300 rows with a genuine,
fixable problem under nearly 2,900 identical "missing required column" errors
from rows nobody had touched yet — a nine-row rehearsal the day before showed
the same failure in miniature: three rows failed, in two genuinely different
ways, all reported identically.

**Blank means not-ready; present-but-wrong means invalid, regardless of which
field.** The governing principle: the tool only complains about assertions
someone actually made. Silence about a field is not an error; a wrong value in
it is. `RowValidation` carries this as `readiness`, a property derived from a
new `missing_fields` list rather than a second stored field — one source of
truth, so the two can never drift apart. Readiness is orthogonal to `is_valid`,
not a replacement for it: a row can be not-ready *and* carrying errors at once
(an uncatalogued row whose filename also happens to be a typo).

**`validate` is loud and owns the data; `upload` is quiet and owns the run.**
`validate` itemizes every row carrying a broken assertion, ready or not, and
adds a per-field breakdown of the uncatalogued backlog. `upload` itemizes only
the rows it would otherwise have uploaded — ready rows that fail validation —
and gives the rest one line pointing back at `validate`, because a not-ready
row was never in this run's scope to begin with. The same reasoning settles
`upload`'s exit code: only a row that was actually in scope and failed flips
it. If an uncatalogued row did too, `upload` would return non-zero on every
run until all ~2,900 rows are filled in, which trains an operator to stop
trusting the exit code at all — `validate` is where that signal belongs.

**Test and live apply identical readiness rules.** No lowered bar for the test
Sheet, no opt-in flag to loosen it. Low-quality test *data* belongs in the test
Sheet, which the operator already controls; a lowered *rule* would mean a
green rehearsal no longer predicts a green live run. This generalizes "Test
identifiers carry a per-run stamp" above: a rehearsal that behaves differently
from the real thing is not a rehearsal.

**Readiness is a field on the validation result, not a new `RowState`
member.** `RowState` (`UNASSIGNED`/`RESERVED`/`DONE`) answers "has this row
been minted and uploaded"; readiness answers "has a person filled it in". A
not-ready row *is* `RowState.UNASSIGNED` — the two are different questions
about the same row, not alternative states of it. A fourth `RowState` member
would have broken the invariant `plan_upload_targets` relies on, that every
row is exactly one of the three.

**Where the distinction is made, and why it must stay there.**
`resolve_sheet_files` deliberately blanks `row["file"]` when resolution fails,
so an unverified candidate can never survive to be mistaken for a real path
later. The cost is that afterward a blank row and a broken row are
indistinguishable — both are `row["file"] == ""`. Any readiness check reading
`row["file"]` at that point would misclassify a broken row (a typo'd filename)
as not-ready, and that misclassification runs in the one direction that
matters: a broken row demoted to "nobody has got to it yet" is a row nobody
ever fixes. So classification happens earlier, at candidate construction
inside `resolve_sheet_files` itself — the last point the raw candidate still
exists: a blank or whitespace-only candidate is recorded not-ready before the
resolver is ever called; a non-blank candidate that fails to resolve is a real
error, exactly as before.

**The partially-filled row.** A row with a blank folder cell but a filled-in
filename cell (or the reverse) routes to not-ready, naming only the specific
blank cell — not lumped in with a row nobody has touched at all. The operator
did assert a filename; the folder cell is simply a question not yet answered,
and a blank cell is a not-yet-answered question, not a wrong answer. The
per-field breakdown (`format_readiness_breakdown`) is what surfaces that
distinction to an operator deciding what to work on next, rather than folding
it into one flat, unhelpful total.

## `--limit` counts planned targets, not Sheet rows; `--chunk-size` overrides `CHUNK_SIZE`

*Decided 2026-08-22.*

`--limit` is applied to `plan_upload_targets()`'s own output — after it has
already filtered to rows that are valid, ready, and not already done — never
to raw Sheet rows. The real Sheet is ~3,000 rows, the great majority
uncatalogued (see "A blank cell is not an error"); "stop after scanning N
rows" and "stop after uploading N rows" are very different numbers on a Sheet
shaped like that, and only the second is useful to an operator pacing
against the 5,000/day cap. `--limit 100` on a Sheet with 2,900 uncatalogued
rows and 150 ready ones uploads 100 of the 150, never the first 100 read.
It combines with `--chunk-size` as "this many total, batched this way", not
"this many chunks": `--limit 10 --chunk-size 3` uploads 10 items in batches
of 3, never 10 batches of 3.

Targets sliced away by `--limit` cost nothing: `plan_upload_targets` still
mints an identifier for every pending row up front (minting is pure
arithmetic — see its own docstring), but a target `--limit` drops is never
reserved, so its number is never written anywhere and `next_identifiers()`
mints it again on the next run. No permanent identifier is spent on a row
this run chose not to touch.

`--chunk-size` makes `SheetUploadRun.execute()`'s `CHUNK_SIZE` read (a
module constant previously overridable only by hand-editing it, or by
`monkeypatch.setattr("ia_bulk.CHUNK_SIZE", ...)` in a test) an ordinary
per-run flag, defaulting to `CHUNK_SIZE` (500, IA's per-run cap) so every
existing chunk-boundary test keeps working unchanged. The operator had
already been hand-editing the constant to rehearse the reserve/upload/confirm
protocol across a chunk boundary; this makes that a flag instead.

Both flags are Sheet-path only, rejected on `--csv` the same way
`--write-identifier`/`--dry-run` are rejected there: `run_rows()` (the `--csv`
path) has no reserve/confirm batching for `--chunk-size` to control, and no
ready/not-ready distinction for `--limit`'s counting promise to mean
anything. Both are recorded in the `run_header` log record (see
`ARCHITECTURE.md`, "Logging and resume") so a run that stopped at `--limit`,
or used a non-default `--chunk-size`, stays reconstructable from its own log
alone.

## Rate-limit detection uses a parsed status code, never message text

*Decided 2026-08-22. Revised 2026-08-22 after review found the first version
could false-positive.*

`is_rate_limit_error()` looks **only** at a parsed status-code integer,
never at `str(exc)` or any server-supplied text. The first version of this
function scanned `str(exc)` for `"status 429"`/`"status 503"` substrings;
review found that unsafe in both directions and it was replaced, not merely
tightened:

- a 404 (or anything else) whose body happens to mention `"status 503"` — a
  mirrored error, a proxied message, an echoed request — misclassified as a
  rate limit and would have wrongly stopped the whole run.
- a plain substring test also matches `"status 5031"` or `"status 42900"` —
  digits that merely *contain* 503/429, not equal to them.

A false positive here is worse than a miss: it halts a batch mid-flight, on
a command that creates permanent items, for a reason that is not real. The
replacement checks two structured sources, both verified by reading source
rather than guessed:

1. **`UploadFailed.status_code`** — a new exception (subclassing
   `RuntimeError`, so the pre-existing `pytest.raises(RuntimeError, ...)`
   caller keeps working) that `upload_row()` raises in place of a bare
   `RuntimeError`, carrying the real, parsed `response.status_code` as a
   structured attribute rather than only embedding it in the message text.
2. **`exc.response.status_code`** —
   `requests.exceptions.RequestException.__init__` (the base of
   `HTTPError`) stores whatever `Response` it is given as `.response`
   (`self.response = kwargs.pop("response", None)`, confirmed by reading
   `requests`' own source, not assumed). Tracing the installed
   `internetarchive` 5.10.1's `Item.upload_file()` — the method
   `upload_row()` actually reaches via `internetarchive.upload()` — shows
   that on a real S3 failure it catches the resulting `HTTPError` and
   re-raises via
   `raise type(exc)(error_msg, response=exc.response, request=exc.request)`.
   The **message** there is rebuilt from the S3 XML body's `Message`/
   `Resource` elements (`get_s3_xml_text()` in `internetarchive/utils.py`)
   and loses the numeric status and the S3 error `Code` (e.g. `SlowDown`)
   entirely — but `response=exc.response` is passed through **unchanged**,
   so `.response.status_code` still holds the real, original status even
   though the text does not.

That second finding reverses what the first version of this decision said:
a live rate limit surfacing through the real library's own exception *is*
catchable, just not by reading its message — it has to be read from the
structured `.response.status_code` the library itself preserves. There is
no text-based fallback: neither exception this codebase's upload path can
raise lacks a structured status (see both sources above), so the rate-limit
decision never depends on server-supplied text at all.

503 is Internet Archive's documented S3 overload signal (the installed
`internetarchive` 5.10.1's own `ia upload --retries` help text: *"Number of
times to retry request if S3 returns a 503 SlowDown error"*); 429 is not
IA-upload-specific documentation, but `session.py`'s default urllib3 `Retry`
`status_forcelist` (`[429, 500, 501, 502, 503, 504]`) shows the library's own
authors also treat it as rate-limit-adjacent.

No `--live` run has ever happened, so no real rate-limit response has ever
been captured — both structured sources above are verified against the
installed library's *source*, not against actual IA behavior. A missed
detection (an exception with neither attribute, or a genuinely different
status) is the safe failure direction: the row is logged as one ordinary
failure and the run continues, same as any other transient error, whereas a
false match aborts a run mid-flight over an unrelated error. `--limit`
(above) remains the operator-controlled fallback either way.

## Every recorded timestamp is UTC

*Decided 2026-08-23.*

`run_stamp()` already used `time.gmtime()`, and said why: local time repeats
an hour during the DST fall-back transition, so two rehearsals started an hour
apart across it would mint the same stamp. Exactly the same is true of the
values that outlive the run — the `ia_uploaded` cell, which is the permanent
record of when an archival item was published, and every line of the log that
`--resume-from` and any later audit read. Both were naive local time, with no
offset recorded to reconstruct the real instant from afterwards.

All of them now go through `utc_timestamp()`: ISO-8601 UTC with an explicit
`Z`. The `Z` is not decoration — without it the string is ambiguous, and the
ambiguity is only resolvable by knowing which machine wrote it and what its
clock was set to that day. `open_log()`'s filename stamp is UTC for the same
reason, so a directory listing sorts in the order the runs actually happened.

## A run may not exceed Internet Archive's daily item cap

*Decided 2026-08-23.*

`.claude/CLAUDE.md` states IA's limits as "500 items per upload run,
5000/day". `CHUNK_SIZE` covered the per-run half from the beginning. Nothing
covered the daily half on either path: a bare `upload --live` on a fully
catalogued Sheet planned every ready row, and the only thing between it and a
throttled half-finished batch was rate-limit detection that has never been
observed against a live response.

Both paths now refuse a run over `DAILY_ITEM_CAP` and name the fix. Three
choices went into that:

- **Refuse rather than silently cap.** Defaulting `--limit` to 5,000 would
  make an over-cap run stop short and still report cleanly, which reads as a
  complete run. That is the same trap the `--limit <= 0` guard already exists
  to avoid.
- **Check after the `--limit` slice.** A Sheet holding more ready rows than
  the cap is not itself an error; running at all of them in one day is. So
  `--limit` is the ordinary way through, not a special case.
- **An explicit override, not none at all.** The cap is IA's, not ours, and an
  account whose cap has been raised is a real case — but it has to be stated.
  `--allow-over-daily-cap` is deliberately verbose enough not to be passed by
  reflex.

It applies in test mode too. A rehearsal uploads to `test_collection` through
the same account and spends the same quota.

## Minted numbers are re-checked against the Sheet before reserving

*Decided 2026-08-23.*

`plan_upload_targets()` mints the whole run's numbers up front, as `max+1`,
`max+2`, … over a single read — see "Identifiers are minted by `upload` and
written back to the Sheet" for why minting happens once rather than per chunk.
On a full-collection run that read can be hours old by the time the last chunk
reserves.

`split_moved_targets()` guards the reserve write, but it only inspects each
target's *own* row. A number written to a row this run is not targeting is
invisible to it — and that is precisely the case that mints a duplicate: two
Sheet rows carrying one permanent identifier. `internetarchive.upload()`
appends to an existing item rather than refusing, so the second upload puts a
second photograph inside the first one's item, unrenameably.

`check_claimed_identifiers()` closes it: `SheetSnapshot` now carries every
`ia_identifier` the fresh read holds, and the reserve leg compares this run's
minted numbers against all of them.

It stops the whole run rather than dropping the colliding target. Every number
this run holds came out of the same arithmetic over the same stale read, so
one collision means the maximum was wrong and the rest are suspect too —
reserving its neighbours would be reserving numbers that are wrong for the
same reason. Nothing has been reserved or uploaded at that point, so a rerun
re-reads, re-mints from the real maximum, and proceeds.

Only newly-minted targets are checked, and only before reserve. A RESERVED
row's identifier is already in the Sheet — that is what RESERVED means — and
after the reserve write so are this run's own, so checking either would stop
every ordinary run on its own writes.

## The Sheet is the correction

*Decided 2026-08-23.*

`validate` and `upload` moved to reading the Sheet live; `sync-metadata` did
not, and was the last command still requiring a hand-made CSV. That left the
round trip the whole "Sheet is the source of truth" premise promises — fix a
description in the Sheet, have it show up on the site — impossible without
exporting a CSV by hand first. Worse, `--csv` was a required POSITIONAL, so
`sync-metadata --project X` did not fail informatively; it printed an argparse
usage error about a missing `csv`.

The Sheet already held everything needed. `upload`'s confirm write records
`ia_uploaded` (this row is done) and `ia_url` (the item it became), and
`ia_url` carries the per-run `zztest-` stamp in test mode. So the Sheet path
needs no log, no CSV, and no stamp arithmetic: read `ia_url`, send that row's
current columns. It handles a Sheet whose rows were uploaded by *different*
runs, under different stamps, without knowing that happened.

Four choices:

- **Scope is `RowState.DONE` and nothing else.** An UNASSIGNED row has no item
  to correct; a RESERVED row's upload never confirmed, and `upload` already
  retries those.
- **Every DONE row is sent, every run.** Internet Archive answers *no changes
  to `_meta.xml`* for an item that already matches, which becomes
  `MetadataUnchanged` and is counted as `unchanged`. That makes a full sync
  idempotent by construction, so there is no need for per-row change
  detection, a fifth `ia_` column, or a second place for the Sheet and the
  item to drift apart.
- **A blank cell means "leave this field alone", as on the `--csv` path.**
  Treating the Sheet as literally canonical — blank means delete — is more
  faithful in principle, but an accidental clear, a bad paste, or a row shift
  would silently strip metadata from a permanent public item with no undo.
  `REMOVE_TAG` deletes, deliberately and visibly, and one rule holds across
  both paths.
- **Mode mismatches are refused, not reported afterwards.** The Sheet is
  itself the mode boundary (test and live are different spreadsheets), so an
  `ia_url` pointing the wrong way means the wrong Sheet is in the registry or
  someone pasted across. Sending a live correction to a rehearsal item, or the
  reverse, is not fixed by rerunning.

`sheet_metadata_fields()` is shared with `upload`, so a column that uploads
but does not sync — or the reverse — cannot exist.

## `sync-metadata --csv` reads its targets from the upload log

*Decided 2026-08-23, closing a defect introduced by the per-run stamp above.*

"Test identifiers carry a per-run stamp" solved rehearsal collisions on
`upload` and silently broke `sync-metadata`, which nothing noticed because
that command had never been run.

`cmd_sync_metadata` derived its target the same way `upload` does, by calling
`effective_identifier()` with `run_stamp()`. But a stamp is unique to the
invocation, so a correction run computed `zztest-<today's stamp>-<identifier>`
for an item created under *yesterday's* stamp — an identifier that has never
existed. Every row would have failed, with a message about the item not being
found rather than about the real cause. Test-mode `sync-metadata` was
unusable, which left `--live` as the only way to exercise it: the worst
possible place to run something for the first time.

The mapping already existed. Every upload log line records both `identifier`
(the real, permanent one) and `uploaded_as` (what actually went over the
wire), precisely so a later reader can tell what landed where. `--from-log`
points at that log and `load_uploaded_as()` reads the pairs out of it.

Three choices went into the shape:

- **Required in test mode, optional with `--live`.** Live identifiers are
  unstamped, so there is nothing to look up. In test mode there is no value
  the CSV could carry that would work — the operator is told never to author a
  `zztest-` identifier by hand, and `check_identifier` would reject one
  anyway — so refusing is the only honest answer.
- **A miss is an error, never a fall back.** Recomputing the target when the
  log does not name a row is exactly the bug being fixed, and it fails
  silently. Rows the log does not record are reported before anything is sent,
  all-or-nothing like the rest of the CSV path.
- **Mode-filtered, like `--resume-from`.** A test log records where a
  `zztest-` item went and says nothing about the real one. Both flags read
  logs through the same `_read_log_results()`, which drops entries from the
  other mode and tolerates a truncated line.

`--from-log` is deliberately separate from `--resume-from` rather than folded
into it: they read the same file for opposite purposes. `--resume-from` says
which rows to SKIP; `--from-log` says where the rows that remain should be
SENT. A single flag doing both would make "skip what is done" and "correct
what is done" the same instruction, which they are not.

## Still open

- ~~`collection_key` has never been confirmed~~ **Settled 2026-08-23**:
  `collection_key` is `"lcps"` — the first segment of every minted identifier
  and of every item's permanent public URL
  (`archive.org/details/lcps-sarasoldphotos-00001`). Confirmed as the most
  specific pointer to the organization; `lcpsociety` (the IA account's domain)
  and `lcpsdigitalcollection` (the parent collection) were both considered and
  rejected as slightly off. This value never reaches Internet Archive — it is
  purely the identifier namespace, used by `format_identifier()`,
  `next_identifiers()` and `check_identifier()`.
  **Not to be confused with `ia_collection`** — see below; they are unrelated
  values, and both are now settled.
- ~~The real IA collection has never been confirmed~~ **Settled 2026-08-22**:
  `ia_collection` is `sarasoldphotos`, the subcollection — confirmed to exist
  (`archive.org/metadata/sarasoldphotos`, title "Sara's Old Photos") and
  already parented to `lcpsdigitalcollection`, the LCPS parent collection.
  Items are tagged into the subcollection alone; membership in the parent
  (and in `clatsopcountyhistoricalsociety` and `americana`, both listed on
  `sarasoldphotos`'s own `collection` field) is transitive, so listing more
  than one collection per item is unnecessary. A same-day earlier pass had
  recorded this value as `lcpsdigitalcollection` itself — that collection is
  real and does exist, but it is the *parent*, not the subcollection the
  operator decided items should carry; this replaces that value before any
  `--live` run. Nothing in this tool checks that a collection exists, so this
  remains a manual, one-time verification.
- **Also settled 2026-08-22**: the registry's project id was corrected from
  `sarahsoldphotos` to `sarasoldphotos` — the donor is Sara, and the `h` was
  only ever the operator's habitual spelling, never a confirmed value.
  `archive.org/metadata/sarahsoldphotos` returned `{}` (no such item);
  `archive.org/metadata/sarasoldphotos` returned the real collection recorded
  above. Because **no `--live` run has ever happened**, no permanent
  identifier was ever minted under the misspelling, which is the only reason
  this correction was still cheap — it reaches the registry key, every
  minted-identifier example in the docs and tests, `README.md`, and
  `docs/OPERATIONS.md`. It does **not** reach the `zztest-`-prefixed sandbox
  identifiers already recorded elsewhere in these docs as historical fact
  (see "Test identifiers carry a per-run stamp" and "`identifier-bib` and
  `mediatype` are generated, not columns" above) — those items were actually
  minted, under the old spelling, in `test_collection`, so rewriting the docs
  to claim otherwise would misstate what is really out there, auto-expiring
  or not. `collection_key` (`"lcps"`) is untouched by either correction; it
  remains a third, unrelated value — see above.
- Whether IA emits a distinguishable signal at its 5,000/day cap, as opposed to
  generic throttling, **remains open** — no `--live` run has ever happened, so
  no real rate-limit response has ever been captured. **2026-08-22, revised
  same day:** `is_rate_limit_error()` now stops a run on a `429`/`503` upload
  failure, read from a PARSED status-code integer rather than message text
  (see "Rate-limit detection uses a parsed status code..." above, which
  replaced an earlier version of this same decision after review found the
  text-scanning approach could false-positive). Tracing the installed
  library's source shows the structured status survives even the path where
  the *message* is rewritten and loses it, so this should catch a real 429/503
  reaching this codebase's upload path either way it can arrive - but "should"
  is still verified only against the installed library's source, not an
  actual response from archive.org, since no `--live` run has happened.
  **2026-08-23:** the run no longer depends on detecting the cap at all to
  respect it — both paths refuse to *start* a run of more than 5,000 items
  (see "A run may not exceed Internet Archive's daily item cap" above). What
  stays open is only whether IA's response is distinguishable when the cap is
  hit anyway, e.g. across two runs in one day, which the tool does not track.
  `--limit` (see "`--limit` counts planned targets..." above) remains the
  operator-controlled fallback either way.
- ~~How a run establishes the next free `NUMBER`~~ **Settled 2026-08-08**:
  reserve in the Sheet before uploading, and track completion in an
  `ia_uploaded` column so an interrupted run is recoverable.
- ~~Which Google credential type the Sheet integration uses~~ **Settled
  2026-08-08**: OAuth with the **Internal** user type, which requires the Cloud
  project to sit inside the lcpsociety.org organization and the operator to
  hold an `@lcpsociety.org` account. An API key was ruled out (read-only, and
  only reaches publicly shared sheets); a service account was ruled out because
  it loses per-person attribution in the Sheet's edit history.
- Automating the Sheet → CSV *export* was raised and deferred pending maintainer
  input. **Superseded 2026-08-08** by reading the Sheet directly (above).
