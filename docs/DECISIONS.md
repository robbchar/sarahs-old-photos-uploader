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
freshly minted `lcps-sarahsoldphotos-NNNNN`.

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
folder column it looks for an exact filename match first, then for a file whose
stem matches ignoring extension. What it finds is what gets used.

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

## Still open

- `collection_key` and the real IA collection have never been confirmed against
  LCPS. No `--live` run has ever been made. This is now the main live blocker.
- Whether IA emits a distinguishable signal at its 5,000/day cap, as opposed to
  generic throttling. `--limit` covers the case where it does not.
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
