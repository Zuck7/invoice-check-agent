# Invoice Audit Agent

Third-party logistics warehouses over-bill. Industry audits routinely find
**7–10% of charges wrong** on a first pass, and a fulfilment company receiving
dozens of invoices a month has no practical way to check them by hand.

This agent reads each invoice, checks every line against the signed rate card
and the company's own warehouse data, and produces a queue of typed, costed
findings a human can act on — then re-rates the corrected invoice onto the
client's sell card to show the real margin.

> **Live demo:** [invoice-audit-demo.onrender.com](https://invoice-audit-demo.onrender.com)
> Upload [`examples/DEMO-INV-5007.csv`](examples/DEMO-INV-5007.csv) — it returns
> 7 findings worth $2,003 and blocks the invoice from being re-billed.

---

## What it catches

Fourteen typed findings, each carrying what was expected, what was billed, the
dollar delta, and the rate-card version consulted — enough to take into a
dispute without re-deriving it.

| | |
|---|---|
| `RATE_DRIFT` | billed above the signed card |
| `QTY_VARIANCE` | billed more units than the warehouse system recorded |
| `UNKNOWN` | a charge that appears on no rate card |
| `WRONG_CLIENT_RATES` | billed at another client's rates |
| `MATH_ERROR` | qty × rate ≠ the extended amount |
| `DUPLICATE_INVOICE` / `DUPLICATE_CHARGE` | the same bill, or the same service, twice |
| `MISSING_CREDIT` | an agreed credit that never landed |
| `STORAGE_AGING_ERROR` | long-term storage billed on pallets that already shipped |
| …and five more | `python -m invoice_audit flags` lists them all |

## How it works

```
intake → extract → normalise → rate check → quantity audit → flag → re-rate
```

| stage | how |
|---|---|
| **Extract** | CSV directly; born-digital PDFs from their text layer (`pdfplumber`); scans and photos via a vision model (Gemini or Claude) |
| **Normalise** | maps free text — "Pick & pack · per order" — to a rate-card key |
| **Check** | plain Python against the versioned buy card, WMS counts, invoice history, dispute log and inventory snapshots |
| **Re-rate** | prices the corrected invoice onto the client's sell card for per-line margin |

**A model is used for exactly one thing: reading documents.** Every number it
produces is then checked by deterministic code. A misread rate becomes a
`RATE_DRIFT`; a misread total becomes a `MATH_ERROR`. The checks that catch a
warehouse's mistakes catch the extractor's too.

## Quick start

```sh
pip install -e ".[pdf]"
npm --prefix ui install && npm --prefix ui run build
python -m invoice_audit serve
```

Opens the UI on `localhost:8765` with three sample invoices already audited.

Scanned documents additionally need an API key. Create a `.env` (gitignored):

```
INVOICE_AUDIT_VISION=gemini
GOOGLE_API_KEY=your-key
```

CSVs and text-layer PDFs never call a model and need no key at all.

**Command line**

```sh
python -m invoice_audit audit data/invoices/*.csv   # audit and report
python -m invoice_audit score                       # recall/precision vs the labelled set
python -m invoice_audit trends --flags-db data/flags.json
```

**Docker**

```sh
docker build -t invoice-audit . && docker run -p 8765:8765 invoice-audit
```

## The UI

React + Vite, served by the Python process.

- **Check an invoice** — drop a file, get the verdict
- **Queue** — open findings, aged, with resolve / dismiss and a required audit note
- **Invoices** — what was billed, what was flagged, buy → sell → margin
- **Trends** — exposure by warehouse and month, and faults that keep recurring
- **Coverage** — which data feeds are connected and what each one enables

## Decisions worth explaining

**Money is `Decimal`, never `float`.** The constructor raises on a float rather
than accepting representation error into a number that may end up in a dispute.

**Arithmetic never touches a model.** Rate lookups and totals are ordinary code.
"The model thought so" is not an argument a warehouse has to accept, and
determinism is what makes regressions detectable.

**The extraction prompt's first rule is that the model must not help.** A
transcriber that "fixes" `22 × $28.00 = $644.00` to `$616.00` silently deletes
the finding. The prompt forbids calculation with a worked example, and a test
asserts the wrong number survives end to end.

**Missing data is not clean data.** No warehouse export means quantity checks
are skipped and the report says the invoice was not fully audited. A gap in a
feed never becomes a green tick.

**The PDF parser defers rather than guesses.** If it cannot label a column, or
the rows it parsed do not sum to the printed total, it refuses and hands the
document to the vision model — because a dropped row would otherwise be blamed
on the warehouse as a missing charge.

**Re-rating uses our quantities, not the warehouse's.** Where the audit found a
quantity variance, the client is billed what actually happened. Otherwise an
inbound error becomes an outbound one.

## Built with

Python 3.11+ · React 18 · Vite · Docker

The audit engine itself has **no third-party dependencies** — intake, mapping,
every check, the flag store and the scorer are standard library. `pdfplumber`
and a vision SDK are optional extras used only for reading documents.

## Testing

```sh
python -m unittest discover -s tests -t .
```

179 tests, no network access required — the vision backends are faked. A
scoring harness measures recall and precision against a labelled set of
invoices with known errors, so changes to the extractor can be evaluated rather
than eyeballed.

## Status

Working end to end: document intake, all fourteen checks, the exception queue,
margin re-rating, and a deployable demo.

Not built: email intake, QuickBooks invoicing and payment chasing. The labelled
set is 4 invoices and wants ~30 before its accuracy numbers mean much, and
neither extractor has yet been run against a real warehouse's documents.

## License

MIT
