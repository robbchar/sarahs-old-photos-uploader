# CSV Preparation

**This is the offline `--csv` procedure, not the required first step.** By
default, `validate`/`upload` read a project's Google Sheet directly over the
Sheets API — there is no export to prepare on that path. The traps documented
below (an unquoted comma splitting a header, stray whitespace, a `Date`/`date`
mismatch) are artifacts of CSV *parsing*, not of the data: the Sheets API
returns cells as a grid, so a header containing a comma is just a header
containing a comma there. See
[`DECISIONS.md`](DECISIONS.md#the-sheet-is-read-live-the-csv-becomes-the-offline-path).
Read this document only if you are deliberately validating or uploading from
a hand-prepared CSV export instead of the live Sheet — offline work, or a
dry run.

Turning a raw Sheet export into a CSV this tool can safely consume. **This is
a manual step by design** — `ia_bulk.py` does not transform the export, and
it is the single most error-prone part of *this offline path*, because most
of the ways it goes wrong do not fail loudly.

## Required schema

`validate` / `upload` require these four columns, spelled exactly like this,
**lowercase**:

| Column | Required | Notes |
|---|---|---|
| `identifier` | yes | Real permanent identifier, `COLLECTIONKEY-PROJECTID-NNNNN`. Never a `zztest-` value. |
| `file` | yes | Filename, optionally with a relative subpath, resolved against `--files-dir`. |
| `mediatype` | yes | `image` for photos. **Not in the Sheet export — you add it.** Cannot be changed after upload. |
| `title` | yes | Non-empty. |

Every other column passes through untouched as an IA metadata field, using the
header text as the field name. `date` is optional; a blank one becomes `[n.d.]`.

`sync-metadata` needs only `identifier` plus whichever columns changed.

## Traps in the real export

These are not hypothetical. All four were present in `data/upload.csv`, the
prepared file in the repo, and were confirmed by running the tool against it.
They have since been corrected there, and **`validate` now rejects traps 1–3
rather than passing them through** — but the export still arrives in this
shape, so you will meet them again on the next batch.

### 1. An unquoted comma in a header silently shifts every later column

The export's header row contains:

```
...,Subject Terms (Controlled Vocab),Names (Last, First M.),Place ,Description,...
```

`Names (Last, First M.)` is **not quoted**, so the CSV parser reads it as two
columns — `Names (Last` and ` First M.)`. The header row ends up one field
longer than every data row, and **every column from that point on is
mislabeled by one position.**

In `data/upload.csv` this meant `Photographs` (a Genre / Form value) uploaded
as `Description`, `Sara Meyer` (the donor) as `Photographer / Studio`, and the
address `600 Marine Dr. (1960)` as `Construction Date` — every one of them a
single column out of place.

`validate` now catches this: `check_row_shape()` compares each row's field
count against the header's and fails the row with
`row has fewer fields than the header (missing: ...)`.

**Fix:** quote any header containing a comma (`"Names (Last, First M.)"`), or
rename it to drop the comma.

### 2. `Date` and `date` are two different fields

The export capitalizes headers. `upload_row` reads the lowercase `date` key, so
a CSV with a `Date` column produces **both**:

- `Date` → `1958` (passed through as an arbitrary metadata field)
- `date` → `[n.d.]` (the undated placeholder, because lowercase `date` is empty)

The item ends up claiming it has no date while also carrying the real one under
a non-standard field.

`validate` now rejects a case variant of any column the script reads by name
(`identifier`, `file`, `mediatype`, `title`, `date`). **Fix:** rename `Date` to
`date` during preparation.

### 3. Headers with stray whitespace become fields with stray whitespace

`Place ` (trailing space) uploads a metadata field literally named `Place `.
`validate` now rejects this. **Fix:** strip whitespace from every header.

### 4. Typos in headers become typos on the item, permanently

Header text is the IA field name; a typo ships as-is across the whole batch and
has to be cleaned up later with `sync-metadata`. `Architectura Style` was in the
header and has been corrected to `Architectural Style`.

**No tool can catch this** — a misspelled field name is indistinguishable from
an intentional one. Proofread the header row once, carefully; it costs a minute
and saves a correction run over 10,000 items. The same applies to typos in
*values* (`Commerical Buildings` is still in the data). Those belong to the
Sheet, so fix them there rather than in the export, or they come back next time.

## Preparation checklist

`validate` enforces everything except the last two, so run it first and work
through whatever it reports:

- [ ] Re-export from the Sheet (do not reuse an old file)
- [ ] Rename to lowercase: `identifier`, `file`, `mediatype`, `title`, `date`
- [ ] Add the `mediatype` column (`image`) — it is not in the export
- [ ] Quote or rename any header containing a comma
- [ ] Strip leading/trailing whitespace from all headers
- [ ] `python ia_bulk.py validate <csv> --files-dir <dir>` exits 0
- [ ] **Proofread header spellings** — not checkable by tool
- [ ] **Test-upload and eyeball one item's metadata in a browser** before
      `--live` — the only check that confirms values landed in the fields you
      meant, rather than merely in *some* consistent set of fields

## Why this is manual

Auto-transforming the export would mean guessing at header intent, and a wrong
guess writes permanent metadata. A human confirming the mapping once per batch
is cheaper than un-picking 10,000 mislabeled items. Automating the *export*
step itself was raised early on and was superseded, not merely deferred:
`validate`/`upload` now read the Sheet directly by default (the note at the
top of this document), which is what actually removed the export step for the
normal path — see
[`DECISIONS.md`](DECISIONS.md#the-sheet-is-read-live-the-csv-becomes-the-offline-path).
This document, and the manual transform it describes, still applies whenever
you deliberately choose the offline `--csv` path.
