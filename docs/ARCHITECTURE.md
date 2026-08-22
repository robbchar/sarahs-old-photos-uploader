# IA Bulk Upload CLI — Architecture

This is the design reference. For running a batch see
[`OPERATIONS.md`](OPERATIONS.md); for preparing the CSV see
[`CSV-PREPARATION.md`](CSV-PREPARATION.md); for verified defects see
[`KNOWN-ISSUES.md`](KNOWN-ISSUES.md); for rationale and reversed decisions see
[`DECISIONS.md`](DECISIONS.md).

## Purpose
Single-script CLI (`ia_bulk.py`) for validating, uploading, and syncing
metadata for Internet Archive items from a CSV exported from the LCPS
Google Sheet. Generic to "a project" so a second LCPS project can reuse
this pipeline — see `projects_registry.json`.

## CSV schemas

### `validate` / `upload`
Required columns: `identifier`, `file`, `mediatype`, `title`.
All other columns pass through untouched as IA item metadata.

`read_csv()` returns a `CsvData` carrying the header and the rows together,
because validating either alone misses the failure that matters:
`check_row_shape()` can only see a row/header field-count mismatch, and
`check_header()` can only see the header text. Both must pass before any
network call — `cmd_validate`, `cmd_upload`, and `cmd_sync_metadata` all run
`header_validation()` alongside their row checks.

- `identifier`: pre-assigned in the Sheet, permanent, never generated or
  renamed by this tool. Must match `COLLECTIONKEY-PROJECTID-NUMBER`,
  lowercase, hyphen-separated, 5-digit zero-padded NUMBER.
- `file`: filename (optionally with a relative subpath), resolved against
  `--files-dir`.
- `mediatype`, `title`: required, non-empty.
- `date`: optional and free-form — IA doesn't enforce a date format.
  `upload` fills a blank `date` cell with `[n.d.]` (the standard archival
  "no date" abbreviation) rather than omitting the field, so every IA item
  ends up with a date value either way.

### `sync-metadata`
Only requires an `identifier` column plus whichever metadata columns
changed. Does not require `file`, `mediatype`, `title`, or `date`.
A blank cell means "leave this field alone" — `update_metadata_row` drops
blank cells from the request entirely rather than sending an empty string,
since the whole point of this CSV shape is to list only what changed. To
actually delete an existing field on the IA item, put the literal value
`REMOVE_TAG` in that cell: the `internetarchive` library (and the official
`ia` CLI's `--modify field:REMOVE_TAG`) treats that exact string as a
delete sentinel and issues a metadata "remove" op for the field.

## Identifier scheme
See `.claude/Claude.md` for the full identifier scheme and project
registry rationale. `projects_registry.json` holds the known
`collection_key` and `PROJECTID` values; `validate` and `sync-metadata`
reject any identifier whose prefix isn't registered there.

The CSV's `identifier` column always holds the real, permanent identifier
— `check_identifier` only accepts the registry's actual `collection_key`
as the first segment. There is no separate "test" identifier form in the
CSV; see "Safety rail" below for how test runs are kept safe instead.

## Chunking
All `upload`/`sync-metadata` runs process rows in batches of 500 (IA's
per-run batch limit) by default, via `chunk_rows()`. This is a
pacing/checkpoint boundary, not a literal separate CSV file per chunk —
each row is uploaded individually through the `internetarchive` Python
library so outcomes are captured per-row. On the Sheet path, `upload
--chunk-size N` overrides the batch size for that run
(`SheetUploadRun.chunk_size`, threaded into `chunk_rows()` at the same call
site `CHUNK_SIZE` used to be read from directly); `--csv` has no
`--chunk-size` of its own, since `run_rows()`'s chunking has no per-chunk
Sheet write for a batch size to actually change.

`upload --limit N` (Sheet path only) caps how many *planned* upload targets
(valid, ready, not already done — `plan_upload_targets()`'s own output) a
single invocation processes at all, applied before chunking: `--limit 10
--chunk-size 3` means 10 items total, in batches of 3. It counts targets
in scope, never raw Sheet rows scanned, and it and `--chunk-size` are both
recorded in the run's `run_header` log record (below) so a capped or
rechunked run stays reconstructable later. See `DECISIONS.md`, "`--limit`
counts planned targets...".

If an upload fails with what `is_rate_limit_error()` recognizes as
Internet Archive's rate limit, `SheetUploadRun.execute()` stops the whole
run after confirming whatever it already uploaded in the current chunk (and
any earlier chunk), rather than treating it as one more per-row failure and
continuing. This detector is best-effort, not confirmed against a real
rate-limit response — see `DECISIONS.md`, "Still open".

## Progress output
`upload`/`sync-metadata` print a `[position/total] ...` line to stdout
before each row, plus a `X uploaded successfully, Y error(s)` summary line
before the final `log written to <path>` line, so a run is never silently
quiet. `upload_row` also passes `verbose=True` through to
`internetarchive.upload()`, which prints its own `tqdm` byte-progress bar
per file — that's IA's own upload status, not something this tool
fabricates. It also passes `checksum=True`, so re-running `upload` against
a CSV whose files haven't changed skips re-uploading (and re-triggering
IA's `derive` task) for anything already present with a matching MD5.

## Logging and resume
Every `upload`/`sync-metadata` run writes a timestamped JSONL log to
`logs/<command>-<timestamp>.jsonl`, one line per row:
`{identifier, file, status, error, uploaded_as, live, timestamp}`.

On the Sheet path, `log_run_header()` writes one more record as the log's
**first** line, before any row result: `{record: "run_header", timestamp,
project, live, dry_run, sheet_id, collection, files_dir, file_template,
columns, held_back, required_for_upload, limit, chunk_size}`. `columns`
and `held_back` come from that run's `ColumnMap` (every header the Sheet
had, and which were excluded as `(LCPS Internal)`); `required_for_upload`,
`limit` and `chunk_size` are the readiness/scope/batching rules in effect
that run. All of these can change between runs even though none of them
changes per row within one, which is why they are captured once here
rather than left to be reconstructed later from a Sheet that has since
moved on. `load_prior_successes()` explicitly skips this record by its
`record` field (not merely by lacking a `status` key, which would also
happen to work but for the wrong reason) so it is never mistaken for a
row result.
`identifier` is always the real CSV identifier. `uploaded_as` is the
identifier actually sent to IA for that row (see "Safety rail" below), so
you can see exactly what landed on the site. `live` records which mode
(test vs. `--live`) produced that row's result.

`--resume-from <log>` reads identifiers marked `"status": "success"` or
`"status": "unchanged"` from a prior log **written in the same mode as the
current run** and skips them; `load_prior_successes(log_path, live)` filters
on the log's `live` field before matching. This is deliberate, not
incidental: since the CSV's `identifier` column is identical for a test run
and a `--live` run of the same file (only `uploaded_as` differs), a success
recorded in `test_collection` says nothing about whether the real item
exists — treating it as interchangeable with a `--live` success would let a
`--live --resume-from <test-run-log>` silently skip a real upload while
still reporting it as successful. Log lines written before this `live`
field existed have no mode recorded and are treated as matching neither
mode, so old-format logs are simply not used to skip anything rather than
skip in the wrong mode. The new run still writes its own complete log
(carrying forward the skipped identifiers as pre-recorded successes), so
each log is a self-contained record of what happened by that point.

Rows carried over via `--resume-from` also skip re-validation in
`validate_rows`/`validate_identifiers` (their identifiers already passed a
prior run's checks) - `skip_identifiers` short-circuits the per-row
checks but still records the identifier for duplicate detection, and row
numbers stay aligned with the full CSV either way.

## `sync-metadata`'s "unchanged" status
IA's metadata-update endpoint returns an HTTP 400 with
`{"error": "no changes to _meta.xml"}` when every field in the request
already matches what's on the item — i.e. nothing was wrong, there was
just nothing to do. `update_metadata_row` detects that specific error and
raises `MetadataUnchanged` instead of `RuntimeError`; `cmd_sync_metadata`
catches it separately, logs the row as `"status": "unchanged"` (not
`"failure"`), and reports it in its own summary bucket
(`X updated successfully, Y unchanged, Z error(s)`) so a CSV row that's
already correct doesn't inflate the error count or flip the exit code.

## Safety rail
Default target is `test_collection`; `--live` is required to target the
real collection and use the real identifier as-is. When not `--live`,
`effective_identifier()` prepends `zztest-<run's stamp>-` to the real
identifier for every network call (e.g.
`zztest-20260819t144907-lcps-sarahsoldphotos-00001`) — this happens
automatically, in code, rather than requiring the CSV to already contain
test-prefixed identifiers. The CSV itself never needs to change between a
test run and a `--live` run.

The stamp (`run_stamp()`) is computed once per invocation and shared by
every row that run touches, so a rehearsal's items group together and never
collide with a previous rehearsal's — see
[`docs/DECISIONS.md`](DECISIONS.md#test-identifiers-carry-a-per-run-stamp)
for why a bare `zztest-` prefix made every fresh-Sheet rehearsal collide
with the last one. (A *resumed* run is its own invocation with its own
stamp, so its items land under a second stamp, not the original run's —
`--resume-from` still recognizes them as done since it matches on the real
`identifier`, never on the stamped `uploaded_as`.) `--live` identifiers
never carry a stamp: they are the permanent, public ones and must stay a
pure function of the Sheet/CSV.

## Known gaps
Verified defects with reproductions live in
[`KNOWN-ISSUES.md`](KNOWN-ISSUES.md). The design-level gaps are below.

`projects_registry.json`'s `collection_key` value (`lcps`) is a placeholder
— confirm it against LCPS's actual IA collection identifier before any
`--live` run. A wrong value here doesn't cause data loss (validation would
just reject every real identifier), but it needs to be right before real
uploads can pass `validate`.

The target IA collection is the project's `ia_collection` in
`projects_registry.json`. `upload`'s `--collection` flag no longer defaults
to `"lcps"` — that string is not a real Internet Archive collection, and a
`--live` run would have pushed real photographs at a collection that does
not exist and reported success. The flag survives only as an explicit
override on the `--csv` path, where `--live` now refuses to run without it;
on the Sheet path passing it is an error rather than a silently ignored
value. Nothing still validates `ia_collection` against IA itself, so confirm
it by hand once, in version control, before the first `--live` run —
`upload --dry-run` prints everything the run would do without doing any of
it. See `DECISIONS.md`, "Technical configuration lives in the registry".

The production CSV export from the LCPS Google Sheet
(`data/LCPS Digital Archive Metadata Spreadsheet - Sheet1.csv`) does not
match the schema this tool requires: its headers are capitalized
(`File on Array`, `Identifier`, `Title`, `Date`, `Theme`, ...) rather than
the lowercase `identifier`/`file`/`mediatype`/`title`/`date` columns listed
under "CSV schemas" above, and it has no `mediatype` column at all. Running
`validate`/`upload` directly against the raw export will fail every row.
The raw export must be transformed by hand into a CSV matching the exact
required schema — including adding a `mediatype` column — before it's
passed to this tool. That transformation is a deliberate, explicit step a
human performs, not something this CLI does automatically. See
[`CSV-PREPARATION.md`](CSV-PREPARATION.md) for the procedure and the
failure modes.

`validate` backstops the structural half of that transformation:
`check_header()` rejects headers with surrounding whitespace, duplicates, or a
case variant of a column the script reads by name, and `check_row_shape()`
rejects any row whose field count disagrees with the header — which is what an
unquoted comma in a header cell produces. Header problems are reported as
row 1. It cannot check whether a correctly-formed header is *semantically*
right, so the manual proofread in
[`CSV-PREPARATION.md`](CSV-PREPARATION.md) still matters.
