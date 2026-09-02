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

*Amended 2026-09-02:* that last claim had one hole — the metadata GET inside
`internetarchive.upload()`, whose status the library stripped before it
reached `is_rate_limit_error()`. Closed structurally rather than by relaxing
the rule; see
["A status the metadata call strips is recovered, still without reading text"](#a-status-the-metadata-call-strips-is-recovered-still-without-reading-text).

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

## Retry covers transport failures, never refusals

*Decided 2026-09-02, closing issue #5.*

A transient failure talking to archive.org used to fail that row for the
whole run — observed as a `ReadTimeout` on `archive.org/metadata/...` with
nothing wrong with the data, the Sheet or the file. Across ~10,000 items, at
least one such failure is close to certain, and the recovery was correct but
manual: the row was never marked done, so the operator re-ran to chase a
flake a retry would have absorbed.

`retry_ia_call()` now wraps the network call inside `upload_row()` and
`update_metadata_row()`. Both run loops — the Sheet path's `SheetUploadRun`
and the CSV path's `run_rows()` — get it without knowing it exists.

**The gap this actually closes is the S3 transfer, not the metadata read.**
Reading the installed `internetarchive` 5.10.1's source rather than assuming:
`ArchiveSession.__init__` mounts a retrying adapter — urllib3
`Retry(total=3, connect=3, read=3, backoff_factor=1)` — but **only on
`archive.org`**, and deliberately not on the S3 host (`session.py`: *"Don't
mount on s3.us.archive.org, only archive.org! IA-S3 requires a more
complicated retry workflow"*). So metadata reads and `modify_metadata` POSTs
already had three transport attempts, and the timeout in the issue is what a
run looks like *after* those. The file transfer — the slowest call this tool
makes, minutes long for a 10 MB photograph on a domestic link — had none.
`Item.upload_file()`'s own `retries` argument would not have closed it
either: it defaults to `retries or 0` and only ever fires on a 503.

**What is retryable is decided by a parsed status, never by message text** —
the same rule, through the same `parsed_status_code()` helper, that
["Rate-limit detection uses a parsed status code, never message text"](#rate-limit-detection-uses-a-parsed-status-code-never-message-text)
established:

| Failure | Retried | Why |
| --- | --- | --- |
| `ConnectionError`, `Timeout` (incl. `ReadTimeout`) | yes | No response completed; nothing says the next attempt fails too |
| 500, 502, 504 | yes | Server-side and not a refusal of *this* request |
| **429, 503** | **no** | Already answered by something stronger — see below |
| 403, 400, 404, any other 4xx | no | A refusal is repeatable; retrying only lengthens the walk to the same answer |
| `ValueError`, `RuntimeError`, `MetadataUnchanged`, anything unrecognized | no | This file's own guards, and a bug is not made truer by a second attempt |

**429 and 503 are deliberately excluded.** They are Internet Archive saying
"slow down", and this tool already answers that with more than a retry:
`is_rate_limit_error()` stops the whole run after the current chunk's confirm
write so the operator resumes tomorrow. Retrying them here would delay that
stop for every rate-limited row while making the overload marginally worse.
The alternative — a short backoff before falling through to the stop, which
is what `ia upload --retries` does — was considered and rejected as
re-litigating a decision already made and documented.

Three attempts, with **equal jitter**: half of a ceiling that doubles each
time (2s, then 4s, capped at 30s), plus a random share of the other half.
The randomness matters because a chunk's rows fail in lockstep when
archive.org is briefly unwell and a fixed backoff would send them all back at
the same instant; the fixed half matters because full jitter can draw a delay
near zero, and a wait that does not wait cannot outlast the slowdown it
exists for. Three rather than more because a row that fails all three is
logged as an ordinary failure and picked up by the next run — re-running is
already the supported recovery, and a failed attempt never burns an
identifier.

**Retrying is safe for both wrapped calls.** `upload_row()` passes
`checksum=True`, so Internet Archive skips a file whose MD5 already matches
the item's: a retry after a timeout that had in fact landed re-sends nothing
and creates no duplicate. A metadata update is a full statement of the fields
to set, not an increment, so applying it twice lands where applying it once
does. In neither case can a retry mint a second identifier — the target is
chosen before the retried function is reached.

The final attempt's exception is re-raised **unchanged**, not wrapped: every
caller's `except Exception` branch logs `str(exc)` and hands the object to
`is_rate_limit_error()`, so wrapping would both change what the log records
and hide the parsed status the run-stopping decision reads.

`fetch_current_metadata()` is deliberately **not** wrapped. It already sits
behind the library's three retries, it already returns `None` rather than
raising so that a dry run which cannot reach one item still reports the other
9,999, and retrying it would make a 10,000-row dry run crawl.

There is no `--retries` flag. The constants are `RETRY_ATTEMPTS`,
`RETRY_BASE_SECONDS` and `RETRY_MAX_SECONDS` in `ia_bulk.py`; a flag can be
added if a real run on the LCPS link wants one.

## A status the metadata call strips is recovered, still without reading text

*Decided 2026-09-02, alongside the retry work above.*

`internetarchive.upload()` reads an item's metadata before transferring
anything, so a rate limit can surface from that GET rather than from S3. But
`session.get_metadata()` catches every failure and re-raises
`type(exc)(error_msg)` — a fresh exception of the same class, built from the
message alone — which drops `.response`, the only structured status
[the rule above](#rate-limit-detection-uses-a-parsed-status-code-never-message-text)
allows us to read. A 503 there was invisible: logged as one ordinary failure
while the run ground on through the rest of the chunk repeating it.

Probing a local server that answers real status codes, rather than reasoning
about the library, showed the gap was **wider than a lost `.response`**:

| Status | What reached `ia_bulk` before | Status readable? |
| --- | --- | --- |
| 429, 500, 503 | `requests.exceptions.RetryError` | no — no `Response` was ever produced |
| 400, 403, 404 | `requests.exceptions.HTTPError`, `.response is None` | no — stripped by the re-raise |

The 5xx row is the surprising one. Those statuses are in the adapter's
`status_forcelist`, so urllib3 retries them three times and then raises
`MaxRetryError`, which requests turns into a `RetryError`. No `Response`
object exists anywhere in that path, so no amount of unwrapping recovers a
status.

**Two changes, both structural, neither reading message text:**

1. **`IA_RETRY` sets `raise_on_status=False`** and is passed to
   `upload()`, `modify_metadata()` and `get_item()` via
   `http_adapter_kwargs`, which every one of them forwards to
   `get_session()`. urllib3 still makes the same three attempts against the
   same forcelist with the same backoff — only the give-up changes: the final
   `Response` is *returned* rather than raised through, so it meets
   `get_metadata()`'s own `raise_for_status()` and becomes an ordinary
   `HTTPError` carrying that response. Every other field of the policy is
   copied from `internetarchive` 5.10.1's `mount_http_adapter()`, and a test
   pins that equality against a session **the library itself builds**, so a
   future version changing its defaults fails loudly instead of silently
   altering what we retry.
2. **`parsed_status_code()` walks `exc.__context__`.** Python's implicit
   chaining still holds the *original* exception, `.response` intact, behind
   the stripped copy. The outermost status wins, so an exception raised while
   handling an older one reports its own failure. The walk is guarded against
   a cycle in `__context__` — an unguarded one would hang the run rather than
   fail a row.

Together these restore the invariant the decision above claims: every failure
this codebase's IA calls can raise now carries a status reachable without
touching `str(exc)`. Verified end to end through the real
`internetarchive.upload()` against a local server:

| Status | Rate limit | Retryable | Effect |
| --- | --- | --- | --- |
| 503, 429 | yes | no | run stops after the chunk's confirm write |
| 500, 502 | no | yes | retry absorbs it |
| 403, 400, 404 | no | no | row fails immediately |

The alternative was parsing the status out of the message, which the library
*does* preserve in text. Rejected: it is exactly what the decision above
replaced, and the message interpolates a server-influenced URL. Recovering
the integer structurally costs a dozen lines and keeps one rule instead of
two.

`urllib3` becomes a direct dependency, declared in `requirements.txt`. It was
already installed as `requests`' own dependency; `Retry` is imported from
`urllib3.util.retry` — where `internetarchive`'s `session.py` gets it — rather
than through `requests.adapters`, which re-exports it without declaring it
public.

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
