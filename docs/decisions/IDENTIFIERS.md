# Identifiers

How a permanent identifier is formed, minted, reserved and protected. Internet
Archive can darken an item but never rename it, so almost everything here is
about not getting a number wrong once.

One of the decision records indexed by
[`../DECISIONS.md`](../DECISIONS.md). Section titles here are cited verbatim
from code comments, so they are stable — if you rename one, grep for it
first.

## Safety rail: prefix in code, never in the CSV

*Changed during the build (`bfd6c34`) after real usage — the earlier design was
the opposite.*

Originally, test runs required the CSV itself to contain `zztest-`
identifiers,
and a `check_live_safety()` function refused to run without them. That meant
keeping two CSVs, or editing identifiers back and forth between test and live
runs — exactly the kind of manual step that eventually ships a `zztest-`
identifier to production, permanently.

Now the CSV always holds the real, permanent identifier and
`effective_identifier()` prepends `zztest-` at call time unless `--live` is
passed. **The same CSV file is used unchanged for both test and live runs.**
`check_identifier` actively rejects a hand-written `zztest-` identifier, and
there is a test asserting exactly that.

## Test identifiers carry a per-run stamp

*Added 2026-08-19, after a rehearsal failed against the tool's own earlier test
items. Amends "Safety rail: prefix in code, never in the CSV" above.*

`effective_identifier()` prepended a bare `zztest-` to the real identifier. That
made a test run's identifiers a pure function of the real ones — and since a
fresh Sheet always starts minting at `00001`, every test run from a fresh
Sheet
produced exactly the same test identifiers as the last one.

Internet Archive never releases an identifier, and `test_collection` darkens
its
items after about thirty days. A darkened item cannot be uploaded to: the
attempt fails with `Access Denied - This item has been taken offline`. So the
first rehearsal permanently burned `zztest-lcps-sarahsoldphotos-00001`, and
every later rehearsal of row 1 collided with it. A July run burned `00001`
through `00009`; the next rehearsal, in August, could not upload a single row.

A rehearsal mode you can only use once is not a rehearsal mode. Test
identifiers
therefore carry a stamp unique to the invocation:

```
zztest-20260819t144907-lcps-sarahsoldphotos-00001
```

One stamp per run, so a run's items group together in a listing. `zztest-`
stays
the leading marker so a test item is still recognisable at a glance. Live
identifiers are untouched — they are the permanent ones and must remain a pure
function of the Sheet.

`--resume-from` is unaffected: `load_prior_successes` matches on the real
`identifier`, not on `uploaded_as`, so resuming still works across runs whose
stamps differ. The log's `uploaded_as` now records which stamped item a row
actually landed on, which is the question you ask when checking a rehearsal.

## Identifiers are minted by `upload` and written back to the Sheet

*Reversed 2026-08-08, before implementation. This section previously recorded
the opposite — "identifiers are assigned in the Sheet, never generated here" —
which did not match how the collection is actually maintained.*

An identifier's `COLLECTIONKEY` and `PROJECTID` halves come from configuration
(`projects_registry.json`). The `NUMBER` half is assigned by `upload` as it
goes and written back to the Sheet, so the Sheet ends up holding the permanent
identifier without anyone typing one by hand.

The batch is uploaded in chunks at different times rather than in one run, so
every run has to establish where the previous one stopped before it can mint
anything. The starting point comes from the Sheet at the start of the run —
the
highest existing `NUMBER` for that `PROJECTID`, plus one. Making that reliable
across interrupted and partial runs is not settled yet and is the open
question
in this design.

Unchanged by the reversal: identifiers are permanent once uploaded, never
reused and never renamed, which is why minting is worth being careful about at
all. The registry still exists so the prefix half can be checked against a
known list rather than a regex alone. Original filenames and donor folder
structure are still deliberately not part of the identifier; they belong in
`identifier-bib`.

## Minted numbers are re-checked against the Sheet before reserving

*Decided 2026-08-23.*

`plan_upload_targets()` mints the whole run's numbers up front, as `max+1`,
`max+2`, … over a single read — see "Identifiers are minted by `upload` and
written back to the Sheet" for why minting happens once rather than per chunk.
On a full-collection run that read can be hours old by the time the last chunk
reserves.

`split_moved_targets()` guards the reserve write, but it only inspects each
target's *own* row. A number written to a row this run is not targeting is
invisible to it — and that is precisely the case that mints a duplicate: two
Sheet rows carrying one permanent identifier. `internetarchive.upload()`
appends to an existing item rather than refusing, so the second upload puts a
second photograph inside the first one's item, unrenameably.

`check_claimed_identifiers()` closes it: `SheetSnapshot` now carries every
`ia_identifier` the fresh read holds, and the reserve leg compares this run's
minted numbers against all of them.

It stops the whole run rather than dropping the colliding target. Every number
this run holds came out of the same arithmetic over the same stale read, so
one collision means the maximum was wrong and the rest are suspect too —
reserving its neighbours would be reserving numbers that are wrong for the
same reason. Nothing has been reserved or uploaded at that point, so a rerun
re-reads, re-mints from the real maximum, and proceeds.

Only newly-minted targets are checked, and only before reserve. A RESERVED
row's identifier is already in the Sheet — that is what RESERVED means — and
after the reserve write so are this run's own, so checking either would stop
every ordinary run on its own writes.

## Tool-owned Sheet columns are all `ia_`-prefixed

*Decided 2026-08-16, after the first run against a real copy of the LCPS Sheet.*

The Sheet already had an `Identifier` column, holding the donor's original
reference (`CD 1 01 53 58 1 Central SS`). That normalizes to `identifier` —
which was the tool's reserved column for the minted IA identifier. The first
`upload` would have overwritten every one of those original references with a
freshly minted `lcps-sarasoldphotos-NNNNN`.

The tool's column is therefore renamed `ia_identifier`, joining `ia_uploaded`
and `ia_url`. Every column the tool writes now carries the `ia_` prefix, which
is both a consistent convention and a namespace the Sheet's existing headers
do
not collide with. The Sheet keeps `Identifier` meaning exactly what it always
meant.

This was found only by running `validate` against real data. No test would
have
caught it: the collision is between the tool's vocabulary and one particular
spreadsheet's, and nothing in the repo knew what that spreadsheet contained.

## The four `ia_` columns are required in every mode, including the safe one

*Decided 2026-08-16, when `upload` first wrote to a Sheet.*

`upload` needs somewhere to put `ia_identifier`, `ia_uploaded`, `ia_url` and
`ia_identifier_bib`. The obvious rule — require the columns only when actually
writing — makes the default, no-`--write-identifier` mode succeed against a
Sheet that `--live` would reject. That is worse than useless: the whole point
of the default mode is to be a rehearsal, and a rehearsal that passes where
the performance fails is a false negative on the one run an operator trusts.

So the check does not vary with the mode, and it runs before anything is
uploaded rather than after — a run that uploaded first and only then noticed
it had nowhere to record the identifier would produce exactly the stranded
item the reserve-first ordering exists to prevent.

## `identifier-bib` is written back to the Sheet, not just generated

*Decided 2026-08-16, extending the decision below.*

`identifier-bib` was to be generated at upload time from the resolved file
path and never recorded locally. It is now also written to an
`ia_identifier_bib`
column, so the value that went to Internet Archive is reviewable in the Sheet
alongside `ia_uploaded` and `ia_url` rather than being inferable only from a
log.

Its value is the donor's original location: the folder column joined to the
filename column (`File on Array` + `/` + `Identifier`). Those are two separate
columns in the real Sheet, which is why the path is assembled rather than read
from one place.

Note this is *not* necessarily the path used to open the file on disk. In the
real Sheet, 225 of 234 filenames carry no extension, so the recorded reference
and the resolvable path differ. See `file_template` for the latter.

## An identifier is checked against the run's project, not the whole registry

*Decided 2026-08-29, closing issue #2.*

`check_identifier` verified that an identifier's `COLLECTIONKEY-PROJECTID`
half named *some* project in `projects_registry.json`. It never compared
`PROJECTID` against the `--project` the run was invoked with, so a row
holding `lcps-otherproject-00099` validated clean and would have uploaded
under that identifier on a `--project sarasoldphotos` run.

The bug was latent only because one project is registered. The registry
exists precisely to support a second — and the failure it would produce is
the worst shape this tool has: quiet, and permanent. An item filed under
another project's numbering cannot be renamed afterwards, and nothing
downstream would have flagged it.

The prefix is now checked twice, and the two failures are reported
differently on purpose:

- prefix names no registered project → `prefix 'lcps-nosuchproj' not found
  in project registry`. The identifier is wrong.
- prefix names a *different* registered project → `identifier
  'lcps-otherproject-00099' belongs to project 'otherproject', but this run
  is --project sarasoldphotos`. The `--project` flag may be the thing that
  is wrong.

Collapsing those into one message would send an operator to edit the Sheet
when the fix is the command line, or the reverse.

`project_id` is a **required** parameter all the way down
(`check_identifier`, `validate_rows`, `validate_csv_rows`,
`validate_sheet_rows`, `validate_identifiers`, `plan_sync_targets`) rather
than one defaulting to anything. A default is how a future call site
silently re-acquires this bug, and it costs nothing to spell out: the Sheet
paths already hold `config.project_id`, and the CSV paths already hold
`args.project`.

### The `--csv` paths gained an unknown-project guard with it

`--project` is `required=True` on every subcommand, but the `--csv` paths
never built a `ProjectConfig`, so they never inherited
`load_project_config`'s check that the named project actually exists. That
cost nothing while `--project` went unread there. Once every row is compared
against it, a typo'd `--project` fails *every row* with "belongs to project
'astoriaphotos', but this run is --project astoriaphoto" — true, useless,
and repeated once per row, naming the wrong file to go fix. So all three
`--csv` entry points now refuse an unregistered `--project` up front, with
the same wording the Sheet paths already used (`unregistered_project_error`
in `project_config.py`).

### `sync-metadata` on the Sheet path checks the item, not a column

The Sheet path never runs `validate_identifiers`: it corrects rows that
already uploaded, and the item it sends to is whatever `ia_url` records, not
whatever an identifier column says. So the check is made against that item
instead, in `plan_sync_targets` — a row whose `ia_url` was pasted from
another project (or copied in with the row) is reported as a problem and
skipped rather than corrected.

This is the highest-stakes instance of issue #2 and the reason the check
could not simply be left to the row-validation helpers: everywhere else a
wrong-project identifier misfiles *this* project's item, but here it
overwrites *another* project's metadata. Test items are read through their
stamp (`item_project_id`), since `ia_url` in test mode records
`zztest-<stamp>-<identifier>`.
