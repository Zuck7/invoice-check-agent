"""pdfplumber routing, the two data-backed flags, and the queue server."""

from __future__ import annotations

import json
import re
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from invoice_audit import AuditEngine, CsvOrderData, HistoryStore, RateCardStore
from invoice_audit.credits import AgreedCredit, CsvCreditLog, NoCreditLog
from invoice_audit.flagstore import FlagStore, Status
from invoice_audit.models import Flag
from invoice_audit.server import UI_DIST, record_json
from invoice_audit.snapshots import (
    LONG_TERM_DAYS,
    CsvSnapshots,
    NoSnapshots,
    PalletRecord,
)

ROOT = Path(__file__).resolve().parent.parent
CARDS = ROOT / "data" / "rate_cards"
WMS = ROOT / "data" / "wms" / "counts.csv"
CREDITS = ROOT / "data" / "credits" / "log.csv"
SNAPSHOTS = ROOT / "data" / "snapshots" / "pallets.csv"
INVOICES = ROOT / "data" / "invoices"
SAMPLES = ROOT / "data" / "samples"


def build_engine(**kw) -> AuditEngine:
    defaults = dict(
        store=RateCardStore.from_dir(CARDS),
        order_data=CsvOrderData.from_file(WMS),
        credit_log=CsvCreditLog.from_file(CREDITS),
        snapshots=CsvSnapshots.from_file(SNAPSHOTS),
        history=HistoryStore(),
        flag_store=FlagStore(),
    )
    defaults.update(kw)
    return AuditEngine(**defaults)


def flag_ids(result) -> list[str]:
    return sorted(f.flag_id for f in result.flags)


# --------------------------------------------------------------------------
# pdfplumber
# --------------------------------------------------------------------------


def pdfplumber_available() -> bool:
    from invoice_audit import pdftext

    return pdftext.available()


@unittest.skipUnless(pdfplumber_available(), "pdfplumber not installed")
class PdfTextTests(unittest.TestCase):
    PDF = INVOICES / "INV-4471.pdf"

    @unittest.skipUnless((INVOICES / "INV-4471.pdf").exists(), "sample pdf absent")
    def test_reads_the_text_layer_exactly(self):
        from invoice_audit.pdftext import PdfTextExtractor

        invoice = PdfTextExtractor().extract(self.PDF)
        self.assertEqual(invoice.invoice_no, "INV-4471")
        self.assertEqual(invoice.stated_total, Decimal("22136.00"))
        self.assertEqual(len(invoice.lines), 6)
        self.assertEqual(invoice.lines[1].unit_rate, Decimal("18.00"))
        self.assertEqual(invoice.period_start, date(2026, 7, 1))

    @unittest.skipUnless((INVOICES / "INV-4471.pdf").exists(), "sample pdf absent")
    def test_a_text_layer_read_carries_no_extraction_confidence(self):
        """Exact reads must never trip LOW_CONFIDENCE_EXTRACTION."""
        from invoice_audit.pdftext import PdfTextExtractor

        invoice = PdfTextExtractor().extract(self.PDF)
        self.assertTrue(all(l.extract_confidence is None for l in invoice.lines))

    @unittest.skipUnless((INVOICES / "INV-4471.pdf").exists(), "sample pdf absent")
    def test_the_pdf_audits_identically_to_the_csv(self):
        csv_result = build_engine().audit_path(INVOICES / "INV-4471.csv")
        pdf_result = build_engine().audit_path(self.PDF)
        self.assertEqual(flag_ids(csv_result), flag_ids(pdf_result))
        self.assertEqual(pdf_result.invoice.line_total, Decimal("22136.00"))

    def test_a_scan_is_declined_not_guessed(self):
        import tempfile

        from invoice_audit.pdftext import NotConfident, PdfTextExtractor

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.pdf"
            # A PDF with no text objects at all.
            path.write_bytes(
                b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<<>>\n%%EOF\n"
            )
            extractor = PdfTextExtractor()
            self.assertFalse(extractor.has_text_layer(path))
            with self.assertRaises(NotConfident):
                extractor.extract(path)

    def test_router_prefers_the_text_layer(self):
        from invoice_audit.intake import PdfRouter

        router = PdfRouter()
        self.assertTrue(router.supports(Path("x.pdf")))
        if self.PDF.exists():
            router.extract(self.PDF)
            self.assertEqual(router.last_route, "text")
            self.assertEqual(router.routes["vision"], 0)


MISMATCH_PDF = Path("/tmp/ui/mismatch.pdf")


@unittest.skipUnless(pdfplumber_available(), "pdfplumber not installed")
class NotConfidentPolicyTests(unittest.TestCase):
    @unittest.skipUnless(MISMATCH_PDF.exists(), "fixture absent")
    def test_totals_that_do_not_reconcile_are_deferred(self):
        """A parse that loses a row must defer rather than hand on short lines.

        If it did not, the missing rows would surface later as MISSING_LINE
        flags against the warehouse for our own parsing mistake.
        """
        from invoice_audit.pdftext import NotConfident, PdfTextExtractor

        with self.assertRaises(NotConfident) as ctx:
            PdfTextExtractor().extract(MISMATCH_PDF)
        self.assertIn("cannot tell a parse error", str(ctx.exception))

    @unittest.skipUnless(MISMATCH_PDF.exists(), "fixture absent")
    def test_the_router_then_hands_it_to_vision(self):
        from invoice_audit.intake import PdfRouter
        from invoice_audit.vision import ExtractionError

        router = PdfRouter()
        with self.assertRaises(Exception) as ctx:
            router.extract(MISMATCH_PDF)
        self.assertEqual(router.last_route, "vision")
        # No credentials here, so vision fails — but the routing decision is
        # what this asserts, and the error names both halves.
        self.assertIn("text layer declined", str(ctx.exception))


# --------------------------------------------------------------------------
# MISSING_CREDIT
# --------------------------------------------------------------------------


class CreditTests(unittest.TestCase):
    def test_agreed_credit_that_never_landed_is_flagged(self):
        result = build_engine().audit_path(INVOICES / "INV-4482.csv")
        credit = [f for f in result.flags if f.flag_id == "MISSING_CREDIT"]
        self.assertEqual(len(credit), 1)
        self.assertEqual(credit[0].evidence["credit_ref"], "CN-118")
        self.assertEqual(credit[0].delta, Decimal("310.00"))

    def test_applied_and_rejected_credits_are_not_outstanding(self):
        log = CsvCreditLog.from_file(CREDITS)
        july = log.outstanding("WH-NORTH", "ACME", date(2026, 7, 1), date(2026, 7, 31))
        self.assertEqual(july, [])  # CN-104 is already applied
        august = log.outstanding("WH-NORTH", "ACME", date(2026, 8, 1), date(2026, 8, 31))
        self.assertEqual([c.credit_ref for c in august], ["CN-118"])  # CN-121 rejected

    def test_a_credit_line_on_the_invoice_satisfies_it(self):
        engine = build_engine()
        invoice = engine.read(INVOICES / "INV-4482.csv")
        from invoice_audit.models import InvoiceLine
        from invoice_audit.money import money

        invoice.lines.append(
            InvoiceLine(
                line_no=99,
                description="Credit CN-118",
                quantity=Decimal(1),
                amount=money("-310.00"),
            )
        )
        invoice.stated_total = money("23535.00")
        result = engine.audit(invoice)
        self.assertNotIn("MISSING_CREDIT", flag_ids(result))

    def test_without_a_log_the_check_is_skipped_and_said_so(self):
        engine = build_engine(credit_log=NoCreditLog())
        result = engine.audit_path(INVOICES / "INV-4482.csv")
        self.assertNotIn("MISSING_CREDIT", flag_ids(result))
        self.assertTrue(any("MISSING_CREDIT" in n for n in result.notes))


# --------------------------------------------------------------------------
# STORAGE_AGING_ERROR
# --------------------------------------------------------------------------


class SnapshotTests(unittest.TestCase):
    def test_surcharge_on_more_pallets_than_qualify(self):
        result = build_engine().audit_path(INVOICES / "INV-4482.csv")
        aging = [f for f in result.flags if f.flag_id == "STORAGE_AGING_ERROR"]
        self.assertEqual(len(aging), 1)
        self.assertEqual(aging[0].evidence["qualifying_pallets"], 9)
        self.assertEqual(aging[0].delta, Decimal("27.00"))  # 3 x 9.00

    def test_already_shipped_pallets_are_named(self):
        result = build_engine().audit_path(INVOICES / "INV-4482.csv")
        aging = [f for f in result.flags if f.flag_id == "STORAGE_AGING_ERROR"][0]
        self.assertEqual(aging.evidence["already_shipped"], ["P-010", "P-011", "P-012"])
        self.assertIn("already shipped", aging.message)

    def test_a_pallet_stops_aging_when_it_ships(self):
        pallet = PalletRecord("P-1", "W", "C", date(2026, 1, 1), date(2026, 2, 1))
        self.assertEqual(pallet.age_days_at(date(2026, 6, 1)), 31)

    def test_on_hand_window(self):
        pallet = PalletRecord("P-1", "W", "C", date(2026, 1, 1), date(2026, 3, 1))
        self.assertTrue(pallet.on_hand_during(date(2026, 2, 1), date(2026, 2, 28)))
        self.assertFalse(pallet.on_hand_during(date(2026, 4, 1), date(2026, 4, 30)))

    def test_billing_within_the_supported_count_is_clean(self):
        engine = build_engine()
        invoice = engine.read(INVOICES / "INV-4482.csv")
        from invoice_audit.money import money

        lts = [l for l in invoice.lines if "Long-term" in l.description][0]
        lts.quantity = Decimal(9)
        lts.amount = money("81.00")
        invoice.stated_total = money("23818.00")
        result = engine.audit(invoice)
        self.assertNotIn("STORAGE_AGING_ERROR", flag_ids(result))

    def test_without_snapshots_the_check_is_skipped_and_said_so(self):
        engine = build_engine(snapshots=NoSnapshots())
        result = engine.audit_path(INVOICES / "INV-4482.csv")
        self.assertNotIn("STORAGE_AGING_ERROR", flag_ids(result))
        self.assertTrue(any("STORAGE_AGING_ERROR" in n for n in result.notes))

    def test_an_unknown_client_returns_no_position(self):
        """A gap in the feed must not read as 'nothing qualifies'."""
        snaps = CsvSnapshots.from_file(SNAPSHOTS)
        self.assertIsNone(
            snaps.position("WH-SOUTH", "NOBODY", date(2026, 8, 1), date(2026, 8, 31))
        )


# --------------------------------------------------------------------------
# The queue UI
# --------------------------------------------------------------------------



class BundleTests(unittest.TestCase):
    """The UI is now a built React app, not a server-rendered template."""

    def test_flag_rows_carry_age_and_escalation_for_the_page(self):
        store = FlagStore()
        old = datetime.now(timezone.utc) - timedelta(days=30)
        record = store.upsert(
            Flag(
                flag_id="RATE_DRIFT", invoice_no="INV-1", message="m",
                warehouse_id="WH", line_no=1, expected="a", actual="b",
                created_at=old,
            ),
            period_month="2026-07", now=old,
        )
        payload = record_json(record)
        self.assertGreaterEqual(payload["age_days"], 30)
        self.assertTrue(payload["escalated"])

    @unittest.skipUnless(UI_DIST.exists(), "UI not built")
    def test_the_built_bundle_is_self_contained_except_for_fonts(self):
        html = (UI_DIST / "index.html").read_text(encoding="utf-8")
        external = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
        self.assertTrue(all("fonts.g" in u for u in external), external)

    @unittest.skipUnless(UI_DIST.exists(), "UI not built")
    def test_the_bundle_has_assets(self):
        self.assertTrue(list((UI_DIST / "assets").glob("*.js")))
        self.assertTrue(list((UI_DIST / "assets").glob("*.css")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
