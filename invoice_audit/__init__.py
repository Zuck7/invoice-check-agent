"""Inbound warehouse invoice audit — v1.

Audits the bill a warehouse sends us against the versioned buy rate card and
our own order data, and emits typed flags with full provenance into an
exception queue for a human.

Out of scope in v1: re-rating to the client's sell card, QuickBooks, sending,
AR chase. See spec.md.

    from invoice_audit import AuditEngine, RateCardStore

    engine = AuditEngine(store=RateCardStore.from_dir("data/rate_cards"))
    result = engine.audit_path("data/invoices/INV-4471.csv")
"""

from .checks import Tolerances
from .engine import AuditEngine
from .flags import FLAGS, Severity
from .history import HistoryStore
from .intake import IntakeError, read_invoice
from .models import AuditResult, Flag, Invoice, InvoiceLine, RateCard
from .normalize import Normalizer, Suggestion
from .orderdata import CsvOrderData, NoOrderData, PeriodKey
from .ratecards import RateCardStore
from .report import render, render_batch, to_json

__all__ = [
    "AuditEngine",
    "AuditResult",
    "CsvOrderData",
    "FLAGS",
    "Flag",
    "HistoryStore",
    "IntakeError",
    "Invoice",
    "InvoiceLine",
    "NoOrderData",
    "Normalizer",
    "PeriodKey",
    "RateCard",
    "RateCardStore",
    "Severity",
    "Suggestion",
    "Tolerances",
    "read_invoice",
    "render",
    "render_batch",
    "to_json",
]

__version__ = "1.0.0"
