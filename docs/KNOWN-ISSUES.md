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

## 2. Batch pacing is manual, though the daily total is enforced

**Severity: low. Daily cap enforced 2026-08-23; pacing still manual.**

`chunk_rows()` groups rows into 500s to match IA's per-run limit, but the loop
still has no pacing (no sleep between batches) and no running counter *across*
a day's separate runs — a second run started the same day does not know what
the first one spent. What a single run can no longer do is exceed the
5,000/day cap by itself: both `upload` paths refuse to start such a run and
name the fix (`--limit` on the Sheet path, splitting the file on `--csv`),
with `--allow-over-daily-cap` as the explicit override. Beyond that: the Sheet path's `upload --limit N`
now caps how many items a single invocation uploads at all (an operator can
size a day's runs by hand with a number the tool enforces, rather than by
pre-splitting a CSV), `--chunk-size` makes the 500-per-run batch size an
overridable flag instead of a constant, and a detected rate-limit response
now stops a run cleanly instead of grinding through the rest of the batch as
unexplained failures — though that detector is unverified against a real
response; see `DECISIONS.md`, "Rate-limit detection matches a status
code...".

**Mitigation today:** pace `--limit` across the day's runs by hand — the tool
enforces the cap per run, not per day; see
[`OPERATIONS.md`](OPERATIONS.md#pacing-and-batch-limits). The `--csv` path
still has neither `--limit` nor `--chunk-size` (see `DECISIONS.md`, "`--limit`
counts planned targets...") — size those CSVs by hand, though a file over
5,000 rows is now refused rather than attempted.

## 3. `--collection` is unvalidated on `--live`

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

## 4. `--files-dir` does not constrain path resolution — **`--csv` path only**

**Severity: low — trusted-input tool. Narrowed 2026-08-23: no longer true of
the Sheet path.**

On the **`--csv` path** this still holds: `Path(files_dir) / row["file"]` will
happily resolve `../` or an absolute path outside the intended directory.
Accepted during review — the CSV is authored by the same person running the
tool.

On the **Sheet path it is false**, and reading this section as a general
statement about the tool gives the wrong answer. `resolve_file()` treats
`files_dir` as a hard boundary rather than a starting point: the folder part is
resolved and then checked to still be underneath it, so anything that *resolves
outside* it — an absolute path, or a `..` that climbs past it — is refused with
a message naming both paths. (A `..` that lands back inside is fine; it never
left.) `Path(files_dir) / part` would otherwise discard `files_dir` entirely
whenever `part` is itself absolute.

It also refuses a candidate whose folder segment is empty (a blank folder cell,
which would otherwise turn a missing required cell into a search of
`files_dir`'s own root) rather than treating that as "look in the top level".

## 5. `check_file_exists`'s `is_file()` catch has no test for the case it exists for

*Found 2026-08-22, during the row-readiness effort — pre-existing, not
introduced by it. Reasons corrected 2026-08-22 after the claims below were
checked by running them; the conclusion is unchanged.*

**Severity: latent — tested generically, untested for its actual purpose.**

`validate_rows`' redundant `is_file()` check (`ia_bulk.py`, inside the
`if check_file_exists:` block) is documented, in this project's own build
history, as load-bearing: it is what catches an internal space in a
multi-segment folder cell that `resolve()` normalizes away, when the Sheet
path's earlier file resolution and this later disk re-check disagree. It
looks like a purely decorative, redundant safety net and is not — the comment
above `SHEET_REQUIRED_COLUMNS` already warns that this is "a completely
different mechanism" from the required-columns check nearby and that removing
it "is a different (and wrong) change" from anything that constant's own
shrink calls for.

The block is **not** untested in general. `test_validate_rows_flags_missing_file`
drives it through the CSV path with a genuine on-disk mismatch and asserts the
`"file not found"` message. Deleting the `if check_file_exists:` block outright
was tried during the 2026-08-22 review: three tests fail
(`test_validate_rows_flags_missing_file`,
`test_cmd_validate_returns_one_when_a_row_fails`, and
`test_cmd_upload_fails_validation_before_touching_network` — the last of which
then uploads a file that does not exist). A refactor that removed the call
would not go green.

What *is* uncovered is narrower, and it is the scenario that makes the check
load-bearing rather than merely redundant: the **Sheet path**, where an
internal space in a multi-segment folder cell makes the earlier file
resolution and this later disk re-check disagree. The existing test is a
generic missing-file case on the CSV path, which the resolver alone would
already have caught on the Sheet path. So the check's *ordinary* behavior is
tested; its *reason for existing* is not.

An earlier version of this entry claimed nothing referenced `check_file_exists`
or asserted that message at all, and that deleting the `is_file()` call "would
pass the entire suite green". Both were wrong, and the irony is worth stating
plainly, because the paragraph directly below warns about exactly this: this
entry reached a right conclusion — the check deserves a test aimed at the case
it was written for — by a route nobody had run. It has now been run, and the
conclusion survives on better grounds.

The warning below is unchanged and still stands. Earlier in the same
row-readiness effort, a planning document confidently instructed an
implementer working directly beside this check to "run the existing test that
covers the internal-space case" — stated as settled fact. No test covers that
case; the implementer caught the false premise and said so rather than
fabricating coverage. A doc asserting a right conclusion ("don't touch this
check") for a wrong reason ("here is the test proving it's safe") is not safe
to leave standing, because the next reader has no way to tell the reason was
never checked.

Reproducing the underlying scenario is likely platform-specific: the
originally observed disagreement was Windows-specific (`Path.is_dir()`
normalizing away a trailing space that `Path.iterdir()` on the same path does
not), so a fixture built on a different OS may not reproduce it at all — which
is itself part of why no test exists yet.

**Mitigation today:** none — this is a test-coverage gap, not a behavior
change. Recorded here so it is not lost the next time this file is reviewed.

## Fixed

### No retry or backoff on transient network failures

*Fixed 2026-09-02, closing issue #5.* A transient failure talking to
archive.org failed that row for the whole run — a `ReadTimeout` with nothing
wrong with the data, the Sheet or the file. Recovery was correct but manual:
the row was never marked done, so the operator re-ran to chase a flake.

`retry_ia_call()` now wraps the network call inside `upload_row()` and
`update_metadata_row()`: three attempts, backing off ~2s then ~4s with jitter.
It retries connection errors, timeouts and 500/502/504, and does **not** retry
refusals (403, 400, any 4xx) or 429/503 — those still stop the run so the
operator resumes tomorrow. Reading `internetarchive` 5.10.1's source showed
the real gap was the S3 file transfer, which had no retry of any kind, rather
than the metadata read, which already had three. See
[`DECISIONS.md`](decisions/QUOTA-AND-RUNS.md#retry-covers-transport-failures-never-refusals).

Re-running with `--resume-from` is still the recovery for a row that fails all
three attempts.

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
