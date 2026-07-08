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
- PROJECTID: short project code, e.g. `photosexample` (illustrative only —
  see `projects_registry.json` for the actual registered codes; tracked in
  a small project registry, not invented ad hoc per script run)
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
- `mediatype`, `title` are present and non-empty; `date` is optional —
  `upload` fills a blank `date` with `[n.d.]` rather than omitting it

Prints a pass/fail report per row and exits non-zero if anything fails.
Always run this before `upload`.

### `upload` — upload a validated CSV

```bash
python ia_bulk.py upload items.csv --files-dir ./photos
```

- Re-validates the CSV before any network call
- Processes rows in chunks of 500 (Internet Archive's per-run batch limit)
- Uploads each row via the `internetarchive` Python library (not the `ia`
  CLI), so per-row success/failure is captured directly
- Writes a timestamped JSONL log to `logs/upload-<timestamp>.jsonl`, one
  line per row: `{identifier, file, status, error, uploaded_as, live, timestamp}`
- `--resume-from <log>` skips identifiers already marked `"success"` or
  `"unchanged"` **in the same mode** (test vs. `--live`) as this run, and
  still writes a complete log of its own

### `sync-metadata` — update metadata on already-uploaded items

```bash
python ia_bulk.py sync-metadata updates.csv
```

Same chunking/logging/safety-rail behavior as `upload`, but only requires
an `identifier` column plus whichever metadata columns changed — no
`file`/`mediatype`/`title`/`date` needed. A blank cell means "leave this
field alone"; to actually delete an existing field on the IA item, put the
literal value `REMOVE_TAG` in that cell (the same sentinel the official
`ia` CLI's `--modify field:REMOVE_TAG` uses).

## Safety rail

By default every command targets IA's `test_collection` sandbox. The CSV's
`identifier` column always holds the real, permanent identifier — never
author a `zztest-` identifier by hand in the CSV. Instead, `upload` and
`sync-metadata` automatically prepend `zztest-` to the real identifier for
every network call (e.g. `lcps-astoriaphotos-00001` becomes
`zztest-lcps-astoriaphotos-00001`) unless `--live` is passed. Pass `--live`
to target the real collection with the real identifier as-is — do this
deliberately, never as a default.

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

## Linting and type checking

```bash
python -m ruff check .        # style/lint (unused imports, bug-prone patterns, ...)
python -m pyright ia_bulk.py test_ia_bulk.py   # static type checking (same engine as VS Code's Pylance)
```

`pyright` is the command-line engine behind the Pylance VS Code extension —
running it here gives the same diagnostics Pylance would show in the editor,
without needing VS Code open.
