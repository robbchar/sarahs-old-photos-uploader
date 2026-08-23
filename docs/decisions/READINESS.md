# Readiness and errors

The difference between a row nobody has filled in yet and a row that is broken
— and what each command does about it.

One of the decision records indexed by
[`../DECISIONS.md`](../DECISIONS.md). Section titles here are cited verbatim
from code comments, so they are stable — if you rename one, grep for it
first.

## A blank cell is not an error

*Decided 2026-08-22.* The real Sheet has roughly 3,000 rows: about 2,900 carry
no metadata at all, around 100 are partly filled, and roughly 25 are complete.
Running `validate --live` against it buried the ~150–300 rows with a genuine,
fixable problem under nearly 2,900 identical "missing required column" errors
from rows nobody had touched yet — a nine-row rehearsal the day before showed
the same failure in miniature: three rows failed, in two genuinely different
ways, all reported identically.

**Blank means not-ready; present-but-wrong means invalid, regardless of which
field.** The governing principle: the tool only complains about assertions
someone actually made. Silence about a field is not an error; a wrong value in
it is. `RowValidation` carries this as `readiness`, a property derived from a
new `missing_fields` list rather than a second stored field — one source of
truth, so the two can never drift apart. Readiness is orthogonal to
`is_valid`,
not a replacement for it: a row can be not-ready *and* carrying errors at once
(an uncatalogued row whose filename also happens to be a typo).

**`validate` is loud and owns the data; `upload` is quiet and owns the run.**
`validate` itemizes every row carrying a broken assertion, ready or not, and
adds a per-field breakdown of the uncatalogued backlog. `upload` itemizes only
the rows it would otherwise have uploaded — ready rows that fail validation —
and gives the rest one line pointing back at `validate`, because a not-ready
row was never in this run's scope to begin with. The same reasoning settles
`upload`'s exit code: only a row that was actually in scope and failed flips
it. If an uncatalogued row did too, `upload` would return non-zero on every
run until all ~2,900 rows are filled in, which trains an operator to stop
trusting the exit code at all — `validate` is where that signal belongs.

**Test and live apply identical readiness rules.** No lowered bar for the test
Sheet, no opt-in flag to loosen it. Low-quality test *data* belongs in the
test
Sheet, which the operator already controls; a lowered *rule* would mean a
green rehearsal no longer predicts a green live run. This generalizes "Test
identifiers carry a per-run stamp" above: a rehearsal that behaves differently
from the real thing is not a rehearsal.

**Readiness is a field on the validation result, not a new `RowState`
member.** `RowState` (`UNASSIGNED`/`RESERVED`/`DONE`) answers "has this row
been minted and uploaded"; readiness answers "has a person filled it in". A
not-ready row *is* `RowState.UNASSIGNED` — the two are different questions
about the same row, not alternative states of it. A fourth `RowState` member
would have broken the invariant `plan_upload_targets` relies on, that every
row is exactly one of the three.

**Where the distinction is made, and why it must stay there.**
`resolve_sheet_files` deliberately blanks `row["file"]` when resolution fails,
so an unverified candidate can never survive to be mistaken for a real path
later. The cost is that afterward a blank row and a broken row are
indistinguishable — both are `row["file"] == ""`. Any readiness check reading
`row["file"]` at that point would misclassify a broken row (a typo'd filename)
as not-ready, and that misclassification runs in the one direction that
matters: a broken row demoted to "nobody has got to it yet" is a row nobody
ever fixes. So classification happens earlier, at candidate construction
inside `resolve_sheet_files` itself — the last point the raw candidate still
exists: a blank or whitespace-only candidate is recorded not-ready before the
resolver is ever called; a non-blank candidate that fails to resolve is a real
error, exactly as before.

**The partially-filled row.** A row with a blank folder cell but a filled-in
filename cell (or the reverse) routes to not-ready, naming only the specific
blank cell — not lumped in with a row nobody has touched at all. The operator
did assert a filename; the folder cell is simply a question not yet answered,
and a blank cell is a not-yet-answered question, not a wrong answer. The
per-field breakdown (`format_readiness_breakdown`) is what surfaces that
distinction to an operator deciding what to work on next, rather than folding
it into one flat, unhelpful total.

## On the Sheet path, `upload` uploads the valid rows and reports the rest

*Decided 2026-08-16. The CSV path keeps the opposite behavior.*

`upload` has always refused to run if any row failed validation. That is right
for a CSV somebody prepared by hand for one batch: the file is small, the
operator just built it, and a bad row means the file is wrong.

It is wrong for the Sheet. The Sheet is a living document of ~10,000 rows
filled
in over months, against an archive whose files arrive in instalments. One typo
in row 9,000 blocking the other 9,999 would mean the collection could never go
out incrementally — and during early testing, with only a handful of photos on
the machine, every unresolvable row would block the few that do resolve.

So on the Sheet path a run uploads every row that passes, lists every row that
fails with its errors, and exits non-zero so a failure is never mistaken for a
clean run. The `ia_uploaded` column already makes this safe: a skipped row is
simply one that has no confirmation yet, and the next run picks it up once its
file appears or its metadata is fixed.

The CSV path is unchanged and still refuses. The two sources have genuinely
different shapes and deserve different answers.

## A bad row is skipped; a bad header stops the whole run

*Decided 2026-08-16, alongside "upload uploads the valid rows and reports the
rest" — which is about rows, and says nothing about headers.*

Skipping works because a bad row is contained: it is one photograph, it is
named in the report, and the next run picks it up once fixed. A header defect
is not contained. Two headers normalizing to the same IA field name silently
overwrite one another on *every* row, so there is no subset of rows that is
still trustworthy — skipping the affected rows would mean skipping all of
them, and uploading the rest means uploading data that is already wrong.

So `upload` refuses outright on a header-level error and skips only row-level
ones. The split already existed inside `validate` (`header_results` vs
`row_results`); `upload` reuses that same partition rather than inventing a
second opinion about which is which.

## A malformed header is rejected, never auto-corrected

*Decided 2026-08-08, when `validate` was hardened.*

`check_header()` could strip stray whitespace and lowercase a `Date` column
instead of failing. It deliberately does not.

Auto-correcting means quietly deciding which field a value lands in — and
landing values in the wrong field is the exact failure the check exists to
catch. A tool that guesses right nine times teaches you to stop reading its
output before the tenth. Since IA metadata is permanent, the cheap outcome is
a
failed `validate` and a two-minute CSV edit; the expensive one is a silent
correction that was wrong across 10,000 items.

The same reasoning is why `check_row_shape()` treats a field-count mismatch as
an error rather than padding the row: `csv.DictReader` will happily fill the
gap
with `None`, but the gap means the header and the data disagree about column
positions, and nothing can tell you which one is right.
