# The Sheet protocol

Reading the Sheet live, the reserve → upload → confirm ordering, the guards
against a Sheet edited mid-run, and how corrections get back out.

One of the decision records indexed by
[`../DECISIONS.md`](../DECISIONS.md). Section titles here are cited verbatim
from code comments, so they are stable — if you rename one, grep for it
first.

## The Sheet is read live; the CSV becomes the offline path

*Decided 2026-08-08, before implementation.*

`validate` and `upload` read rows from the Google Sheet over the API rather
than from a hand-exported CSV. Two reasons, in order of weight.

`upload` has to hold a Sheet connection anyway in order to write identifiers
back. Reading from the same place removes the "which copy is current" question
outright instead of adding a dependency to answer it.

And the export step was itself a source of defects. The traps documented in
[`CSV-PREPARATION.md`](../CSV-PREPARATION.md) — above all a comma inside an
unquoted header splitting one column into two and shifting every field after
it
— are artifacts of CSV *parsing*, not of the data. The Sheets API returns
cells
as a grid, so a header containing a comma is just a header containing a comma.
That failure mode is structurally absent on this path.

The CSV path stays for offline and dry-run work and keeps its own header
validation, since the traps are real for anything hand-prepared.

## A row's identity is its `file_template` columns, not its `ia_identifier`

*Decided 2026-08-16, after review found the first version of the mid-run-edit
guard was checking its own write.*

The guard's first version compared `ia_identifier` at the target row against
the value the run reserved. That is tautological on the reserve→confirm leg:
reserve had written that exact value at that exact index moments earlier, so
the check could only ever pass. It also ran only before the confirm, leaving
the read→reserve window — which is the *whole run*, since row numbers are
fixed by one initial read and the last chunk's reserve write happens hours
later — entirely unguarded. A row deleted in that window shifts a completed
row up into a target's position, and the reserve write overwrites a permanent
identifier and its live archive.org URL, silently, exit 0.

A row's identity has to be something this tool never writes, or the check is
circular. The `file_template` columns are exactly that: they are the
operator's
own data, the tool only ever reads them, and they are already in memory per
row. So the fingerprint is the row's template candidate, captured from the raw
cells before resolution rewrites them, and it is re-checked before **both**
writes. `ia_identifier` is still checked alongside it — blank for a row about
to be reserved, ours once reserved — because it catches a different thing:
somebody else claiming the row.

The residual gap was two rows whose template columns are identical — the
"two rows resolving to the same photograph" case tracked as issue #1. **Its
consequence was a misattributed write, not a withheld one.** With two
UNASSIGNED rows pointing at the same file and a row deleted above them, the
fingerprints still matched after the shift, so the guard passed: the item
uploaded carrying one row's metadata while the identifier, timestamp, URL
and bib landed on the *other* row, leaving the first unassigned and due to
be re-minted next run. Closed on 2026-08-29 — see "A fingerprint only proves
identity while it is unique" below.

## The Sheet is the correction

*Decided 2026-08-23.*

`validate` and `upload` moved to reading the Sheet live; `sync-metadata` did
not, and was the last command still requiring a hand-made CSV. That left the
round trip the whole "Sheet is the source of truth" premise promises — fix a
description in the Sheet, have it show up on the site — impossible without
exporting a CSV by hand first. Worse, `--csv` was a required POSITIONAL, so
`sync-metadata --project X` did not fail informatively; it printed an argparse
usage error about a missing `csv`.

The Sheet already held everything needed. `upload`'s confirm write records
`ia_uploaded` (this row is done) and `ia_url` (the item it became), and
`ia_url` carries the per-run `zztest-` stamp in test mode. So the Sheet path
needs no log, no CSV, and no stamp arithmetic: read `ia_url`, send that row's
current columns. It handles a Sheet whose rows were uploaded by *different*
runs, under different stamps, without knowing that happened.

Four choices:

- **Scope is `RowState.DONE` and nothing else.** An UNASSIGNED row has no item
  to correct; a RESERVED row's upload never confirmed, and `upload` already
  retries those.
- **Every DONE row is sent, every run.** Internet Archive answers *no changes
  to `_meta.xml`* for an item that already matches, which becomes
  `MetadataUnchanged` and is counted as `unchanged`. That makes a full sync
  idempotent by construction, so there is no need for per-row change
  detection, a fifth `ia_` column, or a second place for the Sheet and the
  item to drift apart.
- **A blank cell means "leave this field alone", as on the `--csv` path.**
  Treating the Sheet as literally canonical — blank means delete — is more
  faithful in principle, but an accidental clear, a bad paste, or a row shift
  would silently strip metadata from a permanent public item with no undo.
  `REMOVE_TAG` deletes, deliberately and visibly, and one rule holds across
  both paths.
- **Mode mismatches are refused, not reported afterwards.** The Sheet is
  itself the mode boundary (test and live are different spreadsheets), so an
  `ia_url` pointing the wrong way means the wrong Sheet is in the registry or
  someone pasted across. Sending a live correction to a rehearsal item, or the
  reverse, is not fixed by rerunning.

`sheet_metadata_fields()` is shared with `upload`, so a column that uploads
but does not sync — or the reverse — cannot exist.

## `sync-metadata --csv` reads its targets from the upload log

*Decided 2026-08-23, closing a defect introduced by the per-run stamp above.*

"Test identifiers carry a per-run stamp" solved rehearsal collisions on
`upload` and silently broke `sync-metadata`, which nothing noticed because
that command had never been run.

`cmd_sync_metadata` derived its target the same way `upload` does, by calling
`effective_identifier()` with `run_stamp()`. But a stamp is unique to the
invocation, so a correction run computed `zztest-<today's stamp>-<identifier>`
for an item created under *yesterday's* stamp — an identifier that has never
existed. Every row would have failed, with a message about the item not being
found rather than about the real cause. Test-mode `sync-metadata` was
unusable, which left `--live` as the only way to exercise it: the worst
possible place to run something for the first time.

The mapping already existed. Every upload log line records both `identifier`
(the real, permanent one) and `uploaded_as` (what actually went over the
wire), precisely so a later reader can tell what landed where. `--from-log`
points at that log and `load_uploaded_as()` reads the pairs out of it.

Three choices went into the shape:

- **Required in test mode, optional with `--live`.** Live identifiers are
  unstamped, so there is nothing to look up. In test mode there is no value
  the CSV could carry that would work — the operator is told never to author a
  `zztest-` identifier by hand, and `check_identifier` would reject one
  anyway — so refusing is the only honest answer.
- **A miss is an error, never a fall back.** Recomputing the target when the
  log does not name a row is exactly the bug being fixed, and it fails
  silently. Rows the log does not record are reported before anything is sent,
  all-or-nothing like the rest of the CSV path.
- **Mode-filtered, like `--resume-from`.** A test log records where a
  `zztest-` item went and says nothing about the real one. Both flags read
  logs through the same `_read_log_results()`, which drops entries from the
  other mode and tolerates a truncated line.

`--from-log` is deliberately separate from `--resume-from` rather than folded
into it: they read the same file for opposite purposes. `--resume-from` says
which rows to SKIP; `--from-log` says where the rows that remain should be
SENT. A single flag doing both would make "skip what is done" and "correct
what is done" the same instruction, which they are not.

## `--resume-from` filters on run mode

A test-mode success and a live-mode success are indistinguishable by
identifier — only `uploaded_as` differs. Without a mode check,
`--live --resume-from <test-log>` would skip every row as "already done" and
report a successful live run that uploaded nothing.

So `load_prior_successes()` matches on the log's `live` field. Log lines
written before that field existed record no mode and match **neither**, so old
logs simply never skip anything rather than skipping in the wrong direction.

## A fingerprint only proves identity while it is unique

*Decided 2026-08-29, closing issue #1 — the misattributed-write gap the
"row's identity is its `file_template` columns" note above deferred.*

The guard's fingerprint says "the row at this position still describes the
same photograph". That inference silently assumes no *other* row carries the
same fingerprint: with two rows resolving to one file, a row shift leaves a
matching fingerprint at the target's position while the physical row
underneath is a different one, and the write-back lands on the wrong row —
the item uploads under one row's metadata, the identifier and URL are
recorded on the other, and the first row stays unassigned and is minted a
*second* permanent identifier next run.

Issue #1's fix direction asked for a row identity that does not depend on
`file_template` being unique. Every candidate for such a key fails the
section-above constraint that identity must be something this tool never
writes (a hidden key column is the tool's own write; a wider fingerprint
over the metadata columns just moves the same collision one duplicate-row
away). So instead of finding a key that survives duplicates, duplicates
themselves are refused — which they deserve on their own merits, since two
rows claiming one photograph is exactly the "duplicate row mints a second
permanent identifier" case `reconcile-files` already refuses to create. Two
layers, because duplicates have two ways in:

- **Present at the initial read: refused by `resolve_sheet_files()`.** Rows
  resolving to the same file are all errors — every row in the group, not
  all-but-the-first, because the tool usually cannot know which row is the
  wrong one and flagging the rest would silently elect a winner. It *can*
  know in one case: a group holding exactly one row that has already
  uploaded. That row's identifier is permanent and its row is the only link
  between the identifier and its metadata — including the `ia_url`
  `sync-metadata` reads to find its targets — so it is named as the one to
  keep and only the others are offered for deletion. (Two uploaded rows in
  one group is a worse problem than this check can adjudicate, so it falls
  back to the symmetrical wording.) Keyed on
  `claim_key()` of the *resolved* path, the same key the reconcile survey
  uses: the resolver is deliberately forgiving (case, extension), so two
  rows can spell one disk file differently and a raw-cell comparison would
  miss them. Shared by `validate` and `upload`, so the pair shows up as
  blocked rows long before a live run.
- **Introduced mid-run: the guard distrusts a duplicated fingerprint.**
  `split_moved_targets()` counts each fingerprint across the fresh read; a
  target whose fingerprint appears on more than one row is filed as moved —
  the safe direction, skip and report — because position plus a non-unique
  fingerprint proves nothing. It goes out on a rerun once the Sheet is
  untangled.

  **On the reserve leg only.** After reserve, the target's row carries this
  run's own number, and `check_claimed_identifiers()` has already proved
  that number unique across the whole Sheet — so the `ia_identifier`
  comparison is by itself a complete proof of identity, since a shift puts a
  row that does *not* carry our number underneath. Applying the ambiguity
  veto to the confirm leg as well only produced false positives, and the
  cheapest of them was expensive: a volunteer appending a duplicate row
  (which shifts nothing) between the upload and the confirm write left a
  live Internet Archive item recorded nowhere, with the next run's own
  duplicate refusal then blocking the very row that needed finishing.

Together the original inference is made sound: a write proceeds only when
the fingerprint at the target's position matches, was unique in the initial
read (validation — a duplicate there never becomes a target), is unique in
the fresh read *or* the row already holds this run's proven-unique number
(guard), and the `ia_identifier` cell holds exactly what the protocol step
expects. A pure row shift can no longer satisfy all of that against the
wrong physical row.
