# Invoice audit agent

Audits the bill a warehouse sends us against the versioned buy rate card and our
own order data, and emits typed flags with full provenance into a persistent
exception queue for a human.

Phases 1–3 of the build map: the deterministic spine, line mapping with a retry
budget, and the queue with aging, resolution and trends. Everything outbound —
re-rating to the sell card, QuickBooks, sending, AR chase — is phase 4 and
deliberately absent. See [spec.md](spec.md).

Python 3.11+. The audit engine has **no dependencies** — intake, mapping, every
check, the flag store and the scorer are stdlib only. PDF and image extraction
is the one exception:

```sh
pip install -e ".[pdf]"      # pdfplumber, for born-digital PDFs
pip install -e ".[gemini]"   # google-genai, for scans (default provider)
pip install -e ".[vision]"   # anthropic, the alternative provider
pip install -e ".[all]"
```

### Credentials

Scans need a model; CSVs and born-digital PDFs never touch one and need no key
at all.

```sh
cp .env.example .env     # then paste your key in
```

`.env` is gitignored; `.env.example` is the committed template and is asserted
to hold no values. Every command loads it at startup, searching up from the
working directory.

**A real environment variable always wins over the file.** A shell export, a CI
secret and a systemd unit are all more authoritative than a file someone left
in a directory — silently overriding them is how a deploy ends up running on a
developer's key.

| variable | for |
|---|---|
| `GOOGLE_API_KEY` (or `GEMINI_API_KEY`) | Gemini, the default provider |
| `ANTHROPIC_API_KEY` | Claude, if you switch to it |
| `INVOICE_AUDIT_VISION` | `gemini` or `anthropic` |

Per-command overrides: `--google-api-key`, `--vision anthropic`.

## Run it

```sh
python3 -m invoice_audit serve                               # the UI
python3 -m invoice_audit audit data/invoices/*.csv --flags-db data/flags.json
python3 -m invoice_audit queue --flags-db data/flags.json
python3 -m invoice_audit queue --flags-db data/flags.json --escalated
python3 -m invoice_audit resolve 1670 --flags-db data/flags.json \
    --note "credit CN-118 received" --by sam
python3 -m invoice_audit trends --flags-db data/flags.json
python3 -m invoice_audit serve  --flags-db data/flags.json   # the queue UI
python3 -m invoice_audit score          # recall/precision on the labelled set
python3 -m invoice_audit flags          # print the taxonomy
python3 -m unittest discover -s tests -t .
```

Exit codes: `0` clean, `1` a human has work to do, `2` a document could not be
read. The middle one is the useful signal for a cron.

Useful switches:

| flag | effect |
|---|---|
| `--rate-cards DIR` | card store (default `data/rate_cards`) |
| `--wms FILE` | WMS/OMS counts; without it quantity checks are skipped and the report says so |
| `--history FILE` | persists invoice history so duplicates are caught across runs |
| `--json FILE` | writes the exception queue as JSON |
| `--flags-db FILE` | persistent exception queue; without it the queue is per-run |
| `--qty-tolerance PCT` | variance allowed before flagging (default 0) |
| `--no-mapper` | alias matching only, no fuzzy mapping (phase 1 behaviour) |
| `-v` | show evidence and card version per flag |

## What's in the box

| stage | module | model? |
|---|---|---|
| 01 intake | `intake.py` | routes by suffix |
| 02 extract (csv) | `intake.py` | no — exact and free |
| 02 extract (pdf, text layer) | `pdftext.py` | no — exact and free |
| 02 extract (scans, images) | `vision.py` + `backends.py` | Gemini 2.5 Pro or Claude Opus 5 |
| 03 normalize | `normalize.py` | `FuzzyMapper` by default; LLM slots into the same protocol |
| 04 rate check, 05 qty audit, 06 emit | `checks.py` | no |
| pipeline | `engine.py` | — |
| queue rendering | `report.py` | — |
| queue persistence, aging, trends | `flagstore.py` | — |
| re-rate to the sell card | `rerate.py` | — |
| assembled view for the UI | `workspace.py` | — |
| HTTP API + static host | `server.py` | — |
| React app | `ui/` → `invoice_audit/ui/dist` | — |
| scoring against the labelled set | `scoring.py` | — |

Reference data lives in `ratecards.py`, `orderdata.py` and `history.py`.

## Hosting a public demo

```sh
docker build -t invoice-audit .
docker run -p 8765:8765 invoice-audit
```

Or push to GitHub and point [Render](https://render.com) at `deploy/render.yaml`
(free tier, Docker runtime); `deploy/fly.toml` covers Fly.io. Both read `PORT`
from the platform and health-check `/api/mode`.

The image is a two-stage build — node compiles the UI, the runtime carries
only Python — runs as a non-root user, and is ~247 MB.

### What `--demo` changes

A public URL has nobody to hand a token to, so demo mode opens writes. That is
only safe because everything reachable is disposable:

| | normal | `--demo` |
|---|---|---|
| writes | token required | open |
| uploaded files | kept | gone on restart |
| resolved flags | persist | gone on restart |
| model calls | unlimited | capped by `INVOICE_AUDIT_VISION_LIMIT` |
| binds to | `127.0.0.1` | `0.0.0.0` |

The cap matters: without it a stranger can spend an unbounded amount of your
API credit by uploading scans in a loop. At the limit, uploads still work —
CSVs and text-layer PDFs never call a model — and the message says so.

The upload guards do **not** relax in demo mode: type, size, duplicate-name and
traversal checks all still apply, and the UI banner tells visitors the data is
shared and temporary.

### What this is still not

Demo mode is safe to *abuse*, not secure. There is no per-visitor isolation, no
rate limiting, and no identity — one visitor can resolve a flag another raised.
For anything real, drop `--demo`, put it behind your own auth, and give it a
disk that survives a restart.

## Measuring it

`score` runs the engine over `data/labeled/` and reports recall, precision and
per-flag catches. Each case runs against a **fresh engine**; a case that needs
history declares it in `prior`. Without that isolation two cases covering the
same service period contaminate each other and the scorecard measures the
fixture rather than the engine.

The harness earned its keep immediately: it caught `MISSING_LINE` re-flagging
every activity on a correction invoice that a main invoice had already billed.

Exit code is 1 when anything is missed or falsely flagged, so it works in CI.

## The exception queue

A `Flag` is an observation the engine made; a `FlagRecord` is the work a human
owns. Records are keyed by `Flag.fingerprint` — warehouse, invoice, line, flag
and the expected/actual values — so re-auditing an invoice updates the existing
entry rather than creating a second one, and **a flag someone resolved stays
resolved** across re-runs. A changed number is a different fingerprint, which is
the behaviour you want: the warehouse re-billing at a new wrong rate is new work.

Anything open past 21 days escalates, which is inside the shortest dispute
window (carrier windows run 30–60 days, contractual 30–90).

## Flags implemented

All fourteen. The last two needed data sources rather than logic:

* `MISSING_CREDIT` reads `credits.py` — the dispute log of what the warehouse
  agreed to give back. Only `agreed` rows are outstanding; `applied` and
  `rejected` ones must never raise a flag.
* `STORAGE_AGING_ERROR` reads `snapshots.py` — pallet-level received and
  shipped dates. Period counts cannot answer it: they say how many
  pallet-months were stored, not which pallets were old enough to attract a
  surcharge, nor which had already left the building.

`python3 -m invoice_audit flags` prints the current state.

## Sample data

`data/invoices/INV-4471.csv` is the clean month from the spec, buy column only —
the audit raises nothing on it. `INV-4482.csv` is the same month with one seeded
error per flag; it raises seven flags and $1,976.00 of net exposure. Both are
the seed of the labelled set phase 0 calls for, and both are asserted in
`tests/test_audit.py` with hand-written expected values.

## Design rules worth keeping

**Money is `Decimal`, never `float`.** `money()` raises on a float argument
rather than silently accepting representation error into a number we may have to
defend in a dispute.

**Arithmetic and rate lookup never touch a model.** Everything in `checks.py` is
ordinary code. The model's only job, when phase 2 arrives, is reading documents
and mapping line descriptions to rate-card keys.

**Missing data is not clean data.** No WMS counts for a period means the
quantity checks are skipped and the report says the invoice is not fully
audited. A gap in the feed never becomes a green tick, and never manufactures a
`MISSING_LINE` flag out of an unknown period.

**A low-confidence mapping is left unpriced.** Below the confidence threshold the
line stays unmapped and becomes `UNKNOWN` rather than being quietly priced on a
guess. Below the *plausibility floor* it does not even claim to be a near miss —
`LOW_CONFIDENCE_EXTRACTION` only fires in the band between the two, where telling
a reviewer "we almost had this" is actually true.

**The retry budget is real, not decorative.** `Normalizer` walks a ladder of
progressively cleaner readings of the description — as written, then with dates
and billing noise stripped, then alphabetic tokens only — and stops at
`MAX_RETRIES`. "Storage charges - pallet/month (Aug 2026)" scores 0.67 on the
first pass and 0.89 on the second, which is the difference between a human
touching it and not.

**Duplicate detection is conservative.** Within one invoice a line only counts as
duplicated when rate key, quantity *and* amount match — warehouses legitimately
split one activity across lines. Across invoices the service period must match
too, since billing storage every month is the job, not a fault.

**Cards are chosen by service period, not invoice date.** A card that changes on
the 1st must not re-price the month that just closed. This differs from the
wording in the spec's taxonomy, which framed `RATE_CARD_VERSION_STALE` by invoice
date; the period-based reading is the defensible one and the flag description has
been updated to match.

## The UI

A React app (Vite) served by the Python process. Build it once, then run:

```sh
npm --prefix ui install && npm --prefix ui run build
python3 -m invoice_audit serve
```

The CLI prints a URL carrying a write token; the app stores it in
`sessionStorage` and sends it on mutations only. Open the app without the token
and it works read-only and says so.

Five views:

| view | what it answers |
|---|---|
| **Check an invoice** | drop a CSV, PDF or photo and get the verdict — this is the landing view, and the entry point for anyone who never opens a terminal |
| **Queue** | what needs a human now — severity stripes, escalation filter, full provenance per flag, resolve / dismiss / reopen |
| **Invoices** | per invoice: what was billed, what was flagged, and the buy → sell → margin re-rate |
| **Trends** | exposure by warehouse and month, and which faults keep recurring |
| **Coverage** | which reference feeds are wired, what each one enables, and the full taxonomy |

### Uploading

Drag a file onto the drop zone, or click to browse. The document is saved into
the watched folder, audited immediately, and the verdict comes back inline:
clean, flagged, or blocked, with the money at stake and the margin if it can be
re-rated.

Uploads are raw bytes with the filename in a header rather than multipart — the
browser can POST a `File` directly, which keeps a form parser out of this
server.

Four things it refuses, and why:

| refused | reason |
|---|---|
| anything but `.csv .pdf .png .jpg .jpeg` | nothing else is an invoice |
| a filename already in the folder | silently overwriting a financial document is worse than saying no |
| over 32 MB | the Claude API cannot take a larger PDF anyway |
| a file it cannot parse | the partial file is deleted, so the folder never holds a half-invoice |

Filenames are **sanitised, not escaped** — `../../../../etc/evil.csv` lands as
`evil.csv` inside the folder. Re-uploading the same document is accepted but
immediately flagged `DUPLICATE_INVOICE`, so the same invoice cannot be paid
twice.

**Coverage exists because a missing feed is silently indistinguishable from a
clean month.** Without the WMS export there is no `QTY_VARIANCE`; without the
dispute log there is no `MISSING_CREDIT`. The view names every feed that is off
and the checks it disables, and documents that could not be read at all are
listed separately — a read failure is not a clean invoice.

A resolution **requires a name and a note**, refused client-side and again with
a 400 server-side. Provenance is the entire point of the queue.

For development, `npm --prefix ui run dev` proxies `/api` to port 8765.

### What it is not

It binds to loopback and guards writes with a shared token — enough to stop a
stray script or another user on a shared machine, not real identity. Anything
beyond one trusted network needs proper auth.

## Re-rating to the sell card

`rerate.py` is the first piece of phase 4, and the part no off-the-shelf 3PL
audit tool does — they all assume you are the end customer rather than the
middleman.

Two rules govern it:

**A line we could not price cannot be marked up.** An `UNKNOWN` line has no buy
rate, so it has no defensible sell rate. The invoice is blocked from going out.

**Re-rating uses our quantities, not the warehouse's.** Where the audit found a
`QTY_VARIANCE`, the client is billed what our order data says happened. Billing
a client for 3,200 orders because a warehouse typed it is how an inbound error
becomes an outbound one.

Margin is per line from the card, never a flat markup — the sample month runs
50%, 38.9%, 50%, 50%, 10% and 35.7% across six lines, and a test asserts the
spread stays varied so nobody quietly replaces the card with a multiplier.

## Swapping the vision provider

`vision.py` owns everything that decides whether an extraction is *valid* — the
prompt, the schema, the validation, the retry ladder. `backends.py` owns only
the call itself. A backend turns (bytes, instruction) into a JSON string and
nothing more, so changing provider cannot quietly change what counts as a
well-formed invoice. A test asserts both backends receive the identical prompt
and schema.

One real incompatibility is handled in `to_gemini_schema`: we write nullable
fields as `{"type": ["string", "null"]}`, Gemini wants
`{"type": "string", "nullable": true}`. Left untranslated the whole request is
rejected, so it is not cosmetic — and the translation is a copy, because the
Anthropic backend still needs the original.

Gemini runs at `temperature=0`. This is transcription, not composition.

## Extraction: text layer first, vision second

`PdfRouter` tries `pdftext.PdfTextExtractor` first. When a warehouse sends a
PDF from their billing system the characters are already in the file: reading
them is exact, free, offline, and no model can misread a rate that was never
rendered to pixels.

A scan has no text layer — it is a picture of an invoice — so the parser returns
nothing and the router falls through to `vision.VisionExtractor`. That is a
capability gap, not a quality tradeoff, and warehouses do send scans. Layout is
the second reason to keep vision: each warehouse has its own template, and
rebuilding table rows from character coordinates is per-warehouse work that
never finishes.

**The text parser's rule is defer, never guess.** A half-understood table
produces plausible wrong numbers, and downstream nothing can tell a parse error
from a billing error. So it refuses — raising `NotConfident`, which routes the
document to vision — whenever it cannot label a column header, cannot read a
cell as money, or finds that the rows it parsed do not add up to the printed
total. That last check matters most: a dropped row would otherwise surface as a
`MISSING_LINE` flag blaming the warehouse for our own mistake.

`PdfRouter.routes` counts how many documents took each path, so the split is
measured rather than assumed.

The reason a vision model is *safe* here is the architecture, not the model.
`vision.py` only transcribes. It never computes, never looks up a rate, never
decides. Every number it reads is checked by deterministic code against the buy
card and the WMS, so a misread degrades into a flag rather than a silent error:

| the model misreads | the audit raises |
|---|---|
| a rate | `RATE_DRIFT` |
| a quantity | `QTY_VARIANCE` |
| an extension | `MATH_ERROR` |
| the total | `MATH_ERROR` (invoice total) |
| anything illegibly | `LOW_CONFIDENCE_EXTRACTION` |

The checks that catch a warehouse's mistakes catch the extractor's too. A
system that *trusted* the extraction could not make this trade.

**The prompt's most important rule is that the model must not help.** A
transcriber that "fixes" `22 × $28.00 = $644.00` to `616.00` deletes the
`MATH_ERROR` that pays for the project. `SYSTEM` says so first and with a worked
example, and `tests/test_vision.py` asserts it end to end.

Money and quantities come back as **strings**, parsed into `Decimal` here — a
JSON number would already have been through a float.

## Not done, and why

- **Email intake.** Needs mailbox credentials and a decision about where they
  live. The CLI takes files; a mail poller that drops files into a watched
  directory is the small piece missing.
- **Authentication on the queue.** It binds to loopback and trusts whoever
  reaches it. Anything beyond one trusted network needs a real identity story.
- **A rate-card ingestion path.** Cards are hand-written JSON. A wrong card
  produces confident false flags across every invoice from that warehouse,
  which is the failure mode that destroys trust in the tool fastest.
- **Everything outbound** — phase 4.

## Labelled set

Four cases. **Phase 0 calls for about thirty**, and `score` prints a warning
until it has them. The current numbers (100% recall, 100% precision) say the
engine handles the cases we wrote, not that it handles your invoices.

## Open questions from the spec, still open

1. Which of the five data dependencies do we actually have access to?
2. What basis does each warehouse bill storage on — snapshot, daily average, or
   pallet-days? The sample invoice is ambiguous and the code currently compares
   whatever the WMS export provides.
3. Are the severities right? They drive routing and have not been reviewed by
   anyone who has argued one of these with a warehouse.
