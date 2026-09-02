# Decisions

Why the tool is shaped the way it is. Reconstructed from the build history
(`573cfc6`..`460d1b6`), the review notes, and the behavior of the code itself.
Read this before "simplifying" something here — several of these look like
accidents and are not.

This file is an **index**. Each decision lives in a topic file under
[`decisions/`](decisions/); the links below go straight to it.

Section titles are quoted verbatim by about thirty code comments — for
example `identifiers.py` says *see docs/DECISIONS.md, "Tool-owned Sheet
columns are all ia_-prefixed"*. Find the title in the list below and follow
the link. **Renaming a title means updating those comments too** — grep for
it first.

## [Foundations](decisions/FOUNDATIONS.md)

The choices everything else rests on: what talks to Internet Archive, what is
configuration, and what was accepted as a known limit rather than missed.

- [Use the `internetarchive` Python library, not `ia upload --spreadsheet`](decisions/FOUNDATIONS.md#use-the-internetarchive-python-library-not-ia-upload---spreadsheet)
- [Generic to "a project", not hardcoded to photos](decisions/FOUNDATIONS.md#generic-to-a-project-not-hardcoded-to-photos)
- [Technical configuration lives in the registry, not the command line](decisions/FOUNDATIONS.md#technical-configuration-lives-in-the-registry-not-the-command-line)
- [Accepted, not overlooked](decisions/FOUNDATIONS.md#accepted-not-overlooked)

## [Identifiers](decisions/IDENTIFIERS.md)

How a permanent identifier is formed, minted, reserved and protected. Internet
Archive can darken an item but never rename it, so almost everything here is
about not getting a number wrong once.

- [Safety rail: prefix in code, never in the CSV](decisions/IDENTIFIERS.md#safety-rail-prefix-in-code-never-in-the-csv)
- [Test identifiers carry a per-run stamp](decisions/IDENTIFIERS.md#test-identifiers-carry-a-per-run-stamp)
- [Identifiers are minted by `upload` and written back to the Sheet](decisions/IDENTIFIERS.md#identifiers-are-minted-by-upload-and-written-back-to-the-sheet)
- [Minted numbers are re-checked against the Sheet before reserving](decisions/IDENTIFIERS.md#minted-numbers-are-re-checked-against-the-sheet-before-reserving)
- [Tool-owned Sheet columns are all `ia_`-prefixed](decisions/IDENTIFIERS.md#tool-owned-sheet-columns-are-all-ia_-prefixed)
- [The four `ia_` columns are required in every mode, including the safe one](decisions/IDENTIFIERS.md#the-four-ia_-columns-are-required-in-every-mode-including-the-safe-one)
- [`identifier-bib` is written back to the Sheet, not just generated](decisions/IDENTIFIERS.md#identifier-bib-is-written-back-to-the-sheet-not-just-generated)
- [An identifier is checked against the run's project, not the whole registry](decisions/IDENTIFIERS.md#an-identifier-is-checked-against-the-runs-project-not-the-whole-registry)

## [The Sheet protocol](decisions/SHEET-PROTOCOL.md)

Reading the Sheet live, the reserve → upload → confirm ordering, the guards
against a Sheet edited mid-run, and how corrections get back out.

- [The Sheet is read live; the CSV becomes the offline path](decisions/SHEET-PROTOCOL.md#the-sheet-is-read-live-the-csv-becomes-the-offline-path)
- [A row's identity is its `file_template` columns, not its `ia_identifier`](decisions/SHEET-PROTOCOL.md#a-rows-identity-is-its-file_template-columns-not-its-ia_identifier)
- [The Sheet is the correction](decisions/SHEET-PROTOCOL.md#the-sheet-is-the-correction)
- [`sync-metadata --csv` reads its targets from the upload log](decisions/SHEET-PROTOCOL.md#sync-metadata---csv-reads-its-targets-from-the-upload-log)
- [`--resume-from` filters on run mode](decisions/SHEET-PROTOCOL.md#--resume-from-filters-on-run-mode)
- [A fingerprint only proves identity while it is unique](decisions/SHEET-PROTOCOL.md#a-fingerprint-only-proves-identity-while-it-is-unique)

## [Readiness and errors](decisions/READINESS.md)

The difference between a row nobody has filled in yet and a row that is broken
— and what each command does about it.

- [A blank cell is not an error](decisions/READINESS.md#a-blank-cell-is-not-an-error)
- [On the Sheet path, `upload` uploads the valid rows and reports the rest](decisions/READINESS.md#on-the-sheet-path-upload-uploads-the-valid-rows-and-reports-the-rest)
- [A bad row is skipped; a bad header stops the whole run](decisions/READINESS.md#a-bad-row-is-skipped-a-bad-header-stops-the-whole-run)
- [A malformed header is rejected, never auto-corrected](decisions/READINESS.md#a-malformed-header-is-rejected-never-auto-corrected)

## [Files and metadata](decisions/FILES-AND-METADATA.md)

How a row's file is found on disk, and which of a row's cells become Internet
Archive metadata.

- [A file is found by resolution, not by constructing a path](decisions/FILES-AND-METADATA.md#a-file-is-found-by-resolution-not-by-constructing-a-path)
- [Sheet metadata is filtered at the upload boundary, not in `upload_row`](decisions/FILES-AND-METADATA.md#sheet-metadata-is-filtered-at-the-upload-boundary-not-in-upload_row)
- [`identifier-bib` and `mediatype` are generated, not columns](decisions/FILES-AND-METADATA.md#identifier-bib-and-mediatype-are-generated-not-columns)
- [Blank cell means "leave alone", not "clear"](decisions/FILES-AND-METADATA.md#blank-cell-means-leave-alone-not-clear)
- [Blank `date` becomes `[n.d.]` rather than being omitted](decisions/FILES-AND-METADATA.md#blank-date-becomes-nd-rather-than-being-omitted)
- [`checksum=True` and `verbose=True` on upload](decisions/FILES-AND-METADATA.md#checksumtrue-and-verbosetrue-on-upload)

## [Reconciliation](decisions/RECONCILIATION.md)

Matching a Sheet's filename cell against what `reconcile-files` finds on
disk when the two disagree, and why every match it makes stays a proposal
until a human accepts it.

- [A correction is proposed, never applied](decisions/RECONCILIATION.md#a-correction-is-proposed-never-applied)
- [Pass B requires identical digit sequences](decisions/RECONCILIATION.md#pass-b-requires-identical-digit-sequences)
- [Accents are folded, not deleted](decisions/RECONCILIATION.md#accents-are-folded-not-deleted)
- [A typed filename is resolved, not trusted](decisions/RECONCILIATION.md#a-typed-filename-is-resolved-not-trusted)
- [Reconciliation ships before append](decisions/RECONCILIATION.md#reconciliation-ships-before-append)
- [Exit code is 0 while work remains](decisions/RECONCILIATION.md#exit-code-is-0-while-work-remains)

## [Quota, pacing and the run record](decisions/QUOTA-AND-RUNS.md)

Internet Archive's limits, how a run paces itself against them, and what each
run writes down about itself.

- [`--limit` counts planned targets, not Sheet rows; `--chunk-size` overrides `CHUNK_SIZE`](decisions/QUOTA-AND-RUNS.md#--limit-counts-planned-targets-not-sheet-rows---chunk-size-overrides-chunk_size)
- [A run may not exceed Internet Archive's daily item cap](decisions/QUOTA-AND-RUNS.md#a-run-may-not-exceed-internet-archives-daily-item-cap)
- [Rate-limit detection uses a parsed status code, never message text](decisions/QUOTA-AND-RUNS.md#rate-limit-detection-uses-a-parsed-status-code-never-message-text)
- [Retry covers transport failures, never refusals](decisions/QUOTA-AND-RUNS.md#retry-covers-transport-failures-never-refusals)
- [A status the metadata call strips is recovered, still without reading text](decisions/QUOTA-AND-RUNS.md#a-status-the-metadata-call-strips-is-recovered-still-without-reading-text)
- [Every recorded timestamp is UTC](decisions/QUOTA-AND-RUNS.md#every-recorded-timestamp-is-utc)
- ["Unchanged" is a third outcome, not a failure](decisions/QUOTA-AND-RUNS.md#unchanged-is-a-third-outcome-not-a-failure)

## Still open


- ~~`collection_key` has never been confirmed~~ **Settled 2026-08-23**:
  `collection_key` is `"lcps"` — the first segment of every minted identifier
  and of every item's permanent public URL
  (`archive.org/details/lcps-sarasoldphotos-00001`). Confirmed as the most
  specific pointer to the organization; `lcpsociety` (the IA account's domain)
  and `lcpsdigitalcollection` (the parent collection) were both considered and
  rejected as slightly off. This value never reaches Internet Archive — it is
  purely the identifier namespace, used by `format_identifier()`,
  `next_identifiers()` and `check_identifier()`.
  **Not to be confused with `ia_collection`** — see below; they are unrelated
  values, and both are now settled.
- ~~The real IA collection has never been confirmed~~ **Settled 2026-08-22**:
  `ia_collection` is `sarasoldphotos`, the subcollection — confirmed to exist
  (`archive.org/metadata/sarasoldphotos`, title "Sara's Old Photos") and
  already parented to `lcpsdigitalcollection`, the LCPS parent collection.
  Items are tagged into the subcollection alone; membership in the parent
  (and in `clatsopcountyhistoricalsociety` and `americana`, both listed on
  `sarasoldphotos`'s own `collection` field) is transitive, so listing more
  than one collection per item is unnecessary. A same-day earlier pass had
  recorded this value as `lcpsdigitalcollection` itself — that collection is
  real and does exist, but it is the *parent*, not the subcollection the
  operator decided items should carry; this replaces that value before any
  `--live` run. Nothing in this tool checks that a collection exists, so this
  remains a manual, one-time verification.
- **Also settled 2026-08-22**: the registry's project id was corrected from
  `sarahsoldphotos` to `sarasoldphotos` — the donor is Sara, and the `h` was
  only ever the operator's habitual spelling, never a confirmed value.
  `archive.org/metadata/sarahsoldphotos` returned `{}` (no such item);
  `archive.org/metadata/sarasoldphotos` returned the real collection recorded
  above. Because **no `--live` run has ever happened**, no permanent
  identifier was ever minted under the misspelling, which is the only reason
  this correction was still cheap — it reaches the registry key, every
  minted-identifier example in the docs and tests, `README.md`, and
  `docs/OPERATIONS.md`. It does **not** reach the `zztest-`-prefixed sandbox
  identifiers already recorded elsewhere in these docs as historical fact
  (see "Test identifiers carry a per-run stamp" and "`identifier-bib` and
  `mediatype` are generated, not columns" above) — those items were actually
  minted, under the old spelling, in `test_collection`, so rewriting the docs
  to claim otherwise would misstate what is really out there, auto-expiring
  or not. `collection_key` (`"lcps"`) is untouched by either correction; it
  remains a third, unrelated value — see above.
- Whether IA emits a distinguishable signal at its 5,000/day cap, as opposed to
  generic throttling, **remains open** — no `--live` run has ever happened, so
  no real rate-limit response has ever been captured. **2026-08-22, revised
  same day:** `is_rate_limit_error()` now stops a run on a `429`/`503` upload
  failure, read from a PARSED status-code integer rather than message text
  (see "Rate-limit detection uses a parsed status code..." above, which
  replaced an earlier version of this same decision after review found the
  text-scanning approach could false-positive). Tracing the installed
  library's source shows the structured status survives even the path where
  the *message* is rewritten and loses it, so this should catch a real 429/503
  reaching this codebase's upload path either way it can arrive - but "should"
  is still verified only against the installed library's source, not an
  actual response from archive.org, since no `--live` run has happened.
  **2026-08-23:** the run no longer depends on detecting the cap at all to
  respect it — both paths refuse to *start* a run of more than 5,000 items
  (see "A run may not exceed Internet Archive's daily item cap" above). What
  stays open is only whether IA's response is distinguishable when the cap is
  hit anyway, e.g. across two runs in one day, which the tool does not track.
  `--limit` (see "`--limit` counts planned targets..." above) remains the
  operator-controlled fallback either way.
- ~~How a run establishes the next free `NUMBER`~~ **Settled 2026-08-08**:
  reserve in the Sheet before uploading, and track completion in an
  `ia_uploaded` column so an interrupted run is recoverable.
- ~~Which Google credential type the Sheet integration uses~~ **Settled
  2026-08-08**: OAuth with the **Internal** user type, which requires the Cloud
  project to sit inside the lcpsociety.org organization and the operator to
  hold an `@lcpsociety.org` account. An API key was ruled out (read-only, and
  only reaches publicly shared sheets); a service account was ruled out because
  it loses per-person attribution in the Sheet's edit history.
- Automating the Sheet → CSV *export* was raised and deferred pending maintainer
  input. **Superseded 2026-08-08** by reading the Sheet directly (above).
