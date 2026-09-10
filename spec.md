## Problem (restated)
Audit **inbound** warehouse invoices (buy side) against the buy rate card and our own
order data. Emit a typed flag per problem line item, each with a severity and a dollar
delta. Output is an exception queue for a human to work — not an automated dispute and
not an automated payment hold.

Outbound (re-rating to the sell card, invoicing the client, chasing payment) is real
work but it is not v1. See Roadmap.

## Assumptions
- AMZ prep receives dozens of invoices every day and some have missing information or either incorrect numbers
- This is used to track the success rate every month
- We are a middleman: warehouses bill us on a buy card, we re-rate the same activity to
  a client's sell card, and the spread is our margin. Both sides use per-line rates, not
  a global markup.
- Volume is dozens/day, not thousands. This is small enough that a fixed pipeline beats
  a heavy agent loop; see Rejected.

## Requirements

### v1 — inbound audit
1. Intake (email)
2. Parse PDF/CSV
3. **Normalize line items** — map free-text invoice descriptions to rate-card keys
4. Reconcile vs buy card
5. Audit qty vs order data

Steps 3–5 are the ones that produce a dollar delta. Everything else is plumbing.
Step 3 was missing from the original list and is the hardest part of the job: the
warehouse's "Pick & pack · per order" will not literally match our rate-card key, and
units of measure drift (per pallet vs per pallet/month vs per cubic foot).

### Roadmap — outbound (not v1)
6. Re-rate to sell card
7. Invoice in QuickBooks
8. Send
9. Chase payment (AR)
10. Disputes → human
11. Close & report

Note for later: QuickBooks Online has no true idempotency keys. Creates need a unique
`Request-Id` UUID per attempt and `SyncToken` checks on updates, or retries will
double-bill clients. Rate limit is 500 req/min per realmId, so partition the job queue
by realmId from day one.

## Flag taxonomy

The deliverable is tags, so the tags are the spec. Detection column says whether the
check is deterministic code or needs the model. **Arithmetic and rate lookup are always
deterministic** — never model output.

| flag | description | example | detection | inputs required | severity |
|---|---|---|---|---|---|
| `RATE_DRIFT` | line rate ≠ buy card rate for the effective date | Storage billed at $20/pallet; the signed buy card says $18 | deterministic | buy card + effective dates | high |
| `QTY_VARIANCE` | billed qty ≠ WMS/OMS count, outside tolerance | 3,200 orders billed; our order data shows 3,050 | deterministic | WMS/OMS counts | high |
| `UNKNOWN` | line item that is on no rate card at all | "Container destuff fee — $450" matches nothing on any card | LLM | rate card | high |
| `RATE_CARD_VERSION_STALE` | invoice date outside the card's effective window | Nov invoice priced off a card that expired Sep 30 | deterministic | buy card versions | high |
| `WRONG_CLIENT_RATES` | rate matches a different client's card | Pick billed at $0.42/unit, which is Client B's rate | deterministic | all buy cards | high |
| `UOM_MISMATCH` | billed on a different unit than the card prices | Storage billed per pallet when the card prices per pallet/month | LLM + rules | rate card UoM | high |
| `MATH_ERROR` | qty × rate ≠ extended, or lines ≠ invoice total | 22 × $28 extended as $644 instead of $616 | deterministic | invoice only | high |
| `DUPLICATE_CHARGE` | same service billed twice in or across periods | Receiving for the same ASN on both the Oct and Nov invoice | deterministic | invoice history | high |
| `DUPLICATE_INVOICE` | invoice number or content hash seen before | INV-4471 emailed twice, identical content | deterministic | invoice history | high |
| `MISSING_LINE` | activity present in WMS, absent from the invoice | 22 B2B pallets shipped, no freight line billed | deterministic | WMS/OMS counts | medium |
| `MISSING_CREDIT` | expected credit from the dispute log is absent | $310 credit agreed in Sep never appears | deterministic | dispute/credit log | medium |
| `SURCHARGE_BASE_WRONG` | +10% computed off the wrong base | Parcel +10% taken on $9,900 when carrier cost was $9,500 | deterministic | carrier cost + card | medium |
| `STORAGE_AGING_ERROR` | long-term penalty applied to already-shipped inventory | LTS fee on 12 pallets that shipped Oct 3 | deterministic | WMS snapshots | medium |
| `LOW_CONFIDENCE_EXTRACTION` | model confidence below threshold after retries | Scanned invoice, storage qty unreadable | model | — | low |

`RATE_DRIFT`, `QTY_VARIANCE` and `UNKNOWN` are the three we already name in practice and
are the core of v1 — the rest of the table is the long tail behind them.

**`UNKNOWN` never silently passes through.** A line we cannot price is a line we cannot
re-rate to the client, so it always goes to a human even when the invoice total is
otherwise correct.

`MISSING_LINE` is deliberate: under-billing matters too, because we re-rate to a client
and an activity the warehouse forgot is one we may still owe the client a charge for.

> OPEN QUESTION: severity column is my first pass. Someone who works the disputes should
> re-rank it before we build routing on top of it.

## Must have
- Check the invoice for incorrect information, emitting flags from the taxonomy above
- Every flag carries: source page, extracted value, expected value, dollar delta, and
  the rate-card version that was consulted
- Arithmetic and rate lookup are deterministic code, never model output
- Extraction has a confidence threshold and a retry budget (~2 refinements, then
  escalate to a human rather than looping)
- Every run is reproducible from stored inputs — we are touching money, so the audit
  trail is immutable
- No line is ever passed through unpriced. If it maps to no rate card, it is `UNKNOWN`
  and a human sees it.

### Data dependencies (blocking)
Steps 4–5 are impossible without all of these. If any is not accessible today, that is
the first thing to fix and nothing else matters until it is.

- Versioned **buy rate cards** with effective dates and amendments
- Per-client **sell rate cards** (roadmap, but needed to size the work)
- Queryable **WMS/OMS data**: pallet counts, order counts, unit counts, receipts
- **Carrier manifests** for parcel/freight verification
- Prior **dispute and credit log**, to detect credits that never landed

> OPEN QUESTION: which of these five do we actually have API or export access to right
> now? This gates the whole project.

## Nice to have
- **Flag history and trend reporting.** Store every flag per warehouse per month so we
  can see "Warehouse X has over-billed storage three months running." This is a query
  over stored flags, not a model that learns — same value, and it stays auditable.

## Approach

### Chosen
Fixed six-stage pipeline — intake, classify, extract, validate, route, sync — with the
model confined to two jobs: extraction, and mapping line descriptions to rate-card keys.
All comparison and arithmetic is ordinary code. Failures route to a human exception
queue with the full diagnosis attached, so review never starts cold.

### Rejected (and why)
- **LLM performs the arithmetic and rate comparison** — unauditable, and deterministic
  code is exact. The model's job is reading documents, not doing math.
- **Buy an off-the-shelf 3PL audit tool** (FreightOptics, Implentio, Diversifi) — they
  assume you are the end customer. None of them do the buy→sell re-rate, which is the
  part specific to our model. Worth revisiting for v1's audit half alone if build cost
  runs over.
- **Full agentic loop for v1** — agent architectures run 3–4× the LLM calls of a fixed
  pipeline (5–8 per invoice vs 1–2) to buy adaptive exception handling. At dozens/day we
  don't have the volume to justify it, and the exception path is a human anyway.
- **Automated dispute submission** — too much blast radius before we know our false-flag
  rate.

## Success metrics
"Success rate" was undefined and unmeasurable as written. Track monthly:

- **Recall** — errors caught ÷ errors actually present
- **Precision** — 1 − false-flag rate
- **$ recovered** — the number that justifies the project
- **Straight-through rate** — invoices needing zero human touch

Targets from published AP benchmarks: 60–75% straight-through after roughly three months
of tuning; exception rate under 14% (top performers hit 9%). Industry touchless average
is 32.6%, best-in-class 49.2% — so 60% is ambitious, not conservative.

Recall cannot be measured without ground truth, so building a labeled set is task #1.

## Edge cases

- **Margin is per-line from the rate card, not a global markup.** The sample below runs
  50%, 38.9%, 50%, 50%, 10%, 35.7% across six lines. Any check that assumes a flat
  markup will be wrong on every invoice.
- **Storage quantity basis is ambiguous.** "Storage · per pallet/mo — 120" against 40
  pallets received: snapshot, daily average, or summed pallet-days? Storage is the
  most-disputed category in the field precisely because inventory moves daily.
  > OPEN QUESTION: which basis does each warehouse actually bill on? May differ per
  > warehouse, in which case it belongs in the rate card.
- **Dispute deadlines are short.** Contractual windows are 30–90 days, carrier windows
  30–60. If a warehouse bills monthly, we may already be tight the day the invoice
  arrives — so flags need an age and an escalation SLA, not just a queue.
- Credits, rebills, minimums, and accessorials all appear on real invoices and none were
  in the original spec.
- Same invoice emailed twice; invoice revised and re-sent under the same number.

### Handling
- Duplicates: hash invoice content and key on (warehouse, invoice number, period). A
  re-send with changed content is a revision, not a duplicate — flag it as such.
- Unpriceable lines never fail silently. `UNKNOWN` goes to the queue with the model's
  best guesses ranked, and blocks the invoice from being re-rated until a human resolves
  it.
- Flags age. Anything unworked at 21 days escalates, to stay inside the shortest
  dispute window.

### Deliberately out of scope
- Multi-currency and tax/VAT
- Automated dispute submission to the warehouse
- Carrier-level parcel audit (dim weight, zone, surcharges) — we check the +10% base
  only, not the carrier's own math
- Anything past step 5 (see Roadmap)

## Task list
1. Label a ground-truth set of ~30 real invoices with known errors. Everything else is
   unmeasurable until this exists.
2. Define the flag schema in code, matching the taxonomy table.
3. PDF/CSV extraction with per-field confidence and a retry budget.
4. Line-item normalization — descriptions → rate-card keys, with UoM handling.
5. Deterministic reconcile engine: rate lookup by effective date, math checks,
   duplicate detection.
6. Qty audit against WMS/OMS order data.
7. Exception queue with full provenance per flag.
8. Measure recall/precision against the labeled set from (1); tune.

## Example

### Input the agent actually sees
> TODO: paste a redacted real inbound warehouse invoice here, plus a second copy with
> 2–3 seeded errors and the flag output we expect from it. The table below is *not* what
> the agent parses — a warehouse bill has only the buy column.

### Expected sell-side output (one month, one customer)
| LINE ITEM | QTY | BUY | SELL | MARGIN |
|---|---|---|---|---|
| Receiving · per pallet | 40 | $240 | $360 | $120 |
| Storage · per pallet/mo | 120 | $2,160 | $3,000 | $840 |
| Pick & pack · per order | 3,200 | $8,000 | $12,000 | $4,000 |
| Pick · per extra unit | 5,400 | $1,620 | $2,430 | $810 |
| Parcel/shipping · carrier cost +10% | — | $9,500 | $10,450 | $950 |
| B2B freight · per pallet out | 22 | $616 | $836 | $220 |
| **Total** | | **$22,136** | **$29,076** | **$6,940** |

Math checks out: every line's qty × rate reconciles, the columns sum, and
$29,076 − $22,136 = $6,940. Useful as a regression fixture.
