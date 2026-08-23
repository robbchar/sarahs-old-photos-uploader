# IA Bulk Upload CLI

A small Python CLI for validating, uploading, and syncing metadata for
Internet Archive items in bulk, reading a project's Google Sheet directly
and live over the Sheets API. Built for the Lower Columbia Preservation
Society's (LCPS) Astoria historical photo archive, but kept generic to "a
project" so a second LCPS project can reuse the same pipeline. A
hand-prepared CSV export remains a deliberate offline/dry-run fallback for
`validate` and `upload` — see "Offline `--csv` path" below.

## Docs

- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — runbook: how to run a batch,
  pre-live checklist, resuming, batch limits. **Start here to run something.**
- [`docs/CSV-PREPARATION.md`](docs/CSV-PREPARATION.md) — the offline `--csv`
  path only: turning a raw Sheet export into the required schema, and the
  traps that don't fail loudly.
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

## Project registry

Everything technical about a project — the target IA collection, where its
files live, how a row's file path is built, and which fields a human must
fill in before a row can upload — lives in `projects_registry.json`, never on
the command line. See [`docs/DECISIONS.md`](docs/DECISIONS.md), "Technical
configuration lives in the registry, not the command line".

```json
{
  "collection_key": "lcps",
  "projects": {
    "photosexample": {
      "mediatype": "image",
      "ia_collection": "lcpsdigitalcollection",
      "sheet_id": "...",
      "test_sheet_id": "...",
      "sheet_tab": "TestSheet",
      "files_dir": "./data",
      "file_template": "{folder_on_lacie_drive}/{file_name}",
      "required_for_upload": ["title", "theme"]
    }
  }
}
```

`required_for_upload` names the normalized columns (not raw Sheet header
text) a human must fill in before a row is ready to upload — a blank one
marks the row not-ready rather than invalid, and a typo in this list is a
hard startup error rather than a silent no-op. See
[`docs/DECISIONS.md`](docs/DECISIONS.md), "A blank cell is not an error".

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
python ia_bulk.py validate --project sarasoldphotos

# validate an offline CSV instead
python ia_bulk.py validate --project sarasoldphotos --csv items.csv --files-dir ./photos
```

For the Google Cloud setup this requires (OAuth consent screen, the
`@lcpsociety.org` account requirement, the one-time browser consent) see
[`docs/OPERATIONS.md`](docs/OPERATIONS.md), "Google Cloud prerequisites"; for
the reserve/upload/confirm protocol and registry fields in full see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and "Project registry" above.

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
python ia_bulk.py upload --project sarasoldphotos

# same, but record the minted identifiers in the TEST Sheet
python ia_bulk.py upload --project sarasoldphotos --write-identifier

# see what it would do without doing any of it
python ia_bulk.py upload --project sarasoldphotos --dry-run

# upload from an offline CSV instead
python ia_bulk.py upload --csv items.csv --project sarasoldphotos --files-dir ./photos
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

**A rate limit stops the run, not just one row (Sheet path only).** If an
upload fails with what looks like Internet Archive's rate limit, the run
stops rather than grinding through the rest of the batch as unexplained
failures — everything already uploaded that run, in this chunk or an earlier
one, is still confirmed in the Sheet first. This detector is best-effort: no
`--live` run has ever happened, so no real rate-limit response has ever been
captured, and it may not fire on one — see `docs/DECISIONS.md`, "Still open".
`--limit` (below) is the operator-controlled fallback either way.

**`--limit` and `--chunk-size` (Sheet path only).**

```bash
# upload at most 100 items this run, in the default batches of 500
python ia_bulk.py upload --project sarasoldphotos --write-identifier --limit 100

# 10 items total, in batches of 3 - not 10 batches of 3
python ia_bulk.py upload --project sarasoldphotos --write-identifier --limit 10 --chunk-size 3
```

`--limit` counts *planned* upload targets — rows that are valid, ready
(nothing required left blank), and not already done — never raw Sheet rows
scanned. On a Sheet with 2,900 uncatalogued rows and 150 ready ones,
`--limit 100` uploads 100 of the 150 ready rows, not the first 100 rows
read. `--chunk-size` overrides the reserve/upload/confirm batch size
(default 500, Internet Archive's per-run cap) for the rows `--limit` leaves;
the two combine literally, as shown above. Both are recorded in the
`run_header` record every Sheet-path log starts with (see
`docs/ARCHITECTURE.md`) and rejected on `--csv`, the same way
`--write-identifier`/`--dry-run` are rejected on the Sheet path.

Other behavior, both paths:

- Processes rows in chunks of 500 by default (Internet Archive's per-run
  batch limit), overridable on the Sheet path via `--chunk-size`
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
python ia_bulk.py sync-metadata updates.csv --project sarasoldphotos
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
Instead, `upload` and `sync-metadata` automatically prepend
`zztest-<run's stamp>-` to the real identifier for every network call (e.g.
`lcps-astoriaphotos-00001` becomes
`zztest-20260819t144907-lcps-astoriaphotos-00001`) unless `--live` is
passed. The stamp is unique per invocation, so a rehearsal never collides
with a previous rehearsal's items — see
[`docs/DECISIONS.md`](docs/DECISIONS.md#test-identifiers-carry-a-per-run-stamp).
Pass `--live` to target the real collection with the real identifier as-is,
with no stamp — do this deliberately, never as a default.

```bash
python ia_bulk.py upload --project sarasoldphotos --live
```

**Before any `--live` run**, confirm both of these by hand — neither is
validated automatically against Internet Archive (see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#known-gaps)):
- `projects_registry.json`'s `collection_key` — still unconfirmed against how
  LCPS actually names its collection.
- the project's `ia_collection` in `projects_registry.json` — for
  `sarasoldphotos`, already confirmed by hand against archive.org on
  2026-08-22 (see [`docs/DECISIONS.md`](docs/DECISIONS.md#still-open)); a
  second project's registry entry would need the same one-time check.

`--dry-run` is the cheapest way to check the second one: it prints every
identifier it would mint and every cell it would write, and touches nothing.

## Offline `--csv` path

By default `validate`/`upload` read the project's Sheet directly, live, over
the Google Sheets API — see "Commands" above and
[`docs/DECISIONS.md`](docs/DECISIONS.md#the-sheet-is-read-live-the-csv-becomes-the-offline-path).
Nothing needs preparing on that path; there is no export step and nothing
local to go stale.

`--csv` is a deliberate offline/dry-run fallback for validating or uploading
from a hand-prepared CSV instead. A raw Sheet export does not match the
schema `validate --csv`/`upload --csv` require (capitalized headers, no
`mediatype` column) and must be transformed by hand into the required
lowercase schema first — a deliberate manual step, not something this CLI
automates. `validate --csv` catches the structural failures this creates —
ragged rows, stray whitespace and duplicates in the header, and case variants
of the columns the script reads by name. It cannot catch a *misspelled*
header or a value entered in the wrong column, since neither is
distinguishable from an intentional one. Follow
[`docs/CSV-PREPARATION.md`](docs/CSV-PREPARATION.md) and eyeball a test
upload before going live.

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
