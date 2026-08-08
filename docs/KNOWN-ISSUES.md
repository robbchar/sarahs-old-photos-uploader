# Known Issues

Verified against the code and the real data in `data/` as of 2026-08-08. Each
entry was reproduced, not inferred. Ordered by how much damage it can do to a
`--live` run.

These are the open ones. Issues fixed since this file was written are recorded
at the bottom under [Fixed](#fixed), so the reasoning survives.

## 1. `noindex` cannot be changed by `sync-metadata`

**Severity: low — fails loudly.**

IA treats `noindex` as read-only after upload:

```
400 {"success":false,"error":"Can't modify read-only field 'noindex'"}
```

Recorded against items `00001`–`00005` in the 2026-07-08 sync logs. `noindex`
*can* be set at upload time (`data/upload.csv` sets it to `true`), but it must
be right the first time.

**Implication:** decide the `noindex` policy for the collection before the live
upload, not after.

## 2. Batch limits are documented but not enforced

**Severity: medium for a 10,000-item batch.**

`chunk_rows()` groups rows into 500s to match IA's per-run limit, but the loop
has no pacing, no per-chunk checkpoint, and no counter against the 5,000/day
limit. Chunking is structural only. This was flagged and accepted during the
original build review.

**Mitigation today:** size the CSVs by hand; see
[`OPERATIONS.md`](OPERATIONS.md#pacing-and-batch-limits).

## 3. No retry on transient network failures

**Severity: low — expected, and `--resume-from` covers it.**

Uploads to `s3.us.archive.org` intermittently fail with
`SSLEOFError` / `MaxRetryError`. There is no backoff or retry; the row is
logged as `failure` and skipped. Over 10,000 items this will happen regularly.

**Mitigation today:** re-run with `--resume-from`. `checksum=True` means
already-uploaded files are skipped rather than re-sent.

## 4. `--collection` is unvalidated on `--live`

**Severity: high if wrong, but requires operator error.**

`--collection` defaults to `"lcps"` and is used as-is. Nothing checks it
against `projects_registry.json` or against IA. A wrong value pushes real files
into the wrong collection and reports success. The default was deliberately
left in place during review as a documentation warning rather than a behavior
change.

Relatedly, `projects_registry.json`'s `collection_key` (`"lcps"`) has **never
been confirmed** against LCPS's actual IA collection. A wrong value there is
harmless — validation simply rejects every identifier — but it must be right
before real uploads can pass.

**Possible fix:** cross-check `--collection` against the registry's
`collection_key` and refuse to run `--live` when they disagree.

## 5. `--files-dir` does not constrain path resolution

**Severity: low — trusted-input tool.**

`Path(files_dir) / row["file"]` will happily resolve `../` or an absolute path
outside the intended directory. Accepted during review: the CSV is authored by
the same person running the tool.

## Fixed

### `validate` passed CSVs whose metadata was silently misaligned

*Fixed 2026-08-08.* `validate` inspected four columns and forwarded everything
else to IA unexamined, so a malformed header row shifted every later column by
a position and still reported all rows passing. This was live in the repo's own
`data/upload.csv`: `Photographs` uploaded as `Description`, `Sara Meyer` as
`Photographer / Studio`, and `600 Marine Dr. (1960)` as `Construction Date` —
each one column off, caused by the unquoted comma in `Names (Last, First M.)`
splitting one header cell into two.

`check_row_shape()` now rejects any row whose field count disagrees with the
header (`csv.DictReader` exposes surplus fields under the `None` restkey and
missing ones as `None` values), and `check_header()` rejects headers with
surrounding whitespace, duplicates, and case variants of the columns the script
reads by name. Header problems are reported as row 1. `data/upload.csv` was
corrected in the same change.

### A row with more fields than the header crashed that row

*Fixed 2026-08-08 by the same `check_row_shape()` check.* Surplus fields landed
in a list under the `None` key and `upload_row`'s metadata comprehension called
`.strip()` on it — `AttributeError: 'list' object has no attribute 'strip'`,
visible in three real logs (`upload-20260708T131606`, `-20260708T132619`,
`-20260712T122426`). The mirror case (a *short* row, giving `None` values) had
been papered over by the `(value or "")` guards in `a0944be`, which stopped the
crash without noticing the misalignment behind it. Both are now caught before
any network call.
