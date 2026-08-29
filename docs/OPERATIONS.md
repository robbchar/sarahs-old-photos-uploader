# Operations Runbook

How to actually run a batch, from Google Sheet to Internet Archive. Read
[`ARCHITECTURE.md`](ARCHITECTURE.md) first if you want to know *why* the tool
is shaped this way; read [`KNOWN-ISSUES.md`](KNOWN-ISSUES.md) before you trust
a run's output.

**Current status: no `--live` run has ever happened.** Every log in `logs/` is
a `test_collection` run (`"live": false`). The first real run is still ahead,
and several things below have never been exercised against production.

## Google Cloud prerequisites (one-time, per operator)

Reading and writing the Sheet uses Google OAuth as a real signed-in person,
not a service account and not an API key — both were considered and ruled
out; see [`docs/DECISIONS.md`](DECISIONS.md#still-open), "Which Google
credential type the Sheet integration uses". Concretely, before the first
run on a machine:

- The Google Cloud project backing this integration must have its OAuth
  consent screen set to **Internal** user type, and the project itself must
  sit inside the **lcpsociety.org** Google Workspace organization. Internal
  apps skip Google's verification review and are not subject to the 7-day
  refresh-token expiry that External apps in Testing status get.
- Whoever runs `ia_bulk.py` and completes the one-time browser consent must
  do so with an **`@lcpsociety.org` account**. A personal `gmail.com` account
  cannot authorize an Internal app — Google refuses with `org_internal`.
- An OAuth 2.0 **Desktop app** client ID's downloaded JSON secret must be
  saved to `.ignored/google-client-secret.json` (relative to the project
  root; never committed — everything under `.ignored/` is gitignored).
- The target Sheet (both the real one and the test one named in the
  project's registry entry) must be shared with — or owned by — that same
  `@lcpsociety.org` account, with **edit** access. `upload` writes
  `ia_identifier`/`ia_uploaded`/`ia_url`/`ia_identifier_bib` back to the
  Sheet, so read-only sharing is not enough even in test mode with
  `--write-identifier`.

**First run, on a terminal a human is sitting at:** running any Sheet-reading
command (`validate` or `upload`, no special flag needed) opens a browser for
the consent screen once. Approve it as the `@lcpsociety.org` account above,
and the resulting token is cached to `.ignored/google-token.json`. Every
later run — including an unattended one, like a cron job — reuses and
silently refreshes that cached token; it only fails loudly if the token is
missing, unreadable, or its refresh token has been revoked, in which case
re-run from an interactive terminal to re-consent.

## The pipeline

```
Google Sheet (read live)  →  validate  →  upload  →  sync-metadata
                                             (test)     (corrections)
                                                ↓
                                          upload --live

offline / dry-run fallback:
Google Sheet  →  raw CSV export  →  hand-prepared CSV  →  validate/upload --csv
                                    (see CSV-PREPARATION.md)
```

The Sheet is the source of truth, and `validate`/`upload` read it directly
over the Google Sheets API — see [`docs/DECISIONS.md`](DECISIONS.md), "The
Sheet is read live; the CSV becomes the offline path". A hand-prepared CSV
export (`--csv`) is a deliberate fallback for offline and dry-run work, not
the normal route: it is a snapshot that goes stale the moment someone edits
the Sheet, so treat one as disposable and re-export before trusting it rather
than reusing yesterday's file.

Bringing a batch up from nothing to permanent Internet Archive items is the
four numbered phases below — validate, rehearse, go live, correct — matching
the pipeline diagram above exactly. None of them starts with a CSV export;
that only exists on the offline fallback described immediately below, which
is not one of the four phases and is not something every run needs.

### Offline path: preparing a CSV

Skip this unless you are deliberately validating or uploading from a
hand-prepared CSV instead of reading the live Sheet (offline work, or a
frozen snapshot to compare against). The raw Sheet export **does not** match
the schema `validate --csv`/`upload --csv` require and will silently produce
wrong metadata if this step is skipped. Follow
[`CSV-PREPARATION.md`](CSV-PREPARATION.md) — it is the highest-risk step on
that path.

## 0. Reconcile file names (when rows don't resolve)

Not one of the four phases above — a prerequisite for the first of them,
needed only when it applies. Run it whenever `validate` (§1) reports rows
whose filename cell doesn't resolve against the drive, before trusting the
rest of that report.

```bash
# see what it would propose, without prompting or writing anything
python ia_bulk.py reconcile-files --project sarasoldphotos --dry-run

# work through the mismatches interactively
python ia_bulk.py reconcile-files --project sarasoldphotos
```

It reads the Sheet live, finds every row that *named* a file which
doesn't resolve, and proposes a correction for the ones it can — one row
at a time, nothing written until you press `[y]` or type one at `[e]`. A
row whose filename cell is blank is not yet catalogued rather than broken:
it is reported as a single count and never prompted about, so a run on the
full Sheet asks about the couple of hundred rows an operator can act on
rather than the ~2,900 that have no filename to correct. See
[`README.md`](../README.md#reconcile-files-correct-a-filename-cell-that-doesnt-match-the-drive)
for the prompt keys and exactly what it does and does not touch.

**Why before validate.** "Why this is a hard rule" below walks through the
real case this project already hit: of three rows that failed file
resolution in a nine-row rehearsal, two were real, unnoticed mismatches —
of two different kinds. `Finnis Meat Market.jpg` vs `Finnish Meat
Market.jpg` is a plain human typo. `Roy's Shell.jpg` vs ` Roy_s  Shell.jpg`
is not a typo at all: apostrophes and doubled spaces get mangled when
files are copied off source media, so a volunteer's transcription of that
one, correct by eye, still doesn't match the disk — no amount of care
would have caught it. `reconcile-files` needs both of its matching passes
because of that split: an exact match after normalization catches the
`Roy_s` kind, edit distance catches the `Finnis` kind. `validate`'s error
list and not-ready breakdown are only a useful signal if a row reported
broken is actually broken rather than mismatched in one of these two
ways, so working through both first is what makes the rest of
`validate`'s report worth trusting. See
[`docs/decisions/RECONCILIATION.md`](decisions/RECONCILIATION.md) for why
reconciliation only ever proposes a correction rather than applying one,
and why its exit code stays `0` while rows remain unresolved.

Safe to run repeatedly — a Sheet with nothing left to reconcile prints
`nothing to reconcile - every row with a filename resolves against the
drive` and does nothing else.

## 0b. Append rows for files that have no row (after reconciling)

The other half of the same job: once every row that names a file resolves,
files still unclaimed are genuinely uncatalogued, and this appends a
skeleton row — folder and filename cells only — for each of them.

```bash
# see what would be appended, grouped by folder
python ia_bulk.py append-rows --project sarasoldphotos --dry-run

# append for real
python ia_bulk.py append-rows --project sarasoldphotos
```

The order is not optional, and the tool enforces it: `append-rows` refuses
to run while any row names a file that does not resolve, because a typo'd
row and a missing row both present as "unclaimed file" — appending first
would create a duplicate row for every photograph a typo'd row already
means. Work through `reconcile-files` (§0) until it has nothing left, then
append. Safe to re-run: a second pass over an unchanged drive appends
nothing. See
[`README.md`](../README.md#append-rows--add-skeleton-rows-for-files-that-have-no-row)
for exactly what it writes and refuses.

> **Run this before every `upload`, every time.** It is not optional and it is
> not a first-run-only step. `validate` performs the *same* file resolution
> `upload` does, but uploads nothing and writes nothing, so every row it
> rejects is a row you fixed for free instead of discovering mid-run. See
> "Why this is a hard rule" below.

```bash
# against the project's test Sheet
python ia_bulk.py validate --project sarasoldphotos

# against the real Sheet (still uploads nothing and writes nothing)
python ia_bulk.py validate --project sarasoldphotos --live

# against an offline CSV instead of a Sheet
python ia_bulk.py validate --project sarasoldphotos --csv data/upload.csv --files-dir data
```

Exits `0` if every row passes, `1` otherwise. On the Sheet path `--live`
only chooses *which* Sheet is read; it never makes this command write or
upload anything.

Checks per row, Sheet path: `mediatype` is structurally required (it's
injected from the registry, so a blank one means the registry is wrong, not
the Sheet); each row's file is resolved against `files_dir` using
`file_template`; and, when present, `ia_identifier` matches
`COLLECTIONKEY-PROJECTID-NUMBER`, is registered in `projects_registry.json`,
and is unique. CSV path, unchanged: `identifier`, `file`, `mediatype`, and
`title` are all required and non-empty.

A human-filled field that is simply blank — no title yet, no filename yet —
is *not* an error on the Sheet path. It marks the row **not ready**, a
different question from whether the row is valid; see
[`docs/DECISIONS.md`](DECISIONS.md), "A blank cell is not an error". Which
fields count is the project's `required_for_upload` list in
`projects_registry.json` — normalized column names, e.g. `["title",
"theme"]` — plus the file-resolution outcome. The registry key is required
with no default, and every name in it is checked against the Sheet's actual
headers at startup, so a typo (`"titel"` for `"title"`) fails loudly instead
of quietly marking every row not-ready forever.

`validate`'s report marks a not-ready row inline, and rolls the rest up into
a per-field breakdown instead of listing thousands of identical rows:

```
[FAIL] row 7
    - no file found in '...' matching 'CD 1 01 53 34 2 Finnis Meat Market.jpg' (...)
[FAIL] row 41  (not yet catalogued)
    - no file found in '...' matching 'CD 1 02 11 38 4 Sunfower Dairy.jpg' (...)

2,898/2,900 rows passed

...

2,847 rows not yet catalogued
    2,100 missing title
    1,900 missing file_name
    1,900 missing folder_on_lacie_drive
      940 missing theme
    (a row missing more than one field appears in more than one count
     above, so these do not sum to 2,847)
```

Row 7 is genuinely broken (a filename that doesn't resolve) and is itemized
regardless of readiness. Row 41 carries the `(not yet catalogued)` marker
*and* an error — readiness and validity are different questions about the
same row, not alternatives, so both can be true at once. The great majority
of not-ready rows carry no error at all and are never itemized individually,
only counted in the breakdown above — a flat "N not yet catalogued" total
can't tell you whether the backlog is mostly missing filenames (automatable)
or mostly missing titles (it isn't); the per-field counts can.

Both halves of `file_template` appear in that breakdown. This project's is
`{folder_on_lacie_drive}/{file_name}`, so a row nobody has touched leaves two
blank cells and is counted once under each — which is why those two counts
track each other. The lines are ordered by count, highest first, ties broken
alphabetically.

`upload` deliberately does **not** repeat this breakdown on every run — see
[`docs/DECISIONS.md`](DECISIONS.md), "A blank cell is not an error". It
itemizes only the rows it would otherwise have uploaded (ready, but failing
validation) and gives the uncatalogued backlog one contained line instead:

```
[FAIL] row 7
    - no file found in '...' matching 'CD 1 01 53 34 2 Finnis Meat Market.jpg' (...)

1 row failed validation and will be skipped; the rest are uploaded, and this
command still exits non-zero so a partial run is never mistaken for a clean one

2,847 rows not yet catalogued (41 of them also have unresolvable filenames -
run `validate` to see them)
```

Only a row that was actually in this run's scope — ready, but broken —
affects `upload`'s exit code. A not-ready row was never going to be uploaded
regardless, so it does not flip the exit code; if it did, `upload` would
return non-zero on every run until all ~2,900 uncatalogued rows are filled
in, which trains an operator to stop trusting the exit code at all. See §2
below for `upload`'s modes.

It also prints a **field receipt** — every metadata field name the run would
create, plus the columns held back as `(LCPS Internal)`. Read it. This receipt
has caught two real Sheet typos before they shipped as permanent IA fields.

**What `validate` does not check** — the values inside your columns. A Sheet
with every metadata column shifted into the wrong field still passes. See
[`KNOWN-ISSUES.md`](KNOWN-ISSUES.md#1-noindex-cannot-be-changed-by-sync-metadata).

### Why this is a hard rule

On 2026-08-21 a nine-row rehearsal had three rows fail file resolution. One
was deliberate; two were real, unnoticed Sheet-vs-disk mismatches:

| Sheet said | Disk had | Difference |
|---|---|---|
| `…53 34 2 Finnis Meat Market.jpg` | `…53 34 2 Finnish Meat Market.jpg` | `Finnis` vs `Finnish` |
| `…53 50 01 Roy's Shell.jpg` | `…53 50 01  Roy_s Shell.jpg` | double space; `'` became `_` |

Both were caught, correctly — the tool resolves files by exact match then
case-insensitive stem match, and **never guesses** between near-misses. But
they were caught *during an upload run*, after other rows had already been
uploaded. A `validate` pass would have listed all three in about two seconds,
before anything happened.

The `Roy_s` case is worth understanding: apostrophes and doubled spaces get
mangled when files are copied off source media, so the Sheet cell a volunteer
typed by eye will not match the filename on disk. Expect this class of error
to recur across a 10,000-row collection.

## 2. Test run

```bash
# against the project's test Sheet (the normal path)
python ia_bulk.py upload --project sarasoldphotos

# ...and again, recording the minted identifiers in the test Sheet
python ia_bulk.py upload --project sarasoldphotos --write-identifier

# against an offline CSV
python ia_bulk.py upload --csv data/upload.csv --project sarasoldphotos --files-dir data
```

On the Sheet path, run it once without `--write-identifier` first: that mode
issues zero writes to the Sheet, so it is a rehearsal you can repeat freely.
`--dry-run` goes further and uploads nothing at all, printing the identifiers
it would mint and the cells it would write.

With no `--live`, the tool targets IA's `test_collection` sandbox and prepends
`zztest-<run's stamp>-` to each identifier before every network call — the
stamp is unique per invocation, so this run's items never collide with a
prior rehearsal's (see [`docs/DECISIONS.md`](DECISIONS.md), "Test identifiers
carry a per-run stamp"). The CSV keeps its real, permanent identifiers —
never hand-write a `zztest-` identifier.

Test items auto-expire after roughly 30 days. **Do not construct the URL by
hand from `zztest-<identifier>`** — the stamp makes that guess wrong, and
it will land you on a different (possibly already-darkened) run's item
instead of your own. Get the real URL from the run itself: each row's
progress line and the log's `uploaded_as` field both print the full stamped
identifier, and on the Sheet path the row's `ia_url` cell holds the exact
link once `--write-identifier` (or `--live`) has run. Spot-check a few of
those URLs in a browser and confirm the metadata fields are the ones you
meant, with the values you meant.

## 3. Live run

```bash
python ia_bulk.py upload --project sarasoldphotos --live

# ...or from an offline CSV, where --live must name the collection itself
python ia_bulk.py upload --csv data/upload.csv --project sarasoldphotos --files-dir data --live --collection sarasoldphotos
```

### Pre-live checklist

Nothing on this list is validated automatically. A wrong value here puts real
files in the wrong place under a permanent identifier.

- [ ] `projects_registry.json` → `collection_key` (currently `"lcps"`) is the
      first segment of every identifier this tool mints
      (`lcps-sarasoldphotos-00001`). `check_identifier` already refuses any
      row whose identifier prefix doesn't match this value, and any whose
      `PROJECTID` belongs to a different registered project — but the value
      itself has **never been confirmed** against how LCPS actually names its
      collection. This is a different thing from the IA collection uploads
      land in; see the next item.
- [ ] `projects_registry.json` → `ia_collection` (currently
      `"sarasoldphotos"`) is the actual Internet Archive collection
      `upload --live` uploads into on the Sheet path — taken from the
      registry automatically, never from a `--collection` flag there.
      **Do not confuse this with `collection_key` above; they are unrelated
      values, and it is a coincidence of spelling — not a code relationship —
      that `ia_collection` and the project id now share the same string.**
      `--collection` no longer has a `"lcps"` default: passing it
      on the Sheet path is now a hard error (the registry's `ia_collection` is
      the only source), and on the `--csv --live` path it is required with no
      default, refusing to run rather than guessing. What nothing in this
      tool does is confirm `ia_collection` **exists on archive.org** — that
      confirmation has to happen by hand, once, before the first `--live`
      run. **Done 2026-08-22**: `archive.org/details/sarasoldphotos` was
      checked by hand — it exists (title "Sara's Old Photos") and is already
      a child of the LCPS parent collection, `lcpsdigitalcollection`. Items
      are tagged into this subcollection alone; membership in the parent (and
      in `clatsopcountyhistoricalsociety` and `americana`) follows
      transitively, so listing multiple collections on each item is
      unnecessary. An earlier pass this same day recorded `ia_collection` as
      `lcpsdigitalcollection` itself — that collection is real and does
      exist, but it is the *parent*, not the subcollection the operator
      decided items should carry; this corrects it before any `--live` run.
      Re-check only if the registry value changes again.
- [ ] `validate --project <id> --live` was run **today, against the real
      Sheet, and exited 0** — not a validate of the test Sheet, and not
      yesterday's. See §1; this is the cheapest check on this list and the one
      most likely to find something.
- [ ] That same run's **"N rows ready to upload (no identifier yet)"** line was
      read, and N is the number you expect. **Exiting 0 no longer means "every
      row is ready"** — a Sheet of 2,900 uncatalogued rows exits 0 by design,
      because a not-ready row is waiting on data entry, not carrying an error.
      That count, not the exit code, is what tells you this run has the scope
      you think it has. It is the same set `upload` will plan, so an N that
      surprises you is worth resolving *before* anything permanent happens.
- [ ] The `ia` CLI is authenticated as `admin@lcpsociety.org` (`ia whoami`).
- [ ] The Sheet is current and saved — the rows you intend to upload were
      filled in, and no edit is still sitting unsaved or as a pending
      suggestion. A `--live` run reads the Sheet directly; there is no CSV
      export step to redo, and nothing local to go stale.
- [ ] A test run (**no** `--live`) over these same rows succeeded, and at
      least one resulting `zztest-…` item was eyeballed in a browser.
- [ ] The batch is under IA's daily limit — **see "Pacing" below, the tool does
      not enforce this.**

Identifiers are permanent. An item uploaded under the wrong identifier cannot
be renamed, only darkened by IA staff on request.

## 4. Corrections

```bash
python ia_bulk.py sync-metadata data/update-metadata.csv --project sarasoldphotos --live
```

Decoupled from upload and safe to re-run. Needs only `identifier` plus the
columns that changed. Blank cell = leave alone; literal `REMOVE_TAG` = delete
that field. `noindex` cannot be changed this way —
see [`KNOWN-ISSUES.md`](KNOWN-ISSUES.md#3-no-retry-on-transient-network-failures).

Both of the above are the `--csv` fallback. Normally the Sheet **is** the
correction — edit the cell, then:

```bash
python ia_bulk.py sync-metadata --project sarasoldphotos --dry-run
python ia_bulk.py sync-metadata --project sarasoldphotos --live
```

Every row marked uploaded is sent to the item its own `ia_url` cell names, so
nothing needs a log and rows uploaded by different runs are each targeted
correctly. See [`DECISIONS.md`](DECISIONS.md), "The Sheet is the correction".

**On the `--csv` path**, point `--from-log` at the log of the upload run whose
items you are correcting:

```bash
python ia_bulk.py sync-metadata data/update-metadata.csv --project sarasoldphotos --from-log logs/upload-20260823T161331Z.jsonl
```

The flag is **required** without `--live`: test items carry the stamp of the
run that created them, and that log's `uploaded_as` field is the only record
of which stamped item each row went to. Without it the command refuses rather
than sending corrections to identifiers that have never existed. See
[`DECISIONS.md`](DECISIONS.md), "`sync-metadata` reads its targets from the
upload log".

## Pacing and batch limits

IA's limits are **500 items per upload run** and **5,000 per day**.

`chunk_rows()` groups rows into batches of 500 by default, but the loop just
walks through them — there is still no sleep between batches. **Pacing across
a day's runs is manual, but the daily total is now enforced:** both `upload`
paths refuse to start a run of more than 5,000 items and name the fix.
Refusing rather than silently capping is deliberate — a run that quietly
stopped short would read as a complete one.

On the Sheet path, `upload --limit N` caps how many items a single
invocation uploads (counting rows actually ready to go out, not rows
scanned — see `README.md`), and `--chunk-size N` overrides the 500-item
batch size for that run. The two combine literally: `--limit 10
--chunk-size 3` uploads 10 items in batches of 3. Use `--limit` to pace
today's runs against the 5,000/day cap by hand, e.g. `--limit 2500` twice in
a day rather than one uncapped run. A run planning more than 5,000 items is
refused outright with `--limit 5000` named as the fix; `--allow-over-daily-cap`
overrides that, and is only correct if you know IA has raised this account's
cap. Both values are recorded in the run's
`run_header` log line (`ARCHITECTURE.md`, "Logging and resume") so a later
read of the log shows exactly what each run was capped at. If Internet
Archive's own rate limit shows up mid-run, the run now stops cleanly instead
of continuing to grind through failures — but that detection is best-effort
and unverified against a real response (`DECISIONS.md`, "Rate-limit
detection matches a status code..."), so treat `--limit` as the dependable
control and the detector as a bonus, not the other way around.

Neither flag exists on the `--csv` path (`run_rows()` has no per-chunk Sheet
write, and no ready/not-ready distinction, for either to mean anything
there): with ~10,000 photos on that path, split the CSV into day-sized files
yourself, or run it in sittings and rely on `--resume-from`. The 5,000/day
refusal does apply there — it counts rows left after `--resume-from`
filtering, so rows a previous run already uploaded do not count against
today's quota.

## Resuming a failed run

**A failing row prints why, as it happens**, indented under its own progress
line in the same style `validate` uses:

```
[3/8] uploading zztest-...-lcps-sarasoldphotos-00003 (SOP CD 1/CD 1 01 51 26 1.jpg)
    - Error retrieving metadata from https://archive.org/metadata/... ReadTimeoutError: read timeout=12
```

Long messages are collapsed to one line and truncated; the log below keeps
the complete text. That distinction matters most for the transient case above
— a read timeout is archive.org being slow, not anything wrong with the row,
and a rerun picks it up. Without the message on screen there is no way to tell
that apart from a real refusal such as `Access Denied`.

Every run also writes `logs/<command>-<timestamp>.jsonl`, one line per row:

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
python ia_bulk.py upload --csv data/upload.csv --project sarasoldphotos --files-dir data --resume-from logs/upload-20260712T125326.jsonl
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
