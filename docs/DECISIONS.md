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
- `--collection` keeps its `"lcps"` default and stays unvalidated (documented
  as a warning rather than changed)
- Ragged-CSV handling relies on a broad `except` in `run_rows`

## Still open

- `collection_key` and `--collection` have never been confirmed against LCPS's
  real IA collection. No `--live` run has ever been made.
- How a run establishes the next free `NUMBER` — and how it stays correct when
  a run is interrupted partway — is undecided. See the identifier section above.
- Which Google credential type the Sheet integration uses is undecided. An API
  key is ruled out: Sheets API keys are read-only and only reach publicly
  shared sheets, and the identifier write-back needs write access.
- Automating the Sheet → CSV *export* was raised and deferred pending maintainer
  input. **Superseded 2026-08-08** by reading the Sheet directly (above).
