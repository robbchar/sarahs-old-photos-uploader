# CSV Preparation

Turning the raw LCPS Google Sheet export into a CSV this tool can safely
consume. **This is a manual step by design** — `ia_bulk.py` does not transform
the export, and it is the single most error-prone part of the pipeline, because
most of the ways it goes wrong do not fail loudly.

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

These are not hypothetical. All four are present in `data/upload.csv`, the
prepared file currently in the repo, and were confirmed by running the tool
against it.

### 1. An unquoted comma in a header silently shifts every later column

The export's header row contains:

```
...,Subject Terms (Controlled Vocab),Names (Last, First M.),Place ,Description,...
```

`Names (Last, First M.)` is **not quoted**, so the CSV parser reads it as two
columns — `Names (Last` and ` First M.)`. The header row ends up one field
longer than every data row, and **every column from that point on is
mislabeled by one position.**

In `data/upload.csv` this means the value `Photographs` (a Genre / Form value)
is uploaded as `Description`, and the address `600 Marine Dr. (1960)` is
uploaded as `Construction Date`.

`validate` reports `1/1 rows passed` on this file. It only inspects the four
required columns, which all sit *before* the broken header, so it cannot see
the problem.

**Fix:** quote any header containing a comma (`"Names (Last, First M.)"`), or
rename it to drop the comma. **Check:** header field count must equal data row
field count.

```bash
python -c "import csv,sys; r=list(csv.reader(open(sys.argv[1],encoding='utf-8-sig'))); print('header',len(r[0])); print({len(x) for x in r[1:]})" data/upload.csv
```

If the header count and the row counts differ, stop and fix the CSV.

### 2. `Date` and `date` are two different fields

The export capitalizes headers. `upload_row` reads the lowercase `date` key, so
a CSV with a `Date` column produces **both**:

- `Date` → `1958` (passed through as an arbitrary metadata field)
- `date` → `[n.d.]` (the undated placeholder, because lowercase `date` is empty)

The item ends up claiming it has no date while also carrying the real one under
a non-standard field. **Fix:** rename `Date` to `date` during preparation.

### 3. Headers with stray whitespace become fields with stray whitespace

`Place ` (trailing space) uploads a metadata field literally named `Place `.
**Fix:** strip whitespace from every header.

### 4. Typos in headers become typos on the item, permanently

`Architectura Style` and `Commerical Buildings` are in the current data. Header
text is the IA field name; a typo ships as-is across the whole batch and has to
be cleaned up later with `sync-metadata`. Proofread the header row once,
carefully — it costs a minute and saves a correction run over 10,000 items.

## Preparation checklist

- [ ] Re-export from the Sheet (do not reuse an old file)
- [ ] Header field count == every data row's field count
- [ ] Rename to lowercase: `identifier`, `file`, `mediatype`, `title`, `date`
- [ ] Add the `mediatype` column (`image`) — it is not in the export
- [ ] Quote or rename any header containing a comma
- [ ] Strip leading/trailing whitespace from all headers
- [ ] Proofread header spellings
- [ ] No row has more fields than the header (extra fields crash the run —
      see [`KNOWN-ISSUES.md`](KNOWN-ISSUES.md#2))
- [ ] `python ia_bulk.py validate <csv> --files-dir <dir>` exits 0
- [ ] Test-upload and eyeball one item's metadata in a browser before `--live`

## Why this is manual

Auto-transforming the export would mean guessing at header intent, and a wrong
guess writes permanent metadata. A human confirming the mapping once per batch
is cheaper than un-picking 10,000 mislabeled items. There is a
[standing idea](../README.md) to automate the *export* step (fetching the CSV
from the Sheet); that is separate from — and does not remove the need for —
this schema check.
