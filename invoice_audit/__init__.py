"""Inbound warehouse invoice audit — v1.

Audits the bill a warehouse sends us against the versioned buy rate card and
our own order data, and emits typed flags with full provenance into an
exception queue for a human.

Phases 1-3 of the build map: the deterministic spine, line mapping with a
retry budget, and the persistent exception queue with aging and trends.

Out of scope: re-rating to the client's sell card, QuickBooks, sending,
AR chase. See spec.md.

    from invoice_audit import AuditEngine, RateCardStore

    engine = AuditEngine(store=RateCardStore.from_dir("data/rate_cards"))
    result = engine.audit_path("data/invoices/INV-4471.csv")
"""

from .checks import Tolerances
from .credits import CsvCreditLog, NoCreditLog
from .engine import AuditEngine
from .flags import FLAGS, Severity
from .flagstore import ESCALATION_DAYS, FlagRecord, FlagStore, Status
from .history import HistoryStore
from .intake import IntakeError, read_invoice
from .models import AuditResult, Flag, Invoice, InvoiceLine, RateCard
from .normalize import FuzzyMapper, LineMapper, NullMapper, Normalizer, Suggestion
from .orderdata import CsvOrderData, NoOrderData, PeriodKey
from .ratecards import RateCardStore
from .snapshots import CsvSnapshots, NoSnapshots
from .report import render, render_batch, to_json
from .scoring import LabelledCase, Scorecard, load_cases, score

__all__ = [
    "AuditEngine",
    "AuditResult",
    "CsvCreditLog",
    "CsvOrderData",
    "CsvSnapshots",
    "ESCALATION_DAYS",
    "FLAGS",
    "Flag",
    "FlagRecord",
    "FlagStore",
    "FuzzyMapper",
    "HistoryStore",
    "LabelledCase",
    "LineMapper",
    "NullMapper",
    "IntakeError",
    "Invoice",
    "InvoiceLine",
    "NoCreditLog",
    "NoOrderData",
    "NoSnapshots",
    "Normalizer",
    "PeriodKey",
    "RateCard",
    "RateCardStore",
    "Scorecard",
    "Severity",
    "Status",
    "Suggestion",
    "load_cases",
    "score",
    "Tolerances",
    "read_invoice",
    "render",
    "render_batch",
    "to_json",
]

__version__ = "1.4.0"
