# Invoice audit agent — v1

Audits the bill a warehouse sends us against the versioned buy rate card and our
own order data, and emits typed flags with full provenance into an exception
queue for a human.

Scope is the inbound audit only — stages 01–06 of the component map. Re-rating
to the client's sell card, QuickBooks, sending and AR chase are phase 4 and are
deliberately not here. See [spec.md](spec.md).

No third-party dependencies. Python 3.11+.

## Run it

```sh
python3 -m invoice_audit audit data/invoices/*.csv
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
| `--qty-tolerance PCT` | variance allowed before flagging (default 0) |
| `-v` | show evidence and card version per flag |

## What's in the box

| stage | module | model? |
|---|---|---|
| 01 intake, 02 extract | `intake.py` | no — CSV only in v1 |
| 03 normalize | `normalize.py` | seam for phase 2 |
| 04 rate check, 05 qty audit, 06 emit | `checks.py` | no |
| pipeline | `engine.py` | — |
| exception queue | `report.py` | — |

Reference data lives in `ratecards.py`, `orderdata.py` and `history.py`.

## Flags implemented

Nine deterministic checks plus `UNKNOWN`, which is the only one that will need a
model. `DUPLICATE_CHARGE`, `MISSING_CREDIT` and `STORAGE_AGING_ERROR` are
registered but deferred to phase 3 — they need several periods of stored history
before they can fire. `python3 -m invoice_audit flags` prints the current state.

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
guess.

**Cards are chosen by service period, not invoice date.** A card that changes on
the 1st must not re-price the month that just closed. This differs from the
wording in the spec's taxonomy, which framed `RATE_CARD_VERSION_STALE` by invoice
date; the period-based reading is the defensible one and the flag description has
been updated to match.

## Not done, on purpose

- PDF extraction — phase 2. `PdfExtractor` raises with a pointer rather than
  pretending. Hand-key to the CSV format meanwhile.
- Exception queue UI, flag aging and the 21-day escalation — phase 3.
- The three phase-3 flags above.
- Everything outbound — phase 4.

## Open questions from the spec, still open

1. Which of the five data dependencies do we actually have access to?
2. What basis does each warehouse bill storage on — snapshot, daily average, or
   pallet-days? The sample invoice is ambiguous and the code currently compares
   whatever the WMS export provides.
3. Are the severities right? They drive routing and have not been reviewed by
   anyone who has argued one of these with a warehouse.
