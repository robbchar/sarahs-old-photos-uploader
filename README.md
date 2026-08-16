# IA Bulk Upload CLI

A small Python CLI for validating, uploading, and syncing metadata for
Internet Archive items in bulk, driven by a CSV exported from the LCPS
Google Sheet. Built for the Lower Columbia Preservation Society's (LCPS)
Astoria historical photo archive, but kept generic to "a project" so a
second LCPS project can reuse the same pipeline.

## Docs

- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — runbook: how to run a batch,
  pre-live checklist, resuming, batch limits. **Start here to run something.**
- [`docs/CSV-PREPARATION.md`](docs/CSV-PREPARATION.md) — turning the raw Sheet
  export into the required schema, and the traps that don't fail loudly.
- [`docs/KNOWN-ISSUES.md`](docs/KNOWN-ISSUES.md) — verified defects and gaps,
  with reproductions.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — full design: CSV schema,
  identifier scheme, chunking, logging/resume, safety rail.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — why it's built this way, including
  designs that were tried and reversed.

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

### `validate` — check a project's Sheet, or an offline CSV, no writes

`--project` is required (it looks up the project's block in
`projects_registry.json`). By default `validate` reads the project's Sheet
live over the Google Sheets API (its test Sheet, unless `--live` is passed);
pass `--csv` to validate an offline CSV export instead — the CSV path is
unchanged from before this integration and still needs `--files-dir`.

```bash
# read the project's Sheet
python ia_bulk.py validate --project sarahsoldphotos

# validate an offline CSV instead
python ia_bulk.py validate --project sarahsoldphotos --csv items.csv --files-dir ./photos
```

Note: the full Sheet-workflow write-up (registry fields, Google Cloud
prerequisites, the reserve/upload/confirm protocol) is still to be documented
in a later task; this is a minimal correction so the command above stays
accurate. See `docs/DECISIONS.md` ("The Sheet is read live") in the
meantime.

Sheet path: checks the header for colliding or empty field names and any data
row longer than the header, injects `mediatype` from the registry, resolves
each row's file against `files_dir` using `file_template` (an exact filename
match first, then a case-insensitive match ignoring extension — two matching
candidates is a failure naming both, never a silent pick), then prints a
pass/fail report per row, a receipt of which fields will upload, a lifecycle
summary (rows ready to upload / already uploaded / reserved but unconfirmed),
and advisory suggestions for renaming a column to a standard IA field name.
Every column the tool itself writes is `ia_`-prefixed (`ia_identifier`,
`ia_identifier_bib`, `ia_uploaded`, `ia_url`); a blank `ia_identifier` is
normal for a new row, not an error — it's assigned by `upload` later. The
Sheet's own `identifier` column (if it has one) is ordinary donor metadata,
untouched by this tool.

CSV path: checks the header once —
- no column with leading/trailing whitespace, and no duplicate columns —
  header text becomes the IA metadata field name verbatim
- no case variant of a column the script reads by name (`identifier`, `file`,
  `mediatype`, `title`, `date`) — a `Date` column is silently treated as
  unrelated pass-through metadata while `date` stays empty

Then, per row:
- the row has exactly as many fields as the header — a mismatch means the two
  disagree about column positions, so values upload under the wrong field names
- `file` exists on disk (resolved against `--files-dir`)
- `identifier` is unique in the CSV and matches the
  `COLLECTIONKEY-PROJECTID-NUMBER` scheme (lowercase, 5-digit zero-padded
  NUMBER), with the prefix registered in `projects_registry.json`
- `mediatype`, `title` are present and non-empty; `date` is optional —
  `upload` fills a blank `date` with `[n.d.]` rather than omitting it

Prints a pass/fail report per row (header problems are reported as row 1) and
exits non-zero if anything fails. Always run this before `upload`.

### `upload` — upload a validated CSV

`--project` is required here too (registry lookup), but this command still
only reads from a CSV — it does not yet read or write the Sheet.

```bash
python ia_bulk.py upload items.csv --project sarahsoldphotos --files-dir ./photos
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

`--project` is required here too, for the same reason as `upload`.

```bash
python ia_bulk.py sync-metadata updates.csv --project sarahsoldphotos
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
python ia_bulk.py upload items.csv --project sarahsoldphotos --live --collection lcps
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
running this tool. This is a deliberate manual step, not something this
CLI automates.

`validate` catches the structural failures this creates — ragged rows, stray
whitespace and duplicates in the header, and case variants of the columns the
script reads by name. It cannot catch a *misspelled* header or a value entered
in the wrong Sheet column, since neither is distinguishable from an intentional
one. Follow [`docs/CSV-PREPARATION.md`](docs/CSV-PREPARATION.md) and eyeball a
test upload before going live.

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
