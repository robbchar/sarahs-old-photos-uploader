# Reconciliation

How `reconcile-files` matches a Sheet's filename cell against what is
actually on disk, and why every match it finds stays a proposal until a
human accepts it.

One of the decision records indexed by
[`../DECISIONS.md`](../DECISIONS.md). Section titles here are cited verbatim
from code comments, so they are stable — if you rename one, grep for it
first.

## A correction is proposed, never applied

*Decided 2026-08-23, designing `propose_match()`.*

`propose_match()` returns a `Proposal` or `None` — it never writes to a row
itself, and it never guesses between two plausible answers. When either of
its passes turns up more than one candidate it raises `AmbiguousMatch`,
naming every match, instead of picking one. That is the same rule
`resolve_file()` already follows for the rest of this pipeline: "two files
sharing a stem is an error naming both, never a pick" — see
`FILES-AND-METADATA.md`, "A file is found by resolution, not by
constructing a path". An identifier is permanent once minted, so choosing
between two named files on the operator's behalf is the kind of silent
decision this project refuses everywhere it comes up — reconciliation is
just the newest place that rule applies.

`cmd_reconcile_files` keeps the same shape one level up. A proposal only
ever reaches the Sheet after `prompt_for_decision()` returns
`Decision(action="accept", ...)` — typed by a human at `[y]` or `[e]`.
`--dry-run` makes the boundary visible: it prints exactly the proposals a
real run would ask about and writes nothing at all, so "would propose" and
"did apply" are never the same event even by accident.

## Pass B requires identical digit sequences

*Decided 2026-08-23, after noticing that one of the real source folders
numbers its files instead of naming them.*

`SOP CD 2 COE`'s files are `001_seaside_beach.JPG`,
`002_westport_tunnel_by_Ford.JPG`, and so on — sequential numbers, not
descriptive names. That numbering is what makes edit distance dangerous
here: two files that share every character except one digit —
`001_seaside_beach.JPG` and a hypothetical `002_seaside_beach.JPG` — are
one Levenshtein edit apart, exactly the same distance as a genuine typo.
Edit distance alone cannot tell "the same photograph with a mangled name"
from "an entirely different, adjacently numbered photograph", and
confidently proposing the wrong one is worse than proposing nothing,
because a wrong filename resolves cleanly and never gets a second look.

`digit_runs()` pulls every run of digits out of a normalized name, in
order. Pass B only considers a candidate whose digit runs match the wanted
name's exactly; letters may differ within the edit-distance budget, numbers
may not. That keeps `finnis`/`finnish` (identical digits, letters one edit
apart) matchable while refusing `001_seaside_beach`/`002_seaside_beach`
(different digits) outright, however close their edit distance.

## A typed filename is resolved, not trusted

*Decided 2026-08-23, designing the `[e]` prompt key.*

An operator can type a filename directly instead of accepting a proposal or
leaving a row alone. That path is not a bare write to the Sheet — the typed
string goes through the same `resolve_file()` every other path in this tool
uses. A name that does not resolve to exactly one real file in the row's
folder is refused with `resolve_file()`'s own error and asked again; a name
that resolves to a file another row in this run already claimed is
refused too, with a message saying the file is already used by another
row (not which one — `claimed` is a set of paths, with no row number
attached to remember).

The reason is structural, not extra caution. `reconcile-files` exists to
fix rows where what a human typed does not match what is on disk — a prompt
that trusted a second human-typed string without checking it would reopen
exactly that defect, one keystroke removed. Routing it through
`resolve_file()` also makes typing more forgiving than it looks: a name
typed without its extension, or in the wrong case, still resolves, the same
way it would anywhere else in this tool — see `FILES-AND-METADATA.md`,
"A file is found by resolution, not by constructing a path".

## Reconciliation ships before append

*Decided 2026-08-23, scoping this work away from a second, deferred piece
of the same request: appending a new Sheet row for a photo file that has no
row at all.*

To the tool, "this file has no row" and "this file has a row with a
misspelled name" look identical — both are simply a file left in
`unclaimed`. An append step run first could not tell those cases apart, so
it would add a brand-new row for a photograph that already has one, leaving
the original, misspelled row still sitting on the Sheet. That is a
duplicate row for the same photograph, once for every unresolved row —
exactly the kind of misattribution this tool refuses to introduce anywhere
else.

Reconciliation has to run — and converge, fixing or explicitly leaving
alone as much as an operator will decide in one sitting — before an append
step can trust what remains unclaimed as genuinely orphaned files rather
than "orphaned, or maybe just misspelled". The ordering also happens to be
free: reconciliation only ever edits a cell that already exists on a row
that already exists, so nothing about it depends on append existing yet, or
ever.

## Exit code is 0 while work remains

*Decided 2026-08-23, reapplying an existing judgment about `validate` and
`upload` to a third command — see below.*

`reconcile-files` returns `0` even when rows remain unresolved at the end
of a run — a rejected proposal, a row left for later, or a `[q]` stop
partway through a large backlog are not failures, they are exactly what an
operator working a ~10,000-row collection over many sessions looks like.
The run still reports the count ("N row(s) still unresolved"); it just is
not what decides the exit code.

Only a run that could not do its job at all returns `1` — `read_sheet()`
never even reached the rows (the Sheet's ID is still the registry's
placeholder, the spreadsheet couldn't be read at all, or it had no data
rows), the Sheet has no column matching `file_template`'s name field, or
a write to it fails outright. Every one of those is a reason nothing here
can work around the problem, not that a row turned out to be broken. A
non-zero exit that persists for the weeks this backlog will realistically
take to clear teaches the operator to stop reading it — the same trap
`READINESS.md`'s "A bad row is skipped; a bad
header stops the whole run" already rejects for `validate` and `upload`,
reapplied here rather than relearned.
