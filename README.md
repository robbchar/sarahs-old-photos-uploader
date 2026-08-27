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
each row's file against `files_dir` using `file_template` (three passes: an
exact filename match, then a case-insensitive match against the full name, then
the same ignoring a trailing extension — two matching candidates is a failure
naming both, never a silent pick; `files_dir` is a hard boundary, so a cell that
resolves outside it is refused, as is a blank folder cell), then prints a
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

**The 5,000/day cap is enforced.** Internet Archive allows 5,000 items per
account per day. A run planning more than that is refused before anything is
uploaded, naming the fix — `--limit 5000` on the Sheet path, splitting the
file on `--csv`. It refuses rather than silently capping, because a run that
quietly stopped short would read as a complete one. `--allow-over-daily-cap`
overrides it, and is only correct if you know IA has raised this account's
cap. The refusal applies in test mode too: a rehearsal uploads through the
same account and spends the same quota.

Other behavior, both paths:

- The Sheet path processes rows in chunks of 500 by default (Internet
  Archive's per-run batch limit), overridable via `--chunk-size`
- Uploads each row via the `internetarchive` Python library (not the `ia`
  CLI), so per-row success/failure is captured directly
- Writes a timestamped JSONL log to `logs/upload-<timestamp>.jsonl`, one
  line per row: `{identifier, file, status, error, uploaded_as, live, timestamp}`.
  Timestamps are ISO-8601 UTC with an explicit `Z`, as is the `ia_uploaded`
  cell written back to the Sheet
- `--collection` and `--files-dir` are `--csv`-path overrides only; on the
  Sheet path they come from the project's registry entry, and passing them
  is an error rather than a silently ignored flag
- `--resume-from <log>` (`--csv` only) skips identifiers already marked
  `"success"` or `"unchanged"` **in the same mode** (test vs. `--live`) as
  this run, and still writes a complete log of its own. The Sheet path needs
  no such flag: `ia_uploaded` is already the record of what is done

### `sync-metadata` — update metadata on already-uploaded items

The Sheet is the correction. Fix a description in the Sheet, run this, and it
is on the site:

```bash
python ia_bulk.py sync-metadata --project sarasoldphotos --dry-run
python ia_bulk.py sync-metadata --project sarasoldphotos
```

It reads the Sheet live, takes every row marked uploaded (`ia_uploaded` set),
and sends that row's current metadata to the item named in its `ia_url` cell.
Nothing has to be re-derived: `ia_url` is what `upload`'s confirm write
recorded, so it already names the exact item — including the per-run
`zztest-` stamp in test mode, even when different rows were uploaded by
different runs.

The fields sent are the same ones `upload` sends (`sheet_metadata_fields()`),
so the two commands cannot disagree about what a row means. Tool-owned `ia_`
columns, `(LCPS Internal)` columns, and the generated `mediatype`/
`collection` are all excluded — Internet Archive will not change an item's
mediatype after upload anyway.

Every uploaded row is sent every run. Internet Archive answers *no changes to
`_meta.xml`* for an item that already matches, which is counted as
`unchanged` rather than an error, so a full sync is idempotent and needs no
change tracking.

A blank cell means **leave this field alone**, not "delete it" — so an
accidental cell clear can never strip metadata from a permanent public item.
To actually delete a field, put the literal `REMOVE_TAG` in that cell (the
same sentinel the official `ia` CLI's `--modify field:REMOVE_TAG` uses).

`--live` reads the real Sheet and targets the real, permanent items. The
command refuses to send a live correction to a `zztest-` item, or a rehearsal
correction to a real one.

#### The `--csv` fallback

```bash
python ia_bulk.py sync-metadata --csv updates.csv --project sarasoldphotos --live
python ia_bulk.py sync-metadata --csv updates.csv --project sarasoldphotos --from-log logs/upload-20260823T161331Z.jsonl
```

Offline only, and a hand-prepared CSV rather than the Sheet. Needs an
`identifier` column plus whichever columns changed.

**`--from-log` is required on this path in test mode.** A test item is named
`zztest-<stamp>-<identifier>`, and the stamp is unique to the run that
created it, so the CSV alone cannot say which items to correct — deriving
the target from this run's stamp would name an item that has never existed.
The upload log's `uploaded_as` field is the only record of that mapping, so
`sync-metadata` reads it rather than recomputing. A row the log does not
record as uploaded is an error, never a silent fall back. Live identifiers
are unstamped, so the flag is optional with `--live`.

It is distinct from `--resume-from`: that says which rows to *skip*, this
says where the rows that remain should be *sent*.

Same logging/safety-rail behavior as `upload`, but only requires
an `identifier` column plus whichever metadata columns changed — no
`file`/`mediatype`/`title`/`date` needed. A blank cell means "leave this
field alone"; to actually delete an existing field on the IA item, put the
literal value `REMOVE_TAG` in that cell (the same sentinel the official
`ia` CLI's `--modify field:REMOVE_TAG` uses).

### `reconcile-files` — correct a filename cell that doesn't match the drive

```bash
python ia_bulk.py reconcile-files --project sarasoldphotos --dry-run
python ia_bulk.py reconcile-files --project sarasoldphotos
```

It reads the Sheet live and resolves every row's file the same way
`validate`/`upload` do. Rows that resolve are left alone entirely — never
prompted about, never written to. So are rows nobody has catalogued yet:
a row whose filename cell is blank asserted no file, so there is nothing
to be wrong and nothing to propose — they are counted in one line
(`2,914 rows not yet catalogued - no filename to reconcile, skipped`) and
never prompted about, the same not-ready-versus-broken split
[`docs/decisions/READINESS.md`](docs/decisions/READINESS.md) draws for
`validate` and `upload`. For each row that named a file and does **not**
resolve,
it looks for a single best match among the files still unclaimed in that
row's own folder and asks before touching the Sheet:

```
row 7  'CD 1 01 53 34 2 Finnis Meat Market.jpg'  does not resolve in 'SOP CD 1'
       proposed: 'CD 1 01 53 34 2 Finnish Meat Market.jpg'   (edit distance 1)
       [y] accept   [n] not this one   [e] type it   [l] list unclaimed   [q] stop
       >
```

Prompt keys: `[y]` accept the proposal (only offered when there is one);
`[n]` leave this row alone and move on; `[e]` type a filename yourself — it
is resolved against disk the same way a proposal is, so a typo here is
caught and re-asked rather than written; `[l]` list every unclaimed file in
the row's folder; `[q]` stop the run — whatever was already accepted before
`[q]` has already been written, so it's safe to rerun later and pick up
where this left off. When more than one file matches equally well,
reconciliation asks nothing and leaves the row alone, naming every match on
screen — the same "never guess between two" rule `resolve_file()` follows
everywhere else; see
[`docs/decisions/RECONCILIATION.md`](docs/decisions/RECONCILIATION.md).

A header defect — two Sheet columns whose names normalize to the same IA
field, or one that normalizes to nothing — stops the run before anything is
proposed, because it corrupts every row identically and would send the
correction to a different column than the one the value was read from. A
single *data* row longer than the header is skipped and named instead; the
rest of the run proceeds.

It writes only the `file_name` column — whichever Sheet column
`file_template`'s last segment names — never `folder_on_lacie_drive`, and
never anything else on the row. A wrong folder cell is left for a human;
reconciliation only ever searches inside the folder a row already names.

A file accepted for one row is removed from the pool offered to every row
still to come in the same run, so two misspelled rows in one folder can
never both be pointed at the same photograph.

Before each batch of accepted corrections is written, the Sheet is read
again and any correction whose row no longer holds the filename it was
matched against is dropped and reported rather than written — a session can
run for an hour on a Sheet other volunteers are editing, and one row
inserted in that time would otherwise shift every later write onto the
wrong photograph.

Exits `0` whether or not every row ends up resolved — rows left for later
are the normal state of a ~10,000-row backlog worked over many sessions,
not a failure; see
[`docs/decisions/RECONCILIATION.md`](docs/decisions/RECONCILIATION.md#exit-code-is-0-while-work-remains).
Unless `--dry-run` is passed, it writes a timestamped log to `--log-dir`
(default `logs/`), one line per row considered, recording what was
proposed and what was decided.

Which files on disk even count as photographs — and so can ever be
proposed — is the project's `photo_extensions` in
`projects_registry.json`: optional, defaulting to `.jpg`/`.jpeg`/`.tif`/
`.tiff`/`.png` when a project doesn't set one. That default is what keeps
the contact-sheet PDFs already sitting alongside the photos on the drive
from ever being offered as a match.

`--live` reads and writes the real Sheet instead of the test one, same as
every other command. There is no `--csv` path — reconciliation only makes
sense against the live Sheet it is correcting.

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
[`docs/DECISIONS.md`](docs/decisions/IDENTIFIERS.md#test-identifiers-carry-a-per-run-stamp).
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
[`docs/DECISIONS.md`](docs/decisions/SHEET-PROTOCOL.md#the-sheet-is-read-live-the-csv-becomes-the-offline-path).
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
