# IA Bulk Upload CLI — Architecture

This is the design reference. For running a batch see
[`OPERATIONS.md`](OPERATIONS.md); for preparing an offline CSV see
[`CSV-PREPARATION.md`](CSV-PREPARATION.md); for verified defects see
[`KNOWN-ISSUES.md`](KNOWN-ISSUES.md); for rationale and reversed decisions see
[`DECISIONS.md`](DECISIONS.md).

## Purpose
Single-script CLI (`ia_bulk.py`) for validating, uploading, and syncing
metadata for Internet Archive items from a project's Google Sheet, read live
over the Sheets API — see [`DECISIONS.md`](decisions/SHEET-PROTOCOL.md#the-sheet-is-read-live-the-csv-becomes-the-offline-path).
A hand-prepared CSV (`--csv`) remains a deliberate offline/dry-run fallback
for `validate` and `upload`; `sync-metadata` always takes a CSV. Generic to "a
project" so a second LCPS project can reuse this pipeline — see
`projects_registry.json`.

## Sheet source and column mapping
`validate`/`upload` read a project's Sheet (its test Sheet by default, its
real one with `--live`) via `build_sheet_client()` → `SheetClient.read_grid()`,
then `grid_to_rows()` turns the grid into the same `list[dict[str, str]]`
shape `read_csv()` produces for the CSV path, so every downstream function
(`validate_rows`, `upload_row`, `effective_identifier`, the whole `zztest-`
safety rail) is unaware of which source a row came from.

**Header normalization.** `normalize_header()` (`column_map.py`) is the one
rule that turns a Sheet header into an IA metadata field name: lowercase,
strip surrounding whitespace, drop punctuation (hyphens and underscores kept,
since IA field names like `identifier-bib` use them), collapse whitespace and
repeated underscores to single underscores. It does not correct typos — a
misspelled header ships as-is — see
[`DECISIONS.md`](decisions/READINESS.md#a-malformed-header-is-rejected-never-auto-corrected).
`ColumnMap.field_names` is the resulting `{raw header: normalized name}` map
for every header the Sheet had that run; `check_column_map()` rejects two
headers that normalize to the same field name (a silent per-row overwrite)
and a header that normalizes to an empty name, filing both under row 1
(mirroring `header_validation()`'s CSV-path row-1 convention). `check_grid_shape()`
mirrors `check_row_shape()`: a data row with more fields than the header is
flagged with its real row number, since the Sheets API already omits trailing
empty cells (a short row is not a defect) but never inserts anything.

**Held-back columns.** A header containing `(LCPS Internal)`, matched
case-insensitively, is recorded in `ColumnMap.held_back` and excluded by
`uploadable_fields()` — it is normalized and reported on (so its transform is
still visible), but never uploaded.

**Tool-owned columns.** `upload` writes exactly four columns, all `ia_`-prefixed
so they cannot collide with whatever a Sheet author already has:
`ia_identifier`, `ia_uploaded`, `ia_url`, `ia_identifier_bib`. `uploadable_fields()`
also excludes these (`RESERVED_FIELDS`) alongside `file`, so the tool's own
bookkeeping never ships as IA metadata. The Sheet's own `identifier` column, if
it has one, is ordinary donor metadata (the archival reference the donor
supplied), never the minted IA identifier — see
[`DECISIONS.md`](decisions/IDENTIFIERS.md#tool-owned-sheet-columns-are-all-ia_-prefixed). All four
`ia_` columns must already exist as Sheet headers before `upload` will run, in
every mode including the default rehearsal — see
[`DECISIONS.md`](decisions/IDENTIFIERS.md#the-four-ia_-columns-are-required-in-every-mode-including-the-safe-one).

`format_field_receipt()` prints, before anything permanent happens, exactly
which normalized fields will upload and which are held back. `file` and the
four `ia_` columns never reach this list at all — `uploadable_fields()`
already excludes them via `RESERVED_FIELDS` (see "Tool-owned columns" above).
The receipt separates two different reasons a column does not ship. "NOT
uploaded — Internet Archive reserves these names" is `identifier` alone
(`DROPPED_BY_UPLOAD_ROW`): the one name `upload_row` itself strips that
isn't already tool-owned, since the Sheet's own `Identifier` column, if it
has one, is ordinary donor metadata rather than something `RESERVED_FIELDS`
would catch earlier. "uploaded with a value this tool generates" is
`mediatype` and `collection` (`ia_fields.PIPELINE_OWNED_FIELDS`): those
fields *are* sent, but `upload_from_sheet` overwrites `row['mediatype']`
from the registry and `upload_row` sets `metadata['collection']`
unconditionally, so a Sheet column of either name has its own value
discarded. That section prints only when such a column actually exists —
it is a collision warning, not a standing disclaimer. See
[`DECISIONS.md`](decisions/FILES-AND-METADATA.md#sheet-metadata-is-filtered-at-the-upload-boundary-not-in-upload_row).

**File resolution.** A row's file is *resolved*, not constructed from a
path template: `resolve_file()` looks in the folder named by `file_template`'s
substituted columns for an exact filename match, then a case-insensitive
stem match, and raises rather than picks between two candidates that share a
stem — see
[`DECISIONS.md`](decisions/FILES-AND-METADATA.md#a-file-is-found-by-resolution-not-by-constructing-a-path).
The resolved name (which can differ from what the Sheet cell says) becomes
both `row["file"]` and `ia_identifier_bib`.

## CSV schemas
This section covers the offline `--csv` path (`validate --csv`/`upload --csv`)
and `sync-metadata`, which always takes a CSV — there is no Sheet-reading path
for metadata corrections; see "`sync-metadata` is CSV-only" below.

### `validate --csv` / `upload --csv`
Required columns: `identifier`, `file`, `mediatype`, `title`.
All other columns pass through untouched as IA item metadata.

`read_csv()` returns a `CsvData` carrying the header and the rows together,
because validating either alone misses the failure that matters:
`check_row_shape()` can only see a row/header field-count mismatch, and
`check_header()` can only see the header text. Both must pass before any
network call.

- `identifier`: pre-assigned in the CSV, permanent, never generated or
  renamed by this tool. Must match `COLLECTIONKEY-PROJECTID-NUMBER`,
  lowercase, hyphen-separated, 5-digit zero-padded NUMBER.
- `file`: filename (optionally with a relative subpath), resolved against
  `--files-dir`.
- `mediatype`, `title`: required, non-empty.
- `date`: optional and free-form — IA doesn't enforce a date format.
  `upload` fills a blank `date` cell with `[n.d.]` (the standard archival
  "no date" abbreviation) rather than omitting the field, so every IA item
  ends up with a date value either way.

On the Sheet path these same four concepts exist but split differently:
`mediatype` is injected from the registry (structurally required, never a
Sheet column); `file` is resolved, not required, since its presence is fully
determined by file resolution (see "Readiness" below); `title` moves into the
project's `required_for_upload` list; `identifier` is `ia_identifier` instead,
optional until `upload` mints one. `SHEET_REQUIRED_COLUMNS` is `("mediatype",)`
— everything else that used to be a hard requirement is now either resolved
or a readiness question.

### `sync-metadata` is CSV-only
`sync-metadata` always takes a CSV positional argument (`identifier` plus
whichever metadata columns changed) and never reads a Sheet — `cmd_sync_metadata`
calls `load_registry()` directly and builds no `ColumnMap`, unlike `validate`/
`upload`. `--project` is required on the command line (for consistency with
the other two commands) but the CSV's own columns are what gets sent; there is
no per-project Sheet, `file_template`, or `required_for_upload` rule in play
here. See [`DECISIONS.md`](decisions/FILES-AND-METADATA.md#blank-cell-means-leave-alone-not-clear).

Only requires an `identifier` column plus whichever metadata columns changed.
Does not require `file`, `mediatype`, `title`, or `date`.
A blank cell means "leave this field alone" — `update_metadata_row` drops
blank cells from the request entirely rather than sending an empty string,
since the whole point of this CSV shape is to list only what changed. To
actually delete an existing field on the IA item, put the literal value
`REMOVE_TAG` in that cell: the `internetarchive` library (and the official
`ia` CLI's `--modify field:REMOVE_TAG`) treats that exact string as a
delete sentinel and issues a metadata "remove" op for the field.

## Identifier scheme
See `.claude/CLAUDE.md` for the full identifier scheme and project
registry rationale. `projects_registry.json` holds each project's
`ia_collection`, Sheet IDs, `file_template`, and `required_for_upload` list,
plus the shared `collection_key`; `validate` and `sync-metadata --csv` reject
any identifier whose prefix isn't registered there — and, since issue #2, any
whose `PROJECTID` names a registered project other than the run's own
`--project`. `sync-metadata` on the Sheet path targets the item `ia_url`
names rather than an identifier column, so it makes the `--project` half of
that check against that item and does not run the registry-prefix check at
all. See
[`IDENTIFIERS.md`](decisions/IDENTIFIERS.md#an-identifier-is-checked-against-the-runs-project-not-the-whole-registry).

The permanent identifier always holds the real, permanent value — `check_identifier`
only accepts the registry's actual `collection_key` as the first segment.
There is no separate "test" identifier form in the CSV or the Sheet; see
"Safety rail" below for how test runs are kept safe instead. On the Sheet
path the permanent identifier is minted by `upload` and written to
`ia_identifier` (see "Sheet source and column mapping" above and
[`DECISIONS.md`](decisions/IDENTIFIERS.md#identifiers-are-minted-by-upload-and-written-back-to-the-sheet));
on the CSV path it is pre-assigned and simply named `identifier`.

## Readiness
A row can be **not-ready** (a human hasn't filled in what it needs yet) or
**invalid** (it asserted something and got it wrong) — orthogonal questions
about the same row, both tracked on `RowValidation`: `errors`/`is_valid` for
validity, unchanged in meaning, and a new `missing_fields` list whose
`readiness` property (`Readiness.READY`/`Readiness.NOT_READY`) is derived from
it, never stored separately. The reasoning — why blank and wrong are different
kinds of failure, why this isn't a fourth `RowState`, and why `validate` and
`upload` report the backlog differently — lives entirely in
[`DECISIONS.md`, "A blank cell is not an error"](decisions/READINESS.md#a-blank-cell-is-not-an-error);
this section only covers the mechanism.

**Two sources feed `missing_fields`, always in this order:** the project's
`required_for_upload` list (normalized column names, e.g. `["title", "theme"]`,
checked by `validate_sheet_rows`) and the `file_template` columns that were
blank (found by `resolve_sheet_files`, which never calls the file resolver at
all for a blank or whitespace-only candidate — see `FileOutcomes.blank`).
Classification happens at that point, in `resolve_sheet_files`, because it is
the last point the raw candidate still exists: afterward, a row nobody
touched and a row with a typo'd filename are both `row["file"] == ""` and
indistinguishable. A non-blank candidate that fails to resolve is recorded in
`FileOutcomes.errors` instead — a real error, not a readiness fact. So are
ALL rows of a group resolving to the same disk file (keyed through
`claim_key()`, like the reconcile survey): two rows cannot claim one
photograph, and identical `file_template` cells would also blind the
mid-run-edit guard's fingerprint. Only the remedy differs between them — a
group holding exactly one already-uploaded row names that row as the one to
keep, since its identifier is permanent and its row is the only link to that
identifier's metadata. See
[`SHEET-PROTOCOL.md`](decisions/SHEET-PROTOCOL.md#a-fingerprint-only-proves-identity-while-it-is-unique).

**`required_for_upload` is a registry key, not a code constant.** It has no
default (a missing key is a hard `ConfigError` from `load_project_config`),
and `check_required_for_upload()` cross-checks every name in it against the
Sheet's actual normalized headers at startup, failing loudly on a typo rather
than silently marking every row not-ready forever.

**Reporting.** `format_readiness_breakdown()` counts not-ready rows by which
field is missing (derived from each result's own `missing_fields`, never a
hardcoded list), printed by `validate`. `upload` does not print this
breakdown — see `DECISIONS.md` as linked above — and only a row that was
actually in scope (ready, but failing validation) affects `upload`'s exit
code; `plan_upload_targets` excludes not-ready rows from that scope entirely
(a row's own docstring note explains why filtering on `is_valid` alone would
have uploaded an uncatalogued row under a permanent identifier with no
title).

## The reserve → upload → confirm protocol
Per chunk, `SheetUploadRun.execute()` does four things in this order:

1. **verify** — re-reads the Sheet and checks, per target, that its
   `file_template` columns still fingerprint the same photograph, that —
   before the reserve write only — no OTHER row in the fresh read carries
   the same fingerprint (a duplicated fingerprint proves nothing about which
   physical row is underneath; after reserve the row's own proven-unique
   number is the stronger proof, and vetoing there would withhold the
   confirm write for edits that shifted nothing — see
   [`SHEET-PROTOCOL.md`](decisions/SHEET-PROTOCOL.md#a-fingerprint-only-proves-identity-while-it-is-unique)),
   and that `ia_identifier` is still blank or already ours (see
   [`DECISIONS.md`](decisions/SHEET-PROTOCOL.md#a-rows-identity-is-its-file_template-columns-not-its-ia_identifier)
   for why the fingerprint, not `ia_identifier`, is what makes this check
   meaningful). Targets that moved are reported and skipped, never written to.
   Before the reserve write only, `check_claimed_identifiers()` additionally
   compares this run's *minted* numbers against every `ia_identifier` the
   fresh read holds, anywhere in the Sheet. The per-target check above sees
   only a target's own row, so a number claimed on a row this run is not
   targeting is invisible to it — and that is the case that mints a
   duplicate. A collision stops the whole run rather than dropping one
   target: every number came out of the same `max+1` arithmetic over the
   same stale read, so one collision means the maximum was wrong and the
   rest are suspect. Nothing is reserved or uploaded at that point, so a
   rerun re-mints from the Sheet's current state.
2. **reserve** — one batch write (`write_cells_if_any`) putting each target's
   minted `ia_identifier` in the Sheet, before any upload happens.
3. **upload** — row by row, via `upload_row()`/`internetarchive.upload()`, so
   each row gets its own logged outcome.
4. **verify, then confirm** — re-verifies the rows that actually succeeded,
   then one batch write of `ia_uploaded`, `ia_url`, and `ia_identifier_bib` —
   only for rows whose upload succeeded and whose fingerprint still matches.

Reserving before uploading is deliberate: uploading first would let a crash
strand an item on Internet Archive that the Sheet has no record of, and the
next run's minting would then reuse that same number for a different
photograph, permanently — see
[`DECISIONS.md`](decisions/IDENTIFIERS.md#identifiers-are-minted-by-upload-and-written-back-to-the-sheet).
A `write` that fails mid-protocol (`SheetUploadRun._write()`) prints a clean
message and stops the run rather than raising, and a rate-limited row
(`is_rate_limit_error()`) stops the run after finishing the current chunk's
confirm write, so nothing already uploaded is left reserved-but-unconfirmed —
see "Chunking" below.

A row is chosen for this run based on its own two tool-owned columns
(`classify_row()` → `RowState.UNASSIGNED`/`RESERVED`/`DONE`): blank
`ia_identifier` means mint-and-reserve, a set `ia_identifier` with blank
`ia_uploaded` means retry under the existing identifier (crash recovery,
never re-mint), both set means skip entirely.

## Chunking
The **Sheet path** processes targets in batches of 500 (IA's per-run batch
limit) by default, via `chunk_rows()`. This is a real checkpoint boundary:
each chunk gets its own re-read of the Sheet, reserve write, uploads and
confirm write. It is not a literal separate CSV file per chunk — each row is
uploaded individually through the `internetarchive` Python library so
outcomes are captured per-row. `upload --chunk-size N` overrides the batch
size for that run (`SheetUploadRun.chunk_size`, threaded into `chunk_rows()`).

The **`--csv` path does not chunk at all.** `run_rows()` is a flat loop; it
previously iterated `chunk_rows()` and then the rows within each chunk,
which was exactly equivalent — nothing happened at a boundary — while
implying a batching guarantee that path does not have. This is why `--csv`
has no `--chunk-size`: there is no per-chunk Sheet write for a batch size to
change.

Neither path may exceed `DAILY_ITEM_CAP` (5,000, IA's per-account daily
limit) in one run. Both refuse rather than silently capping and name the fix
— `--limit` on the Sheet path, splitting the file on `--csv` — with
`--allow-over-daily-cap` as the explicit override.

`upload --limit N` (Sheet path only) caps how many *planned* upload targets
(valid, ready, not already done — `plan_upload_targets()`'s own output) a
single invocation processes at all, applied before chunking: `--limit 10
--chunk-size 3` means 10 items total, in batches of 3. It counts targets
in scope, never raw Sheet rows scanned, and it and `--chunk-size` are both
recorded in the run's `run_header` log record (below) so a capped or
rechunked run stays reconstructable later. See `DECISIONS.md`, "`--limit`
counts planned targets...".

If an upload fails with what `is_rate_limit_error()` recognizes as
Internet Archive's rate limit, `SheetUploadRun.execute()` stops the whole
run after confirming whatever it already uploaded in the current chunk (and
any earlier chunk), rather than treating it as one more per-row failure and
continuing. This detector is best-effort, not confirmed against a real
rate-limit response — see `DECISIONS.md`, "Still open".

## Progress output
`upload`/`sync-metadata` print a `[position/total] ...` line to stdout
before each row, plus a `X uploaded successfully, Y error(s)` summary line
before the final `log written to <path>` line, so a run is never silently
quiet. `upload_row` also passes `verbose=True` through to
`internetarchive.upload()`, which prints its own `tqdm` byte-progress bar
per file — that's IA's own upload status, not something this tool
fabricates. It also passes `checksum=True`, so re-running `upload` against
a CSV whose files haven't changed skips re-uploading (and re-triggering
IA's `derive` task) for anything already present with a matching MD5.

## Correcting an uploaded item

`sync-metadata` reads the Sheet live like `validate` and `upload`. Its scope
is every row `classify_row()` calls DONE, and its target for each is the
identifier in that row's own `ia_url` cell — which `upload`'s confirm write
put there, complete with the per-run `zztest-` stamp in test mode. Nothing is
re-derived, so a Sheet whose rows were uploaded by different runs under
different stamps is handled without the command knowing that happened.

The fields sent come from `sheet_metadata_fields()`, shared with `upload`, so
a column that uploads but does not sync cannot exist. Blank cells are dropped
by `update_metadata_row()` — blank means "leave this field alone", and
`REMOVE_TAG` deletes. Every DONE row is sent every run; IA's *no changes to
`_meta.xml`* response becomes `MetadataUnchanged` and is counted as
`unchanged`, which is what makes that idempotent.

The `--csv` path is the offline fallback and is the only one that needs
`--from-log`: a CSV carries real identifiers, so in test mode the stamped
target has to come from the upload log's `uploaded_as` field. See
[`DECISIONS.md`](DECISIONS.md), "The Sheet is the correction".

## Reconciling filenames

`reconcile-files` reads the Sheet live like `validate`/`upload`, but
neither uploads nor changes metadata. `survey_files()` resolves every row
against `files_dir` the same way `resolve_sheet_files()` does, and returns
a `FileSurvey` splitting the result into `claimed` (every disk file some
row currently resolves to) and `unclaimed` (every file matching the
project's `photo_extensions`, in a row's own folder, that no row claims;
both keyed through `claim_key()`, so two rows spelling one folder
differently — `SOP CD 1` and `sop cd 1`, one folder on a case-insensitive
filesystem — cannot end up in two disjoint namespaces with the duplicate
check missing between them) —
plus `unresolved`/`wanted`, the row numbers that failed and what each
one's filename cell said. A row whose `file_template` cells are blank is
counted in `not_ready` and appears in none of the other three: it asserted
no file, so it is not-ready rather than broken (the same split
`resolve_sheet_files()` draws between `errors` and `blank`), and
`cmd_reconcile_files` reports the count in one line rather than raising a
prompt with no proposal and no candidates for each of the ~2,900
uncatalogued rows. Unlike `resolve_sheet_files()`, it never mutates
the rows it's given: reconciliation shows a row's own cells to a human
before any decision is made, so they have to still read exactly as
written.

`propose_match()` (`reconcile.py`) looks for a single best match for an
unresolved row among that row's `unclaimed` candidates — see
[`DECISIONS.md`](decisions/RECONCILIATION.md) for the two-pass matching
strategy and its guards. `cmd_reconcile_files` prompts about each
proposal via `prompt_for_decision()` and, on acceptance, batches a
`CellUpdate` for that row's filename column alone, flushing every
`RECONCILE_FLUSH_EVERY` (25) accepted rows so a long interactive session
doesn't hold hundreds of accepted corrections in memory, unsaved, until
the very end.

**Accepted corrections are re-checked before they are written.**
`flush()` re-reads the grid and drops any pending update whose row no
longer holds the filename it was matched against, saying so on screen. This
is the same hazard `upload`'s `sheet_row_fingerprints()` guard exists for,
one cell wide: `reconcile-files` has the longest read-to-write window in
the tool — an interactive session over a shared Sheet — so a row inserted
or deleted mid-session would otherwise shift every later write by one. A
re-read that fails stops the run rather than writing unverified.
Reconciliation is also the **only** command that writes a `file_template`
column, which `sheet_row_fingerprints()` otherwise relies on being
never-written; the two never run together, and a correction landing during
an `upload` makes that row fingerprint as moved and be skipped, which is
the safe direction.

**The candidate pool shrinks as the run progresses.** `claimed` is a
snapshot built once, before any row is decided, but the candidates offered
to each row are re-filtered against it on *every* iteration of the loop —
not computed once before the loop starts, and `unclaimed` itself is never
recomputed. Accepting a file for one row adds it to `claimed` immediately,
so a later row in the same folder never sees it as a candidate again.
Without that per-iteration filter, two misspelled rows in one folder that
both happen to be within matching distance of the same single file on
disk could each be proposed it — and each accepted onto it — pointing two
Sheet rows at one photograph. A review caught exactly this: the original
implementation filtered `unclaimed` against `claimed` only once, before
the loop started.

`log_decision()` writes reconciliation's own JSONL shape — one line per
row *considered*, not per row acted on:
`{row, folder, wanted, status, chosen, proposed, matches, reason,
timestamp}`, with `status` one of
`accepted`/`typed`/`rejected`/`no_candidate`/`ambiguous`/`stopped`. Every
key is on every line, empty where it does not apply. `chosen` is what was
written and so is empty on every path but an acceptance; `proposed` is what
the tool put forward, which is the only record of *what* a rejection turned
down; `matches` names the files an `ambiguous` row could not be chosen
between. `accepted` versus `typed` comes from how the operator answered
(`[y]` versus `[e]`), not from comparing the two strings — a name typed at
`[e]` is still typed when it happens to equal the proposal.
This is a different record than `upload`/`sync-metadata`'s per-row-result
log below, because it answers a different question later: not "did this
identifier upload", but "what did a human decide about this row, and
why". `--dry-run` opens no log at all — nothing here was decided, only
printed.

## Appending skeleton rows

`append-rows` is reconciliation's other half: once every row that names a
file resolves, files still unclaimed are genuinely uncatalogued, and
`cmd_append_rows` appends one row per such file — values in the
`file_template` columns only, placed by the real header's column indexes
and padded to its width, every other cell blank for the cataloguer.

It does **not** reuse `survey_files()`'s `unclaimed` map, and the
difference is the point: that map only lists folders some catalogued row
already names (reconcile can only prompt about rows that exist), while
`scan_unclaimed_files()` walks every subdirectory of `files_dir` — a
brand-new donor folder with zero rows is exactly what append exists to
pick up. Both sides key through `claim_key()`. Photo files at the top of
`files_dir`, outside any folder, are reported rather than silently
skipped: a folder/name template cannot express a row for them.

Two gates are stricter than reconcile's. Any unresolved row is fatal (see
[`DECISIONS.md`](decisions/RECONCILIATION.md), "Reconciliation ships
before append") — a typo'd row and a missing row both present as an
unclaimed file, so appending past one duplicates a photograph's row. And a
structurally shifted data row, which reconcile merely skips, is fatal
here: reconcile's operator approves rows one at a time, but append trusts
the whole survey at once, and a misread row can make the file it really
means look unclaimed.

The write is `SheetClient.append_rows()` — a single `values.append` call
with `RAW` (a filename starting with `=` must land as text, the same
reason `write_cells` uses it) and `INSERT_ROWS` (add rows, never overwrite
whatever sits below the table). The API finds the end of the data itself,
so no row index is computed — or raced over — on this side; there is no
moved-row window to guard the way reconcile's `flush()` must. Idempotence
comes from the drive and the Sheet, not from any state the command keeps:
appended rows resolve on the next survey, so their files are claimed and a
rerun over an unchanged drive appends nothing. The run log
(`append-rows-<timestamp>.jsonl`, `{folder, name, status, timestamp}`) is
written only after the append call succeeds — it records what happened,
not what was attempted.

## Logging and resume
Every `upload`/`sync-metadata` run writes a timestamped JSONL log to
`logs/<command>-<timestamp>.jsonl`, one line per row:
`{identifier, file, status, error, uploaded_as, live, timestamp}`.
Every `timestamp` this tool records is ISO-8601 UTC with an explicit `Z`
(`utc_timestamp()`), as is the `ia_uploaded` cell written back to the Sheet.
Local time repeats an hour during the DST fall-back transition, so a run
spanning it would stamp a later chunk with an earlier wall-clock time — the
same reason `run_stamp()` uses UTC.

On the Sheet path, `log_run_header()` writes one more record as the log's
**first** line, before any row result: `{record: "run_header", timestamp,
project, live, dry_run, sheet_id, collection, files_dir, file_template,
columns, held_back, required_for_upload, limit, chunk_size}`. `columns`
and `held_back` come from that run's `ColumnMap` (every header the Sheet
had, and which were excluded as `(LCPS Internal)`); `required_for_upload`,
`limit` and `chunk_size` are the readiness/scope/batching rules in effect
that run. All of these can change between runs even though none of them
changes per row within one, which is why they are captured once here
rather than left to be reconstructed later from a Sheet that has since
moved on. `load_prior_successes()` explicitly skips this record by its
`record` field (not merely by lacking a `status` key, which would also
happen to work but for the wrong reason) so it is never mistaken for a
row result.

`load_prior_successes()` also **skips any line it cannot read** rather than
raising, and reports the count on stderr. `log_result()` appends per row
with no atomic write, so a run killed mid-write leaves a truncated final
line — raising on it made the log permanently unusable as a resume source,
disabling the only recovery mechanism the `--csv` path has using the exact
crash it exists to recover from. A skipped line means that row is attempted
again, which is safe: IA matches on MD5.

**`dry_run` is always `False` in a real log.** `upload_from_sheet` returns
on the `if dry_run:` branch (nothing uploaded, nothing logged) before
`open_log()`/`log_run_header()` are ever reached, so no log a real run
produces can show `dry_run: true` — that value is real and exercised by
`log_run_header()`'s own unit tests calling it directly with `dry_run=True`,
but it is not something to expect varying in `logs/*.jsonl`.

`identifier` is always the real CSV identifier. `uploaded_as` is the
identifier actually sent to IA for that row (see "Safety rail" below), so
you can see exactly what landed on the site. `live` records which mode
(test vs. `--live`) produced that row's result.

`--resume-from <log>` reads identifiers marked `"status": "success"` or
`"status": "unchanged"` from a prior log **written in the same mode as the
current run** and skips them; `load_prior_successes(log_path, live)` filters
on the log's `live` field before matching. This is deliberate, not
incidental: since the CSV's `identifier` column is identical for a test run
and a `--live` run of the same file (only `uploaded_as` differs), a success
recorded in `test_collection` says nothing about whether the real item
exists — treating it as interchangeable with a `--live` success would let a
`--live --resume-from <test-run-log>` silently skip a real upload while
still reporting it as successful. Log lines written before this `live`
field existed have no mode recorded and are treated as matching neither
mode, so old-format logs are simply not used to skip anything rather than
skip in the wrong mode. The new run still writes its own complete log
(carrying forward the skipped identifiers as pre-recorded successes), so
each log is a self-contained record of what happened by that point.
`--resume-from` is a `--csv`-path flag only — the Sheet path's `ia_uploaded`
column is already the record of what is done, so a rerun resumes by itself.

Rows carried over via `--resume-from` also skip re-validation in
`validate_rows`/`validate_identifiers` (their identifiers already passed a
prior run's checks) - `skip_identifiers` short-circuits the per-row
checks but still records the identifier for duplicate detection, and row
numbers stay aligned with the full CSV either way.

## `sync-metadata`'s "unchanged" status
IA's metadata-update endpoint returns an HTTP 400 with
`{"error": "no changes to _meta.xml"}` when every field in the request
already matches what's on the item — i.e. nothing was wrong, there was
just nothing to do. `update_metadata_row` detects that specific error and
raises `MetadataUnchanged` instead of `RuntimeError`; `cmd_sync_metadata`
catches it separately, logs the row as `"status": "unchanged"` (not
`"failure"`), and reports it in its own summary bucket
(`X updated successfully, Y unchanged, Z error(s)`) so a CSV row that's
already correct doesn't inflate the error count or flip the exit code.

## Safety rail
Default target is `test_collection`; `--live` is required to target the
real collection and use the real identifier as-is. When not `--live`,
`effective_identifier()` prepends `zztest-<run's stamp>-` to the real
identifier for every network call (e.g.
`zztest-20260819t144907-lcps-sarasoldphotos-00001`) — this happens
automatically, in code, rather than requiring the CSV or Sheet to already
contain test-prefixed identifiers. Neither the CSV nor the Sheet's
`ia_identifier` column ever needs to change between a test run and a
`--live` run.

The stamp (`run_stamp()`) is computed once per invocation and shared by
every row that run touches, so a rehearsal's items group together and never
collide with a previous rehearsal's — see
[`docs/DECISIONS.md`](decisions/IDENTIFIERS.md#test-identifiers-carry-a-per-run-stamp)
for why a bare `zztest-` prefix made every fresh-Sheet rehearsal collide
with the last one. (A *resumed* run is its own invocation with its own
stamp, so its items land under a second stamp, not the original run's —
`--resume-from` still recognizes them as done since it matches on the real
`identifier`, never on the stamped `uploaded_as`.) `--live` identifiers
never carry a stamp: they are the permanent, public ones and must stay a
pure function of the Sheet/CSV.

## Known gaps
Verified defects with reproductions live in
[`KNOWN-ISSUES.md`](KNOWN-ISSUES.md). The design-level gaps are below.

`projects_registry.json`'s `collection_key` value (`lcps`) has never been
confirmed against LCPS's actual IA collection identifier — confirm it before
any `--live` run. A wrong value here doesn't cause data loss (validation
would just reject every real identifier), but it needs to be right before
real uploads can pass `validate`.

The target IA collection is the project's `ia_collection` in
`projects_registry.json`. `upload`'s `--collection` flag no longer defaults
to `"lcps"` — that string is not a real Internet Archive collection, and a
`--live` run would have pushed real photographs at a collection that does
not exist and reported success. The flag survives only as an explicit
override on the `--csv` path, where `--live` now refuses to run without it;
on the Sheet path passing it is an error rather than a silently ignored
value. Nothing still validates `ia_collection` against IA itself at
runtime, so confirm it by hand once, in version control, before the first
`--live` run — `upload --dry-run` prints everything the run would do
without doing any of it. See `DECISIONS.md`, "Technical configuration lives
in the registry". (`ia_collection` for this project was confirmed by hand
against archive.org on 2026-08-22 — see `DECISIONS.md`, "Still open" — but
the tool itself still does not check this automatically, and a second
project's registry entry would need the same manual confirmation.)

The offline `--csv` path still requires a hand-prepared CSV matching the
exact schema under "CSV schemas" above — including a `mediatype` column,
which is not part of a raw Sheet export. That transformation is a
deliberate, explicit step a human performs, not something this CLI does
automatically; see [`CSV-PREPARATION.md`](CSV-PREPARATION.md) for the
procedure and the failure modes it guards against. It does not apply to the
default Sheet path, where header problems are structurally impossible in
the same way (a header containing a comma is just a header containing a
comma, never a CSV-parsing artifact) — see
[`DECISIONS.md`](decisions/SHEET-PROTOCOL.md#the-sheet-is-read-live-the-csv-becomes-the-offline-path).

`validate` backstops the structural half of the CSV transformation:
`check_header()` rejects headers with surrounding whitespace, duplicates, or a
case variant of a column the script reads by name, and `check_row_shape()`
rejects any row whose field count disagrees with the header — which is what an
unquoted comma in a header cell produces. Header problems are reported as
row 1. It cannot check whether a correctly-formed header is *semantically*
right, so the manual proofread in
[`CSV-PREPARATION.md`](CSV-PREPARATION.md) still matters for that path.
