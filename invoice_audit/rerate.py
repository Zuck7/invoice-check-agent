"""Phase 4, step 1: re-rate an audited invoice onto the client's sell card.

This is the part no off-the-shelf 3PL audit tool does, because they all assume
you are the end customer. We are the middleman: the warehouse bills us on a buy
card, we bill the client on a sell card, and the spread is the business.

Two rules govern when re-rating is allowed:

1. **A line we could not price cannot be marked up.** An ``UNKNOWN`` line has no
   buy rate, so it has no defensible sell rate either. The invoice is blocked.
2. **Re-rating uses our own quantities, not the warehouse's.** When the audit
   found a QTY_VARIANCE we bill the client what our order data says happened,
   not what the warehouse claimed. Billing a client for 3,200 orders because a
   warehouse typed it is how an inbound error becomes an outbound one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import AuditResult, Invoice, RateCard, RateCardEntry
from .money import ZERO, fmt, money
from .ratecards import load_card


class SellCardStore:
    """Sell cards, keyed by client and effective date.

    A sell card is not warehouse-specific: the client is billed the same
    whichever of our warehouses did the work, which is precisely where margin
    varies and why it has to be computed per line rather than assumed.
    """

    def __init__(self, cards: list[RateCard]) -> None:
        self._cards = list(cards)

    @classmethod
    def from_dir(cls, directory: Path) -> "SellCardStore":
        paths = sorted(Path(directory).glob("*.json"))
        if not paths:
            raise FileNotFoundError(f"no sell cards found in {directory}")
        return cls([load_card(p) for p in paths])

    def __len__(self) -> int:
        return len(self._cards)

    def for_client(self, client_id: str, on: date) -> RateCard | None:
        candidates = [
            c for c in self._cards if c.client_id == client_id and c.covers(on)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda c: c.effective_from)


@dataclass
class RerateLine:
    rate_key: str
    label: str
    uom: str
    quantity: Decimal
    buy_rate: Decimal
    sell_rate: Decimal
    buy_amount: Decimal
    sell_amount: Decimal
    quantity_source: str = "invoice"
    note: str = ""

    @property
    def margin(self) -> Decimal:
        return self.sell_amount - self.buy_amount

    @property
    def margin_pct(self) -> Decimal:
        if self.buy_amount == ZERO:
            return ZERO
        return ((self.margin / self.buy_amount) * Decimal(100)).quantize(
            Decimal("0.01")
        )

    def to_dict(self, currency: str = "USD") -> dict[str, Any]:
        return {
            "rate_key": self.rate_key,
            "label": self.label,
            "uom": self.uom,
            "quantity": str(self.quantity),
            "buy_rate": str(self.buy_rate),
            "sell_rate": str(self.sell_rate),
            "buy": str(self.buy_amount),
            "sell": str(self.sell_amount),
            "margin": str(self.margin),
            "margin_pct": str(self.margin_pct),
            "buy_display": fmt(self.buy_amount, currency),
            "sell_display": fmt(self.sell_amount, currency),
            "margin_display": fmt(self.margin, currency),
            "quantity_source": self.quantity_source,
            "note": self.note,
        }


@dataclass
class Rerate:
    invoice: Invoice
    lines: list[RerateLine] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sell_card_version: str | None = None
    buy_card_version: str | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_by)

    @property
    def buy_total(self) -> Decimal:
        return sum((l.buy_amount for l in self.lines), ZERO)

    @property
    def sell_total(self) -> Decimal:
        return sum((l.sell_amount for l in self.lines), ZERO)

    @property
    def margin(self) -> Decimal:
        return self.sell_total - self.buy_total

    @property
    def margin_pct(self) -> Decimal:
        if self.buy_total == ZERO:
            return ZERO
        return ((self.margin / self.buy_total) * Decimal(100)).quantize(
            Decimal("0.01")
        )

    @property
    def margin_display(self) -> str:
        return fmt(self.margin, self.invoice.currency)

    @property
    def buy_display(self) -> str:
        return fmt(self.buy_total, self.invoice.currency)

    @property
    def sell_display(self) -> str:
        return fmt(self.sell_total, self.invoice.currency)

    def to_dict(self) -> dict[str, Any]:
        cur = self.invoice.currency
        return {
            "invoice_no": self.invoice.invoice_no,
            "client_id": self.invoice.client_id,
            "warehouse_id": self.invoice.warehouse_id,
            "period": [
                self.invoice.period_start.isoformat(),
                self.invoice.period_end.isoformat(),
            ],
            "currency": cur,
            "blocked": self.blocked,
            "blocked_by": self.blocked_by,
            "warnings": self.warnings,
            "buy_card_version": self.buy_card_version,
            "sell_card_version": self.sell_card_version,
            "lines": [l.to_dict(cur) for l in self.lines],
            "buy_total": str(self.buy_total),
            "sell_total": str(self.sell_total),
            "margin": str(self.margin),
            "margin_pct": str(self.margin_pct),
            "buy_display": self.buy_display,
            "sell_display": self.sell_display,
            "margin_display": self.margin_display,
        }


def rerate(
    result: AuditResult,
    sell_cards: SellCardStore,
    counts: dict[str, Decimal] | None = None,
) -> Rerate:
    """Price an audited invoice onto the client's sell card."""
    invoice = result.invoice
    out = Rerate(
        invoice=invoice,
        buy_card_version=result.rate_card.ref if result.rate_card else None,
    )

    if result.rate_card is None:
        out.blocked_by.append("no buy card in force — nothing can be priced")
        return out

    sell = sell_cards.for_client(invoice.client_id, invoice.period_start)
    if sell is None:
        out.blocked_by.append(
            f"no sell card for {invoice.client_id} covering "
            f"{invoice.period_start.isoformat()}"
        )
        return out
    out.sell_card_version = sell.ref

    unknown = [f for f in result.flags if f.flag_id == "UNKNOWN"]
    for flag in unknown:
        out.blocked_by.append(
            f"line {flag.line_no} could not be priced: {flag.line_description}"
        )

    variances = {
        f.evidence.get("rate_key"): f
        for f in result.flags
        if f.flag_id == "QTY_VARIANCE"
    }

    for line in invoice.lines:
        if not line.mapped:
            continue
        key = line.rate_key or ""
        buy_entry = result.rate_card.entry(key)
        sell_entry = sell.entry(key)
        if buy_entry is None:
            continue
        if sell_entry is None:
            out.blocked_by.append(
                f"{buy_entry.label} is on the buy card but not {invoice.client_id}'s "
                "sell card — we would be absorbing this at cost"
            )
            continue

        quantity = line.quantity
        source = "invoice"
        note = ""
        variance = variances.get(key)
        if variance is not None:
            corrected = variance.evidence.get("wms_count")
            if corrected is not None:
                quantity = Decimal(str(corrected))
                source = "order data"
                note = (
                    f"warehouse billed {line.quantity}; billing the client "
                    f"{quantity} from our own order data"
                )

        if buy_entry.is_percent:
            base = line.base_amount if line.base_amount is not None else line.amount
            buy_amount = money(base * (Decimal(100) + buy_entry.percent) / Decimal(100))
            sell_amount = money(base * (Decimal(100) + sell_entry.percent) / Decimal(100))
            buy_rate, sell_rate = buy_entry.percent, sell_entry.percent
        else:
            buy_rate, sell_rate = buy_entry.rate, sell_entry.rate
            buy_amount = money(quantity * buy_rate)
            sell_amount = money(quantity * sell_rate)

        if sell_amount < buy_amount:
            out.warnings.append(
                f"{sell_entry.label} sells below cost: "
                f"{fmt(sell_amount, invoice.currency)} against "
                f"{fmt(buy_amount, invoice.currency)}"
            )

        out.lines.append(
            RerateLine(
                rate_key=key,
                label=sell_entry.label,
                uom=sell_entry.uom,
                quantity=quantity,
                buy_rate=buy_rate,
                sell_rate=sell_rate,
                buy_amount=buy_amount,
                sell_amount=sell_amount,
                quantity_source=source,
                note=note,
            )
        )

    open_high = [
        f for f in result.flags
        if f.severity.value == "high" and f.flag_id != "UNKNOWN"
    ]
    if open_high:
        out.warnings.append(
            f"{len(open_high)} unresolved high-severity flag(s) on the buy side. "
            "Re-rating is allowed but the buy figures may still move."
        )

    return out
