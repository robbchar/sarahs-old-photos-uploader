# IA Bulk Upload CLI — Architecture

## Purpose
Single-script CLI (`ia_bulk.py`) for validating, uploading, and syncing
metadata for Internet Archive items from a CSV exported from the LCPS
Google Sheet. Generic to "a project" so a second LCPS project can reuse
this pipeline — see `projects_registry.json`.

## CSV schemas

### `validate` / `upload`
Required columns: `identifier`, `file`, `mediatype`, `title`, `date`.
All other columns pass through untouched as IA item metadata.

- `identifier`: pre-assigned in the Sheet, permanent, never generated or
  renamed by this tool. Must match `COLLECTIONKEY-PROJECTID-NUMBER`,
  lowercase, hyphen-separated, 5-digit zero-padded NUMBER.
- `file`: filename (optionally with a relative subpath), resolved against
  `--files-dir`.
- `mediatype`, `title`, `date`: required, non-empty.

### `sync-metadata`
Only requires an `identifier` column plus whichever metadata columns
changed. Does not require `file`, `mediatype`, `title`, or `date`.

## Identifier scheme
See `.claude/CLAUDE.md` for the full identifier scheme and project
registry rationale. `projects_registry.json` holds the known
`collection_key` and `PROJECTID` values; `validate` and `sync-metadata`
reject any identifier whose prefix isn't registered there.

Test-collection identifiers replace `COLLECTIONKEY` with the literal
`zztest`, keeping the real `PROJECTID` — e.g. `zztest-astoriaphotos-00001`,
not `zztest-lcps-00001`. `check_identifier` accepts either the real
`collection_key` or `zztest` as the first segment, as long as the
`PROJECTID` segment is registered.

## Chunking
All `upload`/`sync-metadata` runs process rows in batches of 500 (IA's
per-run batch limit), via `chunk_rows()`. This is a pacing/checkpoint
boundary, not a literal separate CSV file per chunk — each row is
uploaded individually through the `internetarchive` Python library so
outcomes are captured per-row.

## Logging and resume
Every `upload`/`sync-metadata` run writes a timestamped JSONL log to
`logs/<command>-<timestamp>.jsonl`, one line per row:
`{identifier, file, status, error, timestamp}`.

`--resume-from <log>` reads identifiers marked `"status": "success"` from
a prior log and skips them. The new run still writes its own complete
log (carrying forward the skipped identifiers as pre-recorded successes),
so each log is a self-contained record of what happened by that point.

## Safety rail
Default target is `test_collection`; `--live` is required to target the
real collection. When not `--live`, every identifier in the CSV must be
prefixed `zztest-`, checked before any network call.
