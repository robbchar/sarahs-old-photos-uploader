# Operations Runbook

How to actually run a batch, from Google Sheet to Internet Archive. Read
[`ARCHITECTURE.md`](ARCHITECTURE.md) first if you want to know *why* the tool
is shaped this way; read [`KNOWN-ISSUES.md`](KNOWN-ISSUES.md) before you trust
a run's output.

**Current status: no `--live` run has ever happened.** Every log in `logs/` is
a `test_collection` run (`"live": false`). The first real run is still ahead,
and several things below have never been exercised against production.

## The pipeline

```
Google Sheet  →  raw CSV export  →  hand-prepared CSV  →  validate  →  upload  →  sync-metadata
                                    (see CSV-PREPARATION.md)         (test)     (corrections)
                                                                        ↓
                                                                   upload --live
```

The Sheet is the source of truth. A local CSV is a snapshot and goes stale the
moment someone edits the Sheet — re-export deliberately before every run
rather than reusing yesterday's file.

## 1. Prepare the CSV

The raw Sheet export **does not** match the schema this tool requires and will
silently produce wrong metadata if you skip this step. Follow
[`CSV-PREPARATION.md`](CSV-PREPARATION.md) — this is the highest-risk step in
the whole pipeline.

## 2. Validate (offline, no network)

```bash
python ia_bulk.py validate data/upload.csv --files-dir data
```

Exits `0` if every row passes, `1` otherwise. Checks per row: required columns
present (`identifier`, `file`, `mediatype`, `title`), identifier matches
`COLLECTIONKEY-PROJECTID-NUMBER` and is registered in
`projects_registry.json`, identifier is unique within the CSV, and the `file`
resolves to a real file under `--files-dir`.

**What `validate` does not check** — it looks at four columns and the
filesystem, nothing else. A CSV with every metadata column shifted into the
wrong field still passes. See [`KNOWN-ISSUES.md`](KNOWN-ISSUES.md#1).

## 3. Test run

```bash
# against the project's test Sheet (the normal path)
python ia_bulk.py upload --project sarahsoldphotos

# ...and again, recording the minted identifiers in the test Sheet
python ia_bulk.py upload --project sarahsoldphotos --write-identifier

# against an offline CSV
python ia_bulk.py upload --csv data/upload.csv --project sarahsoldphotos --files-dir data
```

On the Sheet path, run it once without `--write-identifier` first: that mode
issues zero writes to the Sheet, so it is a rehearsal you can repeat freely.
`--dry-run` goes further and uploads nothing at all, printing the identifiers
it would mint and the cells it would write.

With no `--live`, the tool targets IA's `test_collection` sandbox and prepends
`zztest-` to each identifier before every network call. The CSV keeps its
real, permanent identifiers — never hand-write a `zztest-` identifier.

Test items land at `https://archive.org/details/zztest-<identifier>` and
auto-expire after roughly 30 days. Spot-check a few in a browser and confirm
the metadata fields are the ones you meant, with the values you meant.

## 4. Live run

```bash
python ia_bulk.py upload --project sarahsoldphotos --live

# ...or from an offline CSV, where --live must name the collection itself
python ia_bulk.py upload --csv data/upload.csv --project sarahsoldphotos --files-dir data --live --collection lcpsdigitalcollection
```

### Pre-live checklist

Nothing on this list is validated automatically. A wrong value here puts real
files in the wrong place under a permanent identifier.

- [ ] `projects_registry.json` → `collection_key` matches LCPS's real IA
      collection identifier. Currently `"lcps"`, **never confirmed against IA.**
- [ ] `--collection` matches that same real collection. Defaults to `"lcps"`,
      and **nothing validates it** — a wrong value pushes real files to the
      wrong collection and reports success.
- [ ] The `ia` CLI is authenticated as `admin@lcpsociety.org` (`ia whoami`).
- [ ] The CSV was re-exported from the Sheet today, not reused.
- [ ] A test run of this exact CSV succeeded and was eyeballed in a browser.
- [ ] The batch is under IA's daily limit — **see "Pacing" below, the tool does
      not enforce this.**

Identifiers are permanent. An item uploaded under the wrong identifier cannot
be renamed, only darkened by IA staff on request.

## 5. Corrections

```bash
python ia_bulk.py sync-metadata data/update-metadata.csv --live
```

Decoupled from upload and safe to re-run. Needs only `identifier` plus the
columns that changed. Blank cell = leave alone; literal `REMOVE_TAG` = delete
that field. `noindex` cannot be changed this way —
see [`KNOWN-ISSUES.md`](KNOWN-ISSUES.md#3).

## Pacing and batch limits

IA's limits are **500 items per upload run** and **5,000 per day**.

`chunk_rows()` groups rows into 500s, but the loop just walks through them —
there is no sleep, no checkpoint, and no daily counter. **Batch sizing is
entirely manual.** With ~10,000 photos this matters: split the CSV into
day-sized files yourself, or run it in sittings and rely on `--resume-from`.

## Resuming a failed run

Every run writes `logs/<command>-<timestamp>.jsonl`, one line per row:

```json
{"identifier": "...", "file": "...", "status": "success|unchanged|failure|unconfirmed",
 "error": null, "uploaded_as": "...", "live": false, "timestamp": "..."}
```

`identifier` is the real, permanent identifier; `uploaded_as` is what was
actually sent to IA. `unconfirmed` is Sheet-path-only and means the item
reached Internet Archive but the Sheet could not be updated, because the row
no longer held the identifier the run reserved — someone edited the Sheet
mid-run. Rerun once it has settled; the row is picked up as reserved-but-
unconfirmed and retried under the same identifier.

On the Sheet path, `ia_uploaded` is the record of what is done, so a rerun
resumes by itself and `--resume-from` is refused there. On the `--csv` path,
to pick up after failures:

```bash
python ia_bulk.py upload --csv data/upload.csv --project sarahsoldphotos --files-dir data --resume-from logs/upload-20260712T125326.jsonl
```

Rows marked `success` or `unchanged` **in the same mode** are skipped. Test-mode
successes never skip a `--live` row — a sandbox success says nothing about
whether the real item exists. The new run writes its own complete log, so the
newest log is always the full picture.

Re-uploading is also cheap on its own: `upload_row` passes `checksum=True`, so
a file already present with a matching MD5 is skipped rather than re-uploaded
and re-derived.

### Transient network failures are expected

Real test runs hit `SSLEOFError` / `MaxRetryError` against
`s3.us.archive.org`. There is no retry or backoff in the tool — the row is
logged as `failure` and the run moves on. This is normal for a long run; use
`--resume-from` and expect to run more than once.

## Reading a run

```bash
# how many of each status in the newest log
grep -o '"status": "[a-z]*"' logs/upload-*.jsonl | sort | uniq -c

# just the failures, with their errors
grep '"status": "failure"' logs/upload-20260712T125326.jsonl
```

## Development

```bash
python -m pytest test_ia_bulk.py -v
python -m ruff check .
python -m pyright ia_bulk.py test_ia_bulk.py
```

Tests are pure-offline — the `internetarchive` calls are monkeypatched, so the
suite never touches the network.
