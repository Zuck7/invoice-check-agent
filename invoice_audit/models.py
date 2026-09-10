"""Core data model: invoices, rate cards, flags, audit results."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from .flags import BLOCKING, Severity, get as get_flag
from .money import ZERO, fmt


# --------------------------------------------------------------------------
# Rate cards
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RateCardEntry:
    """One priced activity on a buy card.

    ``kind`` is either ``per_unit`` (qty x rate) or ``percent_of_base``
    (base x (1 + percent/100)), the latter covering carrier-cost pass-throughs.
    """

    key: str
    label: str
    uom: str
    kind: str = "per_unit"
    rate: Decimal = ZERO
    percent: Decimal = ZERO
    aliases: tuple[str, ...] = ()

    @property
    def is_percent(self) -> bool:
        return self.kind == "percent_of_base"


@dataclass(frozen=True)
class RateCard:
    """A versioned buy card for one (warehouse, client) pair."""

    card_id: str
    version: str
    warehouse_id: str
    client_id: str
    effective_from: date
    effective_to: date | None
    entries: dict[str, RateCardEntry]

    def covers(self, on: date) -> bool:
        if on < self.effective_from:
            return False
        return self.effective_to is None or on <= self.effective_to

    def entry(self, key: str) -> RateCardEntry | None:
        return self.entries.get(key)

    @property
    def ref(self) -> str:
        """Provenance string recorded on every flag this card produced."""
        return f"{self.card_id}@{self.version}"


# --------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------


@dataclass
class InvoiceLine:
    line_no: int
    description: str
    quantity: Decimal
    amount: Decimal
    uom: str | None = None
    unit_rate: Decimal | None = None
    base_amount: Decimal | None = None
    source_page: int | None = None

    # filled in by the normalize stage
    rate_key: str | None = None
    mapped_by: str | None = None
    map_confidence: Decimal | None = None
    map_rationale: str | None = None

    @property
    def mapped(self) -> bool:
        return self.rate_key is not None


@dataclass
class Invoice:
    invoice_no: str
    warehouse_id: str
    client_id: str
    invoice_date: date
    period_start: date
    period_end: date
    lines: list[InvoiceLine]
    stated_total: Decimal
    currency: str = "USD"
    source_path: str | None = None

    @property
    def line_total(self) -> Decimal:
        return sum((line.amount for line in self.lines), ZERO)

    def content_hash(self) -> str:
        """Stable hash of the billed content, ignoring presentation.

        Two files that bill identically hash identically, so a re-send is caught
        even if the PDF was regenerated. A revision changes the hash, which is
        how ``DUPLICATE_INVOICE`` tells the two apart.
        """
        parts = [
            self.invoice_no,
            self.warehouse_id,
            self.client_id,
            self.invoice_date.isoformat(),
            str(self.stated_total),
        ]
        for line in sorted(self.lines, key=lambda l: l.line_no):
            parts.append(
                "|".join(
                    [
                        line.description.strip().lower(),
                        str(line.quantity),
                        str(line.unit_rate or ""),
                        str(line.amount),
                    ]
                )
            )
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Flags
# --------------------------------------------------------------------------


@dataclass
class Flag:
    """One defensible assertion about one invoice.

    Everything needed to argue it with a warehouse travels on the flag itself:
    what we saw, what we expected, the money at stake, and which rate card
    version we consulted to decide.
    """

    flag_id: str
    invoice_no: str
    message: str
    line_no: int | None = None
    line_description: str | None = None
    expected: str | None = None
    actual: str | None = None
    delta: Decimal = ZERO
    rate_card_version: str | None = None
    source_page: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        get_flag(self.flag_id)  # rejects anything outside the taxonomy

    @property
    def severity(self) -> Severity:
        return get_flag(self.flag_id).severity

    @property
    def blocks_rerate(self) -> bool:
        return self.flag_id in BLOCKING

    def to_dict(self, currency: str = "USD") -> dict[str, Any]:
        return {
            "flag": self.flag_id,
            "severity": self.severity.value,
            "invoice_no": self.invoice_no,
            "line_no": self.line_no,
            "line_description": self.line_description,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
            "delta": str(self.delta),
            "delta_display": fmt(self.delta, currency),
            "rate_card_version": self.rate_card_version,
            "source_page": self.source_page,
            "evidence": self.evidence,
        }


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------


@dataclass
class AuditResult:
    invoice: Invoice
    flags: list[Flag]
    rate_card: RateCard | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """True when the invoice must not proceed to re-rating."""
        return any(f.blocks_rerate for f in self.flags)

    @property
    def clean(self) -> bool:
        return not self.flags

    @property
    def exposure(self) -> Decimal:
        """Net money at stake. Positive means the warehouse over-billed us."""
        return sum((f.delta for f in self.flags), ZERO)

    def by_severity(self, severity: Severity) -> list[Flag]:
        return [f for f in self.flags if f.severity is severity]

    def sorted_flags(self) -> list[Flag]:
        return sorted(
            self.flags,
            key=lambda f: (f.severity.rank, f.line_no or 0, f.flag_id),
        )

    def to_dict(self) -> dict[str, Any]:
        cur = self.invoice.currency
        return {
            "invoice_no": self.invoice.invoice_no,
            "warehouse_id": self.invoice.warehouse_id,
            "client_id": self.invoice.client_id,
            "invoice_date": self.invoice.invoice_date.isoformat(),
            "period": [
                self.invoice.period_start.isoformat(),
                self.invoice.period_end.isoformat(),
            ],
            "currency": cur,
            "stated_total": str(self.invoice.stated_total),
            "rate_card_version": self.rate_card.ref if self.rate_card else None,
            "source_path": self.invoice.source_path,
            "content_hash": self.invoice.content_hash(),
            "blocked_from_rerate": self.blocked,
            "exposure": str(self.exposure),
            "exposure_display": fmt(self.exposure, cur),
            "flag_count": len(self.flags),
            "flags": [f.to_dict(cur) for f in self.sorted_flags()],
            "notes": self.notes,
        }
