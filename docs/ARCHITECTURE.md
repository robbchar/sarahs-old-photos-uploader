# IA Bulk Upload CLI — Architecture

## Purpose
Single-script CLI (`ia_bulk.py`) for validating, uploading, and syncing
metadata for Internet Archive items from a CSV exported from the LCPS
Google Sheet. Generic to "a project" so a second LCPS project can reuse
this pipeline — see `projects_registry.json`.

## CSV schemas

### `validate` / `upload`
Required columns: `identifier`, `file`, `mediatype`, `title`.
All other columns pass through untouched as IA item metadata.

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
per-run batch limit), via `chunk_rows()`. This is a pacing/checkpoint
boundary, not a literal separate CSV file per chunk — each row is
uploaded individually through the `internetarchive` Python library so
outcomes are captured per-row.

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
`{identifier, file, status, error, uploaded_as, timestamp}`. `identifier`
is always the real CSV identifier — used for `--resume-from` matching, so
resuming works the same regardless of `--live`. `uploaded_as` is the
identifier actually sent to IA for that row (see "Safety rail" below), so
you can see exactly what landed on the site.

`--resume-from <log>` reads identifiers marked `"status": "success"` or
`"status": "unchanged"` from a prior log and skips them. The new run still
writes its own complete log (carrying forward the skipped identifiers as
pre-recorded successes), so each log is a self-contained record of what
happened by that point.

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
`effective_identifier()` prepends `zztest-` to the real identifier for
every network call (`zztest-lcps-sarahsoldphotos-00001`) — this happens
automatically, in code, rather than requiring the CSV to already contain
test-prefixed identifiers. The CSV itself never needs to change between a
test run and a `--live` run.

## Known gaps
`projects_registry.json`'s `collection_key` value (`lcps`) is a placeholder
— confirm it against LCPS's actual IA collection identifier before any
`--live` run. A wrong value here doesn't cause data loss (validation would
just reject every real identifier), but it needs to be right before real
uploads can pass `validate`.

`upload`'s `--collection` flag also defaults to `"lcps"` (see `build_parser`
in `ia_bulk.py`) and is used as-is on `--live` runs. Unlike the
`collection_key` placeholder above, nothing validates this value — a wrong
or stale `--collection` on a `--live` run will silently push real files to
the wrong IA collection instead of failing. Double-check `--collection`
by hand before every `--live upload` run. The default is intentionally left
as `"lcps"` (matches the original plan); this is a documentation warning,
not a call to change the CLI's behavior.

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
human performs, not something this CLI does automatically.
