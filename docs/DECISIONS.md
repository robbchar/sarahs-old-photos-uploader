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

## Identifiers are assigned in the Sheet, never generated here

The tool validates identifiers; it never mints them. Identifiers are permanent
once uploaded, so the numbering authority lives in one place — the Sheet — and
a script re-run can never invent a colliding or off-by-one number. The registry
(`projects_registry.json`) exists so the prefix half of an identifier can be
checked against a known list rather than a regex alone.

Original filenames and donor folder structure are deliberately not part of the
identifier; they belong in `identifier-bib`.

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
- Automating the Sheet → CSV *export* was raised and deferred pending maintainer
  input. It would not remove the manual schema check in
  [`CSV-PREPARATION.md`](CSV-PREPARATION.md).
