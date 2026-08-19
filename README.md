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

### `upload` — mint identifiers, upload, record the result

Reads the project's Sheet by default; `--csv` switches to the offline path.

```bash
# rehearse against the test Sheet: uploads as zztest-…, writes nothing back
python ia_bulk.py upload --project sarahsoldphotos

# same, but record the minted identifiers in the TEST Sheet
python ia_bulk.py upload --project sarahsoldphotos --write-identifier

# see what it would do without doing any of it
python ia_bulk.py upload --project sarahsoldphotos --dry-run

# upload from an offline CSV instead
python ia_bulk.py upload --csv items.csv --project sarahsoldphotos --files-dir ./photos
```

| Mode | Reads | Uploads as | Writes back |
|---|---|---|---|
| default | test Sheet | `zztest-…` → `test_collection` | nothing |
| `--write-identifier` | test Sheet | `zztest-…` → `test_collection` | test Sheet |
| `--live` | real Sheet | real identifier → registry's `ia_collection` | real Sheet, always |
| `--dry-run` | either | nothing | nothing — prints the intended writes |

Per chunk of 500 rows the Sheet path does four things **in this order**:

1. **reserve** — one batch write putting the minted `ia_identifier` in the Sheet
2. **upload** — row by row, to Internet Archive, under that identifier
3. **confirm** — one batch write of `ia_uploaded`, `ia_url` and
   `ia_identifier_bib`, but only for rows whose upload succeeded
4. **log** — per-row outcomes appended to the JSONL log

Reserving first is deliberate. Uploading first would let a crash strand an
item on Internet Archive that the Sheet has no record of, and the next run's
`max+1` would then mint that same number onto a different photograph —
permanently. Reserving first degrades the same crash into an unused gap in the
sequence, which is harmless. Batching matters too: the Sheets API allows 60
writes per minute per user and counts a batch as one request, so ~10,000 rows
cost about 40 requests rather than 10,000.

A row is chosen by what its two tool-owned columns already hold:

| `ia_identifier` | `ia_uploaded` | Action |
|---|---|---|
| blank | blank | mint, reserve, upload |
| set | blank | upload under the **existing** identifier, never re-mint |
| set | set | skip entirely |

The Sheet must already have all four `ia_` columns as headers
(`ia_identifier`, `ia_uploaded`, `ia_url`, `ia_identifier_bib`); `upload`
refuses in every mode until they exist, so a rehearsal never succeeds where
the real run would fail.

**Editing the Sheet while a run is in progress.** Row numbers are positional,
so inserting or deleting a row shifts every row below it — and a run holds row
numbers from the read it started with, which on a full-collection run is hours
before the last chunk writes. Before **every** write, reserve and confirm
alike, the run re-reads the Sheet and checks two things about each target row:
that its `file_template` columns still describe the same photograph (a
fingerprint this tool never writes, so the check cannot pass by verifying its
own earlier write), and that `ia_identifier` is blank or already ours. A
mismatch — including a column inserted or removed — is reported and the write
is withheld, rather than landing on a different photograph. Nothing is lost:
rerun once the Sheet has settled and every unrecorded row is picked up.

If the Sheet becomes unreadable mid-run, or one of the four `ia_` columns is
renamed or deleted, the run stops cleanly with the reason on stderr, still
prints its summary and log path, and exits non-zero.

That said, the check is a safety net, not a licence. It cannot distinguish two
rows whose `file_template` columns are identical — if a shift moves one onto
the other, the guard passes and the identifier and URL are written to the
wrong row (the file itself still uploads correctly, and rows already marked
uploaded cannot be touched). So: **avoid editing the Sheet while a run is in
progress.**

**A failing row is skipped, not fatal.** One unresolvable file in row 9,000
does not block the other 9,999; the failures are listed with their errors, the
valid rows upload, and the command exits non-zero so a partial run is never
mistaken for a clean one. The `--csv` path keeps the opposite behavior — it
refuses to upload anything if any row fails.

Other behavior, both paths:

- Processes rows in chunks of 500 (Internet Archive's per-run batch limit)
- Uploads each row via the `internetarchive` Python library (not the `ia`
  CLI), so per-row success/failure is captured directly
- Writes a timestamped JSONL log to `logs/upload-<timestamp>.jsonl`, one
  line per row: `{identifier, file, status, error, uploaded_as, live, timestamp}`
- `--collection` and `--files-dir` are `--csv`-path overrides only; on the
  Sheet path they come from the project's registry entry, and passing them
  is an error rather than a silently ignored flag
- `--resume-from <log>` (`--csv` only) skips identifiers already marked
  `"success"` or `"unchanged"` **in the same mode** (test vs. `--live`) as
  this run, and still writes a complete log of its own. The Sheet path needs
  no such flag: `ia_uploaded` is already the record of what is done

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

By default every command targets IA's `test_collection` sandbox. The Sheet's
`ia_identifier` column (and the CSV's `identifier` column) always holds the
real, permanent identifier — never author a `zztest-` identifier by hand.
Instead, `upload` and `sync-metadata` automatically prepend `zztest-` to the
real identifier for every network call (e.g. `lcps-astoriaphotos-00001`
becomes `zztest-lcps-astoriaphotos-00001`) unless `--live` is passed. Pass
`--live` to target the real collection with the real identifier as-is — do
this deliberately, never as a default.

```bash
python ia_bulk.py upload --project sarahsoldphotos --live
```

**Before any `--live` run**, confirm both of these by hand — neither is
validated automatically, and both ship as placeholders (see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#known-gaps)):
- `projects_registry.json`'s `collection_key`
- the project's `ia_collection` in `projects_registry.json`

`--dry-run` is the cheapest way to check the second one: it prints every
identifier it would mint and every cell it would write, and touches nothing.

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
