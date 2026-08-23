# Quota, pacing and the run record

Internet Archive's limits, how a run paces itself against them, and what each
run writes down about itself.

One of the decision records indexed by
[`../DECISIONS.md`](../DECISIONS.md). Section titles here are cited verbatim
from code comments, so they are stable — if you rename one, grep for it
first.

## `--limit` counts planned targets, not Sheet rows; `--chunk-size` overrides `CHUNK_SIZE`

*Decided 2026-08-22.*

`--limit` is applied to `plan_upload_targets()`'s own output — after it has
already filtered to rows that are valid, ready, and not already done — never
to raw Sheet rows. The real Sheet is ~3,000 rows, the great majority
uncatalogued (see "A blank cell is not an error"); "stop after scanning N
rows" and "stop after uploading N rows" are very different numbers on a Sheet
shaped like that, and only the second is useful to an operator pacing
against the 5,000/day cap. `--limit 100` on a Sheet with 2,900 uncatalogued
rows and 150 ready ones uploads 100 of the 150, never the first 100 read.
It combines with `--chunk-size` as "this many total, batched this way", not
"this many chunks": `--limit 10 --chunk-size 3` uploads 10 items in batches
of 3, never 10 batches of 3.

Targets sliced away by `--limit` cost nothing: `plan_upload_targets` still
mints an identifier for every pending row up front (minting is pure
arithmetic — see its own docstring), but a target `--limit` drops is never
reserved, so its number is never written anywhere and `next_identifiers()`
mints it again on the next run. No permanent identifier is spent on a row
this run chose not to touch.

`--chunk-size` makes `SheetUploadRun.execute()`'s `CHUNK_SIZE` read (a
module constant previously overridable only by hand-editing it, or by
`monkeypatch.setattr("ia_bulk.CHUNK_SIZE", ...)` in a test) an ordinary
per-run flag, defaulting to `CHUNK_SIZE` (500, IA's per-run cap) so every
existing chunk-boundary test keeps working unchanged. The operator had
already been hand-editing the constant to rehearse the reserve/upload/confirm
protocol across a chunk boundary; this makes that a flag instead.

Both flags are Sheet-path only, rejected on `--csv` the same way
`--write-identifier`/`--dry-run` are rejected there: `run_rows()` (the `--csv`
path) has no reserve/confirm batching for `--chunk-size` to control, and no
ready/not-ready distinction for `--limit`'s counting promise to mean
anything. Both are recorded in the `run_header` log record (see
`ARCHITECTURE.md`, "Logging and resume") so a run that stopped at `--limit`,
or used a non-default `--chunk-size`, stays reconstructable from its own log
alone.

## A run may not exceed Internet Archive's daily item cap

*Decided 2026-08-23.*

`.claude/CLAUDE.md` states IA's limits as "500 items per upload run,
5000/day". `CHUNK_SIZE` covered the per-run half from the beginning. Nothing
covered the daily half on either path: a bare `upload --live` on a fully
catalogued Sheet planned every ready row, and the only thing between it and a
throttled half-finished batch was rate-limit detection that has never been
observed against a live response.

Both paths now refuse a run over `DAILY_ITEM_CAP` and name the fix. Three
choices went into that:

- **Refuse rather than silently cap.** Defaulting `--limit` to 5,000 would
  make an over-cap run stop short and still report cleanly, which reads as a
  complete run. That is the same trap the `--limit <= 0` guard already exists
  to avoid.
- **Check after the `--limit` slice.** A Sheet holding more ready rows than
  the cap is not itself an error; running at all of them in one day is. So
  `--limit` is the ordinary way through, not a special case.
- **An explicit override, not none at all.** The cap is IA's, not ours, and an
  account whose cap has been raised is a real case — but it has to be stated.
  `--allow-over-daily-cap` is deliberately verbose enough not to be passed by
  reflex.

It applies in test mode too. A rehearsal uploads to `test_collection` through
the same account and spends the same quota.

## Rate-limit detection uses a parsed status code, never message text

*Decided 2026-08-22. Revised 2026-08-22 after review found the first version
could false-positive.*

`is_rate_limit_error()` looks **only** at a parsed status-code integer,
never at `str(exc)` or any server-supplied text. The first version of this
function scanned `str(exc)` for `"status 429"`/`"status 503"` substrings;
review found that unsafe in both directions and it was replaced, not merely
tightened:

- a 404 (or anything else) whose body happens to mention `"status 503"` — a
  mirrored error, a proxied message, an echoed request — misclassified as a
  rate limit and would have wrongly stopped the whole run.
- a plain substring test also matches `"status 5031"` or `"status 42900"` —
  digits that merely *contain* 503/429, not equal to them.

A false positive here is worse than a miss: it halts a batch mid-flight, on
a command that creates permanent items, for a reason that is not real. The
replacement checks two structured sources, both verified by reading source
rather than guessed:

1. **`UploadFailed.status_code`** — a new exception (subclassing
   `RuntimeError`, so the pre-existing `pytest.raises(RuntimeError, ...)`
   caller keeps working) that `upload_row()` raises in place of a bare
   `RuntimeError`, carrying the real, parsed `response.status_code` as a
   structured attribute rather than only embedding it in the message text.
2. **`exc.response.status_code`** —
   `requests.exceptions.RequestException.__init__` (the base of
   `HTTPError`) stores whatever `Response` it is given as `.response`
   (`self.response = kwargs.pop("response", None)`, confirmed by reading
   `requests`' own source, not assumed). Tracing the installed
   `internetarchive` 5.10.1's `Item.upload_file()` — the method
   `upload_row()` actually reaches via `internetarchive.upload()` — shows
   that on a real S3 failure it catches the resulting `HTTPError` and
   re-raises via
   `raise type(exc)(error_msg, response=exc.response, request=exc.request)`.
   The **message** there is rebuilt from the S3 XML body's `Message`/
   `Resource` elements (`get_s3_xml_text()` in `internetarchive/utils.py`)
   and loses the numeric status and the S3 error `Code` (e.g. `SlowDown`)
   entirely — but `response=exc.response` is passed through **unchanged**,
   so `.response.status_code` still holds the real, original status even
   though the text does not.

That second finding reverses what the first version of this decision said:
a live rate limit surfacing through the real library's own exception *is*
catchable, just not by reading its message — it has to be read from the
structured `.response.status_code` the library itself preserves. There is
no text-based fallback: neither exception this codebase's upload path can
raise lacks a structured status (see both sources above), so the rate-limit
decision never depends on server-supplied text at all.

503 is Internet Archive's documented S3 overload signal (the installed
`internetarchive` 5.10.1's own `ia upload --retries` help text: *"Number of
times to retry request if S3 returns a 503 SlowDown error"*); 429 is not
IA-upload-specific documentation, but `session.py`'s default urllib3 `Retry`
`status_forcelist` (`[429, 500, 501, 502, 503, 504]`) shows the library's own
authors also treat it as rate-limit-adjacent.

No `--live` run has ever happened, so no real rate-limit response has ever
been captured — both structured sources above are verified against the
installed library's *source*, not against actual IA behavior. A missed
detection (an exception with neither attribute, or a genuinely different
status) is the safe failure direction: the row is logged as one ordinary
failure and the run continues, same as any other transient error, whereas a
false match aborts a run mid-flight over an unrelated error. `--limit`
(above) remains the operator-controlled fallback either way.

## Every recorded timestamp is UTC

*Decided 2026-08-23.*

`run_stamp()` already used `time.gmtime()`, and said why: local time repeats
an hour during the DST fall-back transition, so two rehearsals started an hour
apart across it would mint the same stamp. Exactly the same is true of the
values that outlive the run — the `ia_uploaded` cell, which is the permanent
record of when an archival item was published, and every line of the log that
`--resume-from` and any later audit read. Both were naive local time, with no
offset recorded to reconstruct the real instant from afterwards.

All of them now go through `utc_timestamp()`: ISO-8601 UTC with an explicit
`Z`. The `Z` is not decoration — without it the string is ambiguous, and the
ambiguity is only resolvable by knowing which machine wrote it and what its
clock was set to that day. `open_log()`'s filename stamp is UTC for the same
reason, so a directory listing sorts in the order the runs actually happened.

## "Unchanged" is a third outcome, not a failure

IA returns HTTP 400 with `{"error": "no changes to _meta.xml"}` when a
metadata
update matches what is already on the item. Nothing is wrong; there was just
nothing to do. Treating that as a failure would inflate the error count and
flip the exit code on a re-run of a correct CSV — which matters because
`sync-metadata` is meant to be safe to run repeatedly.

`update_metadata_row` detects that exact error string and raises
`MetadataUnchanged`, which `run_rows` counts and logs in its own bucket.
