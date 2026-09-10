"""The pipeline.

Stages map one-to-one onto the component map:

    01 intake -> 02 extract -> 03 normalize -> 04 rate check
              -> 05 qty audit -> 06 flag emitter

Reference data (cards, WMS counts, invoice history) is shared across stages
rather than owned by any one of them, which is why it is injected here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .checks import ALL_CHECKS, CheckContext, Tolerances
from .history import HistoryStore, SeenInvoice
from .intake import DEFAULT_EXTRACTORS, Extractor, read_invoice
from .models import AuditResult, Invoice
from .normalize import LineMapper, NullMapper, Normalizer
from .orderdata import NoOrderData, OrderDataProvider, PeriodKey
from .ratecards import RateCardStore


@dataclass
class AuditEngine:
    store: RateCardStore
    order_data: OrderDataProvider = field(default_factory=NoOrderData)
    history: HistoryStore = field(default_factory=HistoryStore)
    mapper: LineMapper = field(default_factory=NullMapper)
    tolerances: Tolerances = field(default_factory=Tolerances)
    extractors: tuple[Extractor, ...] = DEFAULT_EXTRACTORS
    record_history: bool = True

    # -- stage 01/02 -------------------------------------------------------

    def read(self, path: Path) -> Invoice:
        return read_invoice(Path(path), self.extractors)

    def audit_path(self, path: Path) -> AuditResult:
        return self.audit(self.read(path))

    # -- stages 03-06 ------------------------------------------------------

    def audit(self, invoice: Invoice) -> AuditResult:
        notes: list[str] = []

        # Priced against the card in force when the work happened, not when the
        # invoice was typed. A card that changes on the 1st must not silently
        # re-price the month that just closed.
        card = self.store.for_invoice(
            invoice.warehouse_id, invoice.client_id, invoice.period_start
        )
        if card is not None and not card.covers(invoice.period_end):
            notes.append(
                f"Card {card.ref} expires mid-period "
                f"({card.effective_to}). Lines dated after that may be priced "
                "on the wrong card — split the invoice by card window."
            )

        # 03 normalize
        if card is not None:
            Normalizer(card=card, mapper=self.mapper).apply_all(invoice.lines)
        else:
            notes.append(
                "No buy card in force: line mapping, rate and quantity checks "
                "were all skipped."
            )

        # reference data for stage 05
        counts = None
        if card is not None:
            counts = self.order_data.counts(
                PeriodKey(
                    warehouse_id=invoice.warehouse_id,
                    client_id=invoice.client_id,
                    period_start=invoice.period_start,
                    period_end=invoice.period_end,
                )
            )
            if counts is None:
                notes.append(
                    "No WMS/OMS counts for this period: QTY_VARIANCE and "
                    "MISSING_LINE were not checked. This invoice is not fully "
                    "audited."
                )

        ctx = CheckContext(
            invoice=invoice,
            card=card,
            store=self.store,
            counts=counts,
            history=self.history,
            tolerances=self.tolerances,
        )

        # 04-06
        flags = [flag for check in ALL_CHECKS for flag in check(ctx)]

        unmapped = [l for l in invoice.lines if not l.mapped]
        if card is not None and unmapped:
            notes.append(
                f"{len(unmapped)} of {len(invoice.lines)} lines could not be "
                "priced. This invoice is blocked from re-rating."
            )

        if self.record_history:
            self.history.record(
                SeenInvoice(
                    invoice_no=invoice.invoice_no,
                    warehouse_id=invoice.warehouse_id,
                    client_id=invoice.client_id,
                    content_hash=invoice.content_hash(),
                    invoice_date=invoice.invoice_date,
                    source_path=invoice.source_path,
                )
            )

        return AuditResult(
            invoice=invoice, flags=flags, rate_card=card, notes=notes
        )

    def audit_all(self, paths: list[Path]) -> list[AuditResult]:
        """Audit in filename order so duplicate detection sees the earlier
        invoice first."""
        return [self.audit_path(p) for p in paths]
