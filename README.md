# IA Bulk Upload CLI

A small Python CLI for validating, uploading, and syncing metadata for
Internet Archive items in bulk, driven by a CSV exported from the LCPS
Google Sheet. Built for the Lower Columbia Preservation Society's (LCPS)
Astoria historical photo archive, but kept generic to "a project" so a
second LCPS project can reuse the same pipeline.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design —
CSV schema, identifier scheme, chunking, logging/resume, and the safety
rail.

## Identifier scheme
`COLLECTIONKEY-PROJECTID-NUMBER` — all lowercase, hyphen-separated.
- COLLECTIONKEY: LCPS's IA collection identifier (confirm before real runs)
- PROJECTID: short project code, e.g. `astoriaphotos` (tracked in a small
  project registry, not invented ad hoc per script run)
- NUMBER: 5-digit zero-padded sequential number, unique per project
Identifiers are permanent once uploaded — never reused, never renamed.
Original filenames/donor folder structure are NOT part of the identifier;
they go in the `identifier-bib` metadata field instead.

## Setup
```bash
pip install -r requirements.txt
```

Requires `internetarchive` to be authenticated against the shared org
account (`ia configure`) before running `upload` or `sync-metadata`.

## Commands

### `validate` — check a CSV offline, no network calls

```bash
python ia_bulk.py validate items.csv --files-dir ./photos --registry projects_registry.json
```

Checks, per row:
- `file` exists on disk (resolved against `--files-dir`)
- `identifier` is unique in the CSV and matches the
  `COLLECTIONKEY-PROJECTID-NUMBER` scheme (lowercase, 5-digit zero-padded
  NUMBER), with the prefix registered in `projects_registry.json`
- `mediatype`, `title`, `date` are present and non-empty

Prints a pass/fail report per row and exits non-zero if anything fails.
Always run this before `upload`.

### `upload` — upload a validated CSV

```bash
python ia_bulk.py upload items.csv --files-dir ./photos
```

- Re-validates the CSV, then blocks on the safety rail (see below), before
  any network call
- Processes rows in chunks of 500 (Internet Archive's per-run batch limit)
- Uploads each row via the `internetarchive` Python library (not the `ia`
  CLI), so per-row success/failure is captured directly
- Writes a timestamped JSONL log to `logs/upload-<timestamp>.jsonl`, one
  line per row: `{identifier, file, status, error, timestamp}`
- `--resume-from <log>` skips identifiers already marked `"success"` in a
  prior log, and still writes a complete log of its own

### `sync-metadata` — update metadata on already-uploaded items

```bash
python ia_bulk.py sync-metadata updates.csv
```

Same chunking/logging/safety-rail behavior as `upload`, but only requires
an `identifier` column plus whichever metadata columns changed — no
`file`/`mediatype`/`title`/`date` needed.

## Safety rail

By default every command targets IA's `test_collection` sandbox, and
`upload`/`sync-metadata` refuse to run unless every identifier in the CSV
is prefixed `zztest-` (e.g. `zztest-astoriaphotos-00001`). Pass `--live` to
target the real collection with real identifiers — do this deliberately,
never as a default.

```bash
python ia_bulk.py upload items.csv --live --collection lcps
```

**Before any `--live` run**, double-check both of these by hand — neither
is validated automatically, and both default to placeholder values (see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#known-gaps)):
- `projects_registry.json`'s `collection_key`
- `upload`'s `--collection` flag

## Known limitation: raw Sheet export needs preparation

The raw CSV exported from the LCPS Google Sheet does not match the schema
`validate`/`upload` require (capitalized headers, no `mediatype` column).
It must be transformed by hand into the required lowercase schema before
running this tool — see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#known-gaps) for details.
This is a deliberate manual step, not something this CLI automates.

## Tests

```bash
python -m pytest test_ia_bulk.py -v
```
