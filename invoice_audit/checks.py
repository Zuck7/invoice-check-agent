"""Stages 04-06: rate check, quantity audit, flag emission.

Every function here is ordinary deterministic code. No model is consulted and
none should be: these are the assertions we take into a dispute, and
"the model thought so" is not an argument a warehouse has to accept.

Each check yields zero or more :class:`Flag` objects carrying full provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterator

from .history import HistoryStore
from .models import Flag, Invoice, InvoiceLine, RateCard
from .money import ZERO, fmt, money, pct
from .normalize import canonical
from .orderdata import PeriodKey
from .ratecards import RateCardStore

ONE = Decimal(1)

# Unit-of-measure synonyms. The warehouse's wording varies; the meaning does not.
_UOM_SYNONYMS = {
    "pallet": "pallet",
    "pallets": "pallet",
    "plt": "pallet",
    "pallet mo": "pallet_month",
    "pallet month": "pallet_month",
    "pallet per month": "pallet_month",
    "pallet months": "pallet_month",
    "palletmonth": "pallet_month",
    "order": "order",
    "orders": "order",
    "unit": "unit",
    "units": "unit",
    "each": "unit",
    "cubic foot": "cubic_foot",
    "cubic feet": "cubic_foot",
    "cuft": "cubic_foot",
    "shipment": "shipment",
    "shipments": "shipment",
}


def normalise_uom(value: str | None) -> str | None:
    if not value:
        return None
    key = canonical(value)
    return _UOM_SYNONYMS.get(key, key.replace(" ", "_"))


def uom_label(uom: str, count: Decimal | None = None) -> str:
    """Render a unit for a human: ``pallet_month`` -> ``pallet months``.

    This text ends up in dispute correspondence, so it reads as English rather
    than as a rate-card key.
    """
    text = uom.replace("_", " ")
    if count is not None and abs(count) == 1:
        return text
    return f"{text}s"


@dataclass(frozen=True)
class Tolerances:
    """What counts as a difference worth a human's time.

    ``money_tolerance`` absorbs cent-level rounding between the warehouse's
    billing system and ours. ``min_exposure`` keeps trivia out of the queue —
    a $0.04 delta is real but not worth a dispute letter.
    """

    money_tolerance: Decimal = money("0.01")
    qty_tolerance_pct: Decimal = Decimal("0")
    min_exposure: Decimal = money("1.00")

    def money_differs(self, a: Decimal, b: Decimal) -> bool:
        return abs(a - b) > self.money_tolerance

    def qty_differs(self, billed: Decimal, actual: Decimal) -> bool:
        if billed == actual:
            return False
        if self.qty_tolerance_pct <= 0 or actual == 0:
            return True
        allowed = abs(actual) * self.qty_tolerance_pct / Decimal(100)
        return abs(billed - actual) > allowed


@dataclass
class CheckContext:
    invoice: Invoice
    card: RateCard | None
    store: RateCardStore
    counts: dict[str, Decimal] | None
    history: HistoryStore
    tolerances: Tolerances = Tolerances()

    @property
    def period(self) -> PeriodKey:
        return PeriodKey(
            warehouse_id=self.invoice.warehouse_id,
            client_id=self.invoice.client_id,
            period_start=self.invoice.period_start,
            period_end=self.invoice.period_end,
        )

    @property
    def card_ref(self) -> str | None:
        return self.card.ref if self.card else None

    def flag(self, flag_id: str, message: str, **kw: object) -> Flag:
        kw.setdefault("rate_card_version", self.card_ref)
        return Flag(
            flag_id=flag_id,
            invoice_no=self.invoice.invoice_no,
            message=message,
            **kw,  # type: ignore[arg-type]
        )

    def line_flag(
        self, flag_id: str, line: InvoiceLine, message: str, **kw: object
    ) -> Flag:
        kw.setdefault("line_no", line.line_no)
        kw.setdefault("line_description", line.description)
        kw.setdefault("source_page", line.source_page)
        return self.flag(flag_id, message, **kw)


# --------------------------------------------------------------------------
# Intake-level checks
# --------------------------------------------------------------------------


def check_duplicate_invoice(ctx: CheckContext) -> Iterator[Flag]:
    inv = ctx.invoice
    content_hash = inv.content_hash()

    for prior in ctx.history.lookup_hash(content_hash):
        yield ctx.flag(
            "DUPLICATE_INVOICE",
            f"Identical content to {prior.invoice_no} already processed on "
            f"{prior.invoice_date.isoformat()}. Do not pay twice.",
            expected="not previously seen",
            actual=f"exact content match with {prior.invoice_no}",
            delta=inv.stated_total,
            evidence={
                "kind": "exact_duplicate",
                "content_hash": content_hash,
                "prior_source": prior.source_path,
            },
        )
        return

    same_number = ctx.history.lookup(inv.invoice_no, inv.warehouse_id)
    if same_number:
        yield ctx.flag(
            "DUPLICATE_INVOICE",
            f"Invoice number {inv.invoice_no} was seen before with different "
            "content. This is a revision, not a duplicate — confirm which "
            "version supersedes before paying.",
            expected="a number we have not billed against",
            actual="same number, changed content",
            delta=ZERO,
            evidence={
                "kind": "revision",
                "content_hash": content_hash,
                "prior_hashes": [s.content_hash for s in same_number],
                "prior_source": same_number[-1].source_path,
            },
        )


def check_rate_card_available(ctx: CheckContext) -> Iterator[Flag]:
    """No card in force for the service period is itself the finding."""
    if ctx.card is not None:
        return

    inv = ctx.invoice
    known = ctx.store.all_for_pair(inv.warehouse_id, inv.client_id)
    if known:
        windows = ", ".join(
            f"{c.version} ({c.effective_from.isoformat()}"
            f"..{c.effective_to.isoformat() if c.effective_to else 'open'})"
            for c in known
        )
        message = (
            f"No buy card is in force for {inv.client_id} at {inv.warehouse_id} "
            f"for a period starting {inv.period_start.isoformat()}. "
            f"Known cards: {windows}."
        )
        evidence = {"known_versions": [c.ref for c in known]}
    else:
        message = (
            f"No buy card exists at all for {inv.client_id} at "
            f"{inv.warehouse_id}. Nothing on this invoice can be priced."
        )
        evidence = {"known_versions": []}

    yield ctx.flag(
        "RATE_CARD_VERSION_STALE",
        message,
        expected=f"a card covering {inv.period_start.isoformat()}",
        actual="no effective card",
        delta=ZERO,
        rate_card_version=None,
        evidence=evidence,
    )


# --------------------------------------------------------------------------
# Line-level checks
# --------------------------------------------------------------------------


def check_unknown_lines(ctx: CheckContext) -> Iterator[Flag]:
    if ctx.card is None:
        # Without a card nothing is priceable, and flagging every line as
        # UNKNOWN would bury the one finding that matters: there is no card.
        return
    for line in ctx.invoice.lines:
        if line.mapped:
            continue

        if line.mapped_by == "model-rejected":
            yield ctx.line_flag(
                "LOW_CONFIDENCE_EXTRACTION",
                line,
                f"Mapping confidence {line.map_confidence} is below threshold "
                "after retries; the line was left unpriced.",
                expected="a confident rate-card match",
                actual=f"confidence {line.map_confidence}",
                evidence={"rationale": line.map_rationale},
            )

        yield ctx.line_flag(
            "UNKNOWN",
            line,
            f'"{line.description}" matches nothing on the buy card. '
            "It cannot be priced, so it cannot be re-rated to the client.",
            expected="a line item priced on the buy card",
            actual=f"{fmt(line.amount, ctx.invoice.currency)} unpriceable",
            delta=line.amount,
            evidence={
                "rationale": line.map_rationale,
                "mapped_by": line.mapped_by,
            },
        )


def check_math(ctx: CheckContext) -> Iterator[Flag]:
    """qty x rate == extended, and the lines add up to the stated total."""
    inv = ctx.invoice
    tol = ctx.tolerances

    for line in inv.lines:
        if line.unit_rate is None:
            continue
        expected = money(line.quantity * line.unit_rate)
        if tol.money_differs(expected, line.amount):
            yield ctx.line_flag(
                "MATH_ERROR",
                line,
                f"{line.quantity} x {fmt(line.unit_rate, inv.currency)} is "
                f"{fmt(expected, inv.currency)}, but the line is extended as "
                f"{fmt(line.amount, inv.currency)}.",
                expected=fmt(expected, inv.currency),
                actual=fmt(line.amount, inv.currency),
                delta=line.amount - expected,
                evidence={
                    "quantity": str(line.quantity),
                    "unit_rate": str(line.unit_rate),
                },
            )

    line_total = inv.line_total
    if tol.money_differs(line_total, inv.stated_total):
        yield ctx.flag(
            "MATH_ERROR",
            f"Line items total {fmt(line_total, inv.currency)} but the invoice "
            f"states {fmt(inv.stated_total, inv.currency)}.",
            expected=fmt(line_total, inv.currency),
            actual=fmt(inv.stated_total, inv.currency),
            delta=inv.stated_total - line_total,
            evidence={"scope": "invoice_total"},
        )


def check_uom(ctx: CheckContext) -> Iterator[Flag]:
    if ctx.card is None:
        return
    inv = ctx.invoice
    for line in inv.lines:
        if not line.mapped or not line.uom:
            continue
        entry = ctx.card.entry(line.rate_key or "")
        if entry is None:
            continue
        billed = normalise_uom(line.uom)
        carded = normalise_uom(entry.uom)
        if billed is None or carded is None or billed == carded:
            continue
        yield ctx.line_flag(
            "UOM_MISMATCH",
            line,
            f"Billed per {line.uom}, but the card prices {entry.label} per "
            f"{uom_label(entry.uom, ONE)}. The quantities are not comparable.",
            expected=f"per {uom_label(entry.uom, ONE)}",
            actual=f"per {line.uom}",
            evidence={"rate_key": entry.key},
        )


def check_rates(ctx: CheckContext) -> Iterator[Flag]:
    """Stage 04. RATE_DRIFT, or WRONG_CLIENT_RATES when the number belongs
    to someone else's account at the same warehouse."""
    if ctx.card is None:
        return
    inv = ctx.invoice
    tol = ctx.tolerances

    for line in inv.lines:
        if not line.mapped:
            continue
        entry = ctx.card.entry(line.rate_key or "")
        if entry is None or entry.is_percent:
            continue
        if line.unit_rate is None:
            continue
        if not tol.money_differs(line.unit_rate, entry.rate):
            continue

        delta = money((line.unit_rate - entry.rate) * line.quantity)

        owners = ctx.store.find_rate_owners(
            warehouse_id=inv.warehouse_id,
            key=entry.key,
            rate=line.unit_rate,
            on=inv.period_start,
            exclude_client=inv.client_id,
        )
        if owners:
            names = ", ".join(sorted({m.card.client_id for m in owners}))
            yield ctx.line_flag(
                "WRONG_CLIENT_RATES",
                line,
                f"Billed at {fmt(line.unit_rate, inv.currency)}/{uom_label(entry.uom, ONE)}, "
                f"which is {names}'s rate at this warehouse. Our card says "
                f"{fmt(entry.rate, inv.currency)}.",
                expected=f"{fmt(entry.rate, inv.currency)}/{uom_label(entry.uom, ONE)}",
                actual=f"{fmt(line.unit_rate, inv.currency)}/{uom_label(entry.uom, ONE)}",
                delta=delta,
                evidence={
                    "rate_key": entry.key,
                    "matching_clients": sorted({m.card.client_id for m in owners}),
                    "matching_cards": sorted({m.card.ref for m in owners}),
                },
            )
            continue

        yield ctx.line_flag(
            "RATE_DRIFT",
            line,
            f"{entry.label} billed at {fmt(line.unit_rate, inv.currency)}/"
            f"{uom_label(entry.uom, ONE)}; the signed buy card says "
            f"{fmt(entry.rate, inv.currency)}.",
            expected=f"{fmt(entry.rate, inv.currency)}/{uom_label(entry.uom, ONE)}",
            actual=f"{fmt(line.unit_rate, inv.currency)}/{uom_label(entry.uom, ONE)}",
            delta=delta,
            evidence={"rate_key": entry.key, "quantity": str(line.quantity)},
        )


def check_surcharge_base(ctx: CheckContext) -> Iterator[Flag]:
    """Pass-through lines: base x (1 + percent) must be what was billed.

    Skipped silently when the invoice does not disclose the base — we cannot
    verify it, and guessing would produce a flag we could not defend.
    """
    if ctx.card is None:
        return
    inv = ctx.invoice
    tol = ctx.tolerances

    for line in inv.lines:
        if not line.mapped:
            continue
        entry = ctx.card.entry(line.rate_key or "")
        if entry is None or not entry.is_percent:
            continue
        if line.base_amount is None:
            continue

        expected = money(line.base_amount * (Decimal(100) + entry.percent) / Decimal(100))
        if not tol.money_differs(expected, line.amount):
            continue

        implied = ZERO
        if line.base_amount:
            implied = (
                (line.amount - line.base_amount) / line.base_amount * Decimal(100)
            ).quantize(Decimal("0.01"))

        yield ctx.line_flag(
            "SURCHARGE_BASE_WRONG",
            line,
            f"{entry.label}: base {fmt(line.base_amount, inv.currency)} at "
            f"{pct(entry.percent)} is {fmt(expected, inv.currency)}, but "
            f"{fmt(line.amount, inv.currency)} was billed "
            f"(an implied {pct(implied)}).",
            expected=fmt(expected, inv.currency),
            actual=fmt(line.amount, inv.currency),
            delta=line.amount - expected,
            evidence={
                "rate_key": entry.key,
                "base_amount": str(line.base_amount),
                "carded_percent": str(entry.percent),
                "implied_percent": str(implied),
            },
        )


# --------------------------------------------------------------------------
# Quantity audit (stage 05)
# --------------------------------------------------------------------------


def check_quantities(ctx: CheckContext) -> Iterator[Flag]:
    if ctx.counts is None or ctx.card is None:
        return
    inv = ctx.invoice
    tol = ctx.tolerances

    for line in inv.lines:
        if not line.mapped:
            continue
        key = line.rate_key or ""
        if key not in ctx.counts:
            continue
        entry = ctx.card.entry(key)
        if entry is None or entry.is_percent:
            continue

        actual = ctx.counts[key]
        if not tol.qty_differs(line.quantity, actual):
            continue

        rate = line.unit_rate if line.unit_rate is not None else entry.rate
        delta = money((line.quantity - actual) * rate)

        yield ctx.line_flag(
            "QTY_VARIANCE",
            line,
            f"{line.quantity} {uom_label(entry.uom, line.quantity)} billed; "
            f"our order data shows "
            f"{actual} for {inv.period_start.isoformat()} to "
            f"{inv.period_end.isoformat()}.",
            expected=f"{actual} {uom_label(entry.uom, actual)}",
            actual=f"{line.quantity} {uom_label(entry.uom, line.quantity)}",
            delta=delta,
            evidence={
                "rate_key": key,
                "wms_count": str(actual),
                "billed_qty": str(line.quantity),
                "rate_applied": str(rate),
            },
        )


def check_missing_lines(ctx: CheckContext) -> Iterator[Flag]:
    """Activity the WMS recorded that the invoice never billed.

    Under-billing is still a finding: we re-rate to the client, so an activity
    the warehouse forgot is revenue we may be failing to charge on.
    """
    if ctx.counts is None or ctx.card is None:
        return
    inv = ctx.invoice
    billed = {line.rate_key for line in inv.lines if line.mapped}

    for key, count in sorted(ctx.counts.items()):
        if key in billed or count <= 0:
            continue
        entry = ctx.card.entry(key)
        if entry is None or entry.is_percent:
            continue
        value = money(count * entry.rate)
        if value <= ctx.tolerances.min_exposure:
            continue
        yield ctx.flag(
            "MISSING_LINE",
            f"Our order data shows {count} {uom_label(entry.uom, count)} of "
            f"{entry.label} in this "
            f"period, but the invoice bills no such line "
            f"({fmt(value, inv.currency)} at card rate).",
            expected=f"a line for {count} {uom_label(entry.uom, count)}",
            actual="absent from the invoice",
            delta=-value,
            evidence={"rate_key": key, "wms_count": str(count)},
        )


#: Run in this order so the queue reads the way a person would work it:
#: is it a duplicate, can we price it at all, then rate, then quantity.
ALL_CHECKS = (
    check_duplicate_invoice,
    check_rate_card_available,
    check_unknown_lines,
    check_math,
    check_uom,
    check_rates,
    check_surcharge_base,
    check_quantities,
    check_missing_lines,
)
