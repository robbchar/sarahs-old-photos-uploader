# Known Issues

Verified against the code and the real data in `data/` as of 2026-08-08. Each
entry was reproduced, not inferred. Ordered by how much damage it can do to a
`--live` run.

Nothing here has been fixed — this is a record for whoever picks the project
up next, not a changelog.

## 1. `validate` passes CSVs whose metadata is silently misaligned

**Severity: high — writes wrong metadata to permanent items.**

`validate_rows` checks four columns (`identifier`, `file`, `mediatype`,
`title`) plus file existence. Every other column is passed through to IA
unexamined. If the header row is malformed — for example an unquoted comma
splitting one header into two — all later columns shift by a position and
upload under the wrong field names.

Reproduced with the repo's own `data/upload.csv`: `validate` reports
`1/1 rows passed`, and the upload attaches `Description: Photographs` and
`Construction Date: 600 Marine Dr. (1960)`, both of which belong to different
columns.

**Mitigation today:** the manual field-count check in
[`CSV-PREPARATION.md`](CSV-PREPARATION.md#1).

**Possible fix:** have `read_rows` reject any CSV where a row's field count
differs from the header's (`csv.DictReader` exposes this as the `None` restkey
and a `None` value for missing fields), and fail `validate` on unexpected
whitespace in headers.

## 2. A row with more fields than the header crashes that row

**Severity: medium — noisy, not silent.**

`csv.DictReader` collects surplus fields into a list under the `None` key.
`upload_row`'s metadata comprehension then calls `.strip()` on that list:

```
AttributeError: 'list' object has no attribute 'strip'
```

This appears in three real logs (`upload-20260708T131606`, `-20260708T132619`,
`-20260712T122426`). The row is caught by `run_rows`, logged as `failure`, and
the run continues — so it costs a row, not a run.

The mirror-image case (a row with *fewer* fields, giving `None` values) was
fixed by the `(value or "")` guards in `a0944be`; the surplus-field case was
not.

**Possible fix:** drop the `None` key in `read_rows`, or reject ragged rows
outright in `validate` (which also fixes issue 1).

## 3. `noindex` cannot be changed by `sync-metadata`

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

## 4. Batch limits are documented but not enforced

**Severity: medium for a 10,000-item batch.**

`chunk_rows()` groups rows into 500s to match IA's per-run limit, but the loop
has no pacing, no per-chunk checkpoint, and no counter against the 5,000/day
limit. Chunking is structural only. This was flagged and accepted during the
original build review.

**Mitigation today:** size the CSVs by hand; see
[`OPERATIONS.md`](OPERATIONS.md#pacing-and-batch-limits).

## 5. No retry on transient network failures

**Severity: low — expected, and `--resume-from` covers it.**

Uploads to `s3.us.archive.org` intermittently fail with
`SSLEOFError` / `MaxRetryError`. There is no backoff or retry; the row is
logged as `failure` and skipped. Over 10,000 items this will happen regularly.

**Mitigation today:** re-run with `--resume-from`. `checksum=True` means
already-uploaded files are skipped rather than re-sent.

## 6. `--collection` is unvalidated on `--live`

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

## 7. `--files-dir` does not constrain path resolution

**Severity: low — trusted-input tool.**

`Path(files_dir) / row["file"]` will happily resolve `../` or an absolute path
outside the intended directory. Accepted during review: the CSV is authored by
the same person running the tool.
