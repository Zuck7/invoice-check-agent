"""Regression suite.

The sample invoices under ``data/invoices`` are the seed of the labelled set
phase 0 calls for: one known-clean month and one with a seeded error per flag.
Every assertion here is a claim about money, so expected values are written out
by hand rather than computed from the code under test.
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from invoice_audit import (
    AuditEngine,
    CsvOrderData,
    HistoryStore,
    NoOrderData,
    RateCardStore,
    Tolerances,
    read_invoice,
)
from invoice_audit.checks import normalise_uom
from invoice_audit.flags import FLAGS, Severity
from invoice_audit.intake import IntakeError
from invoice_audit.models import Flag, InvoiceLine
from invoice_audit.money import money, quantity
from invoice_audit.normalize import Normalizer, Suggestion, canonical

ROOT = Path(__file__).resolve().parent.parent
CARDS = ROOT / "data" / "rate_cards"
WMS = ROOT / "data" / "wms" / "counts.csv"
INVOICES = ROOT / "data" / "invoices"


def build_engine(**kw) -> AuditEngine:
    defaults = dict(
        store=RateCardStore.from_dir(CARDS),
        order_data=CsvOrderData.from_file(WMS),
        history=HistoryStore(),
    )
    defaults.update(kw)
    return AuditEngine(**defaults)


def flag_ids(result) -> list[str]:
    return sorted(f.flag_id for f in result.flags)


def one(result, flag_id: str) -> Flag:
    matches = [f for f in result.flags if f.flag_id == flag_id]
    assert len(matches) == 1, f"expected exactly one {flag_id}, got {len(matches)}"
    return matches[0]


# --------------------------------------------------------------------------


class MoneyTests(unittest.TestCase):
    def test_rejects_float(self):
        with self.assertRaises(TypeError):
            money(18.0)
        with self.assertRaises(TypeError):
            quantity(3200.0)

    def test_quantises_to_cents(self):
        self.assertEqual(money("18.005"), Decimal("18.01"))
        self.assertEqual(money(""), Decimal("0.00"))

    def test_quantity_keeps_precision(self):
        self.assertEqual(quantity("12.375"), Decimal("12.375"))


class CanonicalTests(unittest.TestCase):
    def test_folds_punctuation_and_ampersand(self):
        self.assertEqual(canonical("Pick & pack · per order"), "pick and pack per order")

    def test_folds_accents(self):
        self.assertEqual(canonical("Réception"), "reception")

    def test_uom_synonyms(self):
        self.assertEqual(normalise_uom("pallet/mo"), "pallet_month")
        self.assertEqual(normalise_uom("pallet_month"), "pallet_month")
        self.assertEqual(normalise_uom("Pallets"), "pallet")
        self.assertIsNone(normalise_uom(""))


# --------------------------------------------------------------------------


class CleanInvoiceTests(unittest.TestCase):
    def test_clean_invoice_raises_nothing(self):
        result = build_engine().audit_path(INVOICES / "INV-4471.csv")
        self.assertEqual(result.flags, [], f"unexpected: {flag_ids(result)}")
        self.assertTrue(result.clean)
        self.assertFalse(result.blocked)
        self.assertEqual(result.exposure, Decimal("0.00"))

    def test_clean_invoice_totals_match_the_spec_sample(self):
        invoice = read_invoice(INVOICES / "INV-4471.csv")
        self.assertEqual(invoice.stated_total, Decimal("22136.00"))
        self.assertEqual(invoice.line_total, Decimal("22136.00"))

    def test_every_line_maps_to_the_card(self):
        result = build_engine().audit_path(INVOICES / "INV-4471.csv")
        self.assertTrue(all(l.mapped for l in result.invoice.lines))
        self.assertTrue(all(l.mapped_by == "alias" for l in result.invoice.lines))

    def test_card_selected_is_the_july_version(self):
        result = build_engine().audit_path(INVOICES / "INV-4471.csv")
        self.assertEqual(result.rate_card.version, "2026.07")


class SeededErrorTests(unittest.TestCase):
    def setUp(self):
        self.result = build_engine().audit_path(INVOICES / "INV-4482.csv")

    def test_finds_exactly_the_seeded_flags(self):
        self.assertEqual(
            flag_ids(self.result),
            [
                "MATH_ERROR",
                "MISSING_LINE",
                "QTY_VARIANCE",
                "RATE_DRIFT",
                "SURCHARGE_BASE_WRONG",
                "UNKNOWN",
                "WRONG_CLIENT_RATES",
            ],
        )

    def test_rate_drift_on_storage(self):
        flag = one(self.result, "RATE_DRIFT")
        self.assertEqual(flag.line_no, 1)
        self.assertEqual(flag.delta, Decimal("240.00"))  # (20.00 - 18.00) x 120
        self.assertEqual(flag.rate_card_version, "BUY-ACME-WHNORTH@2026.07")
        self.assertEqual(flag.evidence["rate_key"], "storage.per_pallet_month")

    def test_qty_variance_on_pick_and_pack(self):
        flag = one(self.result, "QTY_VARIANCE")
        self.assertEqual(flag.line_no, 2)
        self.assertEqual(flag.delta, Decimal("375.00"))  # 150 x 2.50
        self.assertEqual(flag.evidence["wms_count"], "3050")

    def test_wrong_client_rates_names_the_other_client(self):
        flag = one(self.result, "WRONG_CLIENT_RATES")
        self.assertEqual(flag.line_no, 3)
        self.assertEqual(flag.delta, Decimal("648.00"))  # (0.42 - 0.30) x 5400
        self.assertEqual(flag.evidence["matching_clients"], ["BETA"])

    def test_wrong_client_rates_wins_over_plain_drift(self):
        """The more specific diagnosis must not also emit RATE_DRIFT."""
        drift_lines = [f.line_no for f in self.result.flags if f.flag_id == "RATE_DRIFT"]
        self.assertNotIn(3, drift_lines)

    def test_surcharge_base(self):
        flag = one(self.result, "SURCHARGE_BASE_WRONG")
        self.assertEqual(flag.line_no, 4)
        self.assertEqual(flag.delta, Decimal("475.00"))  # 9975 - 9500
        self.assertEqual(flag.evidence["implied_percent"], "5.00")

    def test_math_error_on_extension(self):
        flag = one(self.result, "MATH_ERROR")
        self.assertEqual(flag.line_no, 5)
        self.assertEqual(flag.delta, Decimal("28.00"))  # 644 - 616

    def test_unknown_line_blocks_rerating(self):
        flag = one(self.result, "UNKNOWN")
        self.assertEqual(flag.line_no, 6)
        self.assertEqual(flag.delta, Decimal("450.00"))
        self.assertTrue(flag.blocks_rerate)
        self.assertTrue(self.result.blocked)

    def test_missing_line_is_negative_exposure(self):
        flag = one(self.result, "MISSING_LINE")
        self.assertIsNone(flag.line_no)
        self.assertEqual(flag.delta, Decimal("-240.00"))  # 40 x 6.00 never billed
        self.assertEqual(flag.evidence["rate_key"], "receiving.per_pallet")

    def test_net_exposure(self):
        # 240 + 375 + 648 + 475 + 28 + 450 - 240
        self.assertEqual(self.result.exposure, Decimal("1976.00"))

    def test_every_flag_carries_provenance(self):
        for flag in self.result.flags:
            with self.subTest(flag=flag.flag_id):
                self.assertTrue(flag.message)
                self.assertIn(flag.flag_id, FLAGS)
                if flag.flag_id != "MISSING_LINE":
                    self.assertIsNotNone(flag.rate_card_version)


# --------------------------------------------------------------------------


class TotalMismatchTests(unittest.TestCase):
    def test_stated_total_that_does_not_match_the_lines(self):
        invoice = read_invoice(INVOICES / "INV-4471.csv")
        invoice.stated_total = money("22200.00")
        result = build_engine().audit(invoice)
        flag = one(result, "MATH_ERROR")
        self.assertIsNone(flag.line_no)
        self.assertEqual(flag.delta, Decimal("64.00"))
        self.assertEqual(flag.evidence["scope"], "invoice_total")


class UomTests(unittest.TestCase):
    def test_storage_billed_per_pallet_instead_of_pallet_month(self):
        invoice = read_invoice(INVOICES / "INV-4471.csv")
        invoice.lines[1].uom = "pallet"
        result = build_engine().audit(invoice)
        flag = one(result, "UOM_MISMATCH")
        self.assertEqual(flag.line_no, 2)
        self.assertEqual(flag.expected, "per pallet month")


class StaleCardTests(unittest.TestCase):
    def test_period_with_no_effective_card(self):
        invoice = read_invoice(INVOICES / "INV-4471.csv")
        invoice.period_start = date(2025, 3, 1)
        invoice.period_end = date(2025, 3, 31)
        result = build_engine().audit(invoice)
        flag = one(result, "RATE_CARD_VERSION_STALE")
        self.assertIsNone(flag.rate_card_version)
        self.assertIn("2026.01", " ".join(flag.evidence["known_versions"]))
        # Nothing else fires: without a card there is nothing to price against.
        self.assertEqual(flag_ids(result), ["RATE_CARD_VERSION_STALE"])

    def test_card_chosen_by_service_period_not_invoice_date(self):
        """June service invoiced in August prices on the January card."""
        invoice = read_invoice(INVOICES / "INV-4471.csv")
        invoice.period_start = date(2026, 6, 1)
        invoice.period_end = date(2026, 6, 30)
        result = build_engine().audit(invoice)
        self.assertEqual(result.rate_card.version, "2026.01")
        # That card prices storage at 17.00, so the 18.00 billed now drifts.
        drift = [f for f in result.flags if f.flag_id == "RATE_DRIFT"]
        self.assertTrue(drift)


class DuplicateTests(unittest.TestCase):
    def test_identical_resubmission_is_a_duplicate(self):
        engine = build_engine()
        first = engine.audit_path(INVOICES / "INV-4471.csv")
        self.assertEqual(first.flags, [])
        second = engine.audit_path(INVOICES / "INV-4471.csv")
        flag = one(second, "DUPLICATE_INVOICE")
        self.assertEqual(flag.evidence["kind"], "exact_duplicate")
        self.assertEqual(flag.delta, Decimal("22136.00"))

    def test_same_number_changed_content_is_a_revision(self):
        engine = build_engine()
        engine.audit_path(INVOICES / "INV-4471.csv")
        revised = read_invoice(INVOICES / "INV-4471.csv")
        revised.lines[0].amount = money("250.00")
        revised.stated_total = money("22146.00")
        result = engine.audit(revised)
        flag = one(result, "DUPLICATE_INVOICE")
        self.assertEqual(flag.evidence["kind"], "revision")
        self.assertEqual(flag.delta, Decimal("0.00"))

    def test_history_round_trips_through_disk(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            engine = build_engine(history=HistoryStore(path))
            engine.audit_path(INVOICES / "INV-4471.csv")
            engine.history.flush()

            reopened = build_engine(history=HistoryStore(path))
            result = reopened.audit_path(INVOICES / "INV-4471.csv")
            self.assertEqual(
                one(result, "DUPLICATE_INVOICE").evidence["kind"], "exact_duplicate"
            )


class MissingDataTests(unittest.TestCase):
    def test_without_wms_quantity_checks_are_skipped_and_said_so(self):
        engine = build_engine(order_data=NoOrderData())
        result = engine.audit_path(INVOICES / "INV-4482.csv")
        self.assertNotIn("QTY_VARIANCE", flag_ids(result))
        self.assertNotIn("MISSING_LINE", flag_ids(result))
        self.assertTrue(any("not fully audited" in n for n in result.notes))

    def test_unknown_period_is_not_treated_as_zero_activity(self):
        """A gap in the WMS feed must never manufacture MISSING_LINE flags."""
        invoice = read_invoice(INVOICES / "INV-4471.csv")
        invoice.period_start = date(2026, 7, 2)  # no counts for this window
        result = build_engine().audit(invoice)
        self.assertNotIn("MISSING_LINE", flag_ids(result))
        self.assertNotIn("QTY_VARIANCE", flag_ids(result))


class ToleranceTests(unittest.TestCase):
    def test_qty_tolerance_suppresses_small_variance(self):
        engine = build_engine(
            tolerances=Tolerances(qty_tolerance_pct=Decimal("5"))
        )
        result = engine.audit_path(INVOICES / "INV-4482.csv")
        # 3200 vs 3050 is 4.9%, inside a 5% tolerance.
        self.assertNotIn("QTY_VARIANCE", flag_ids(result))

    def test_default_tolerance_flags_it(self):
        result = build_engine().audit_path(INVOICES / "INV-4482.csv")
        self.assertIn("QTY_VARIANCE", flag_ids(result))

    def test_cent_rounding_is_absorbed(self):
        invoice = read_invoice(INVOICES / "INV-4471.csv")
        invoice.lines[0].amount = money("240.01")
        invoice.stated_total = money("22136.01")
        result = build_engine().audit(invoice)
        self.assertEqual(result.flags, [], f"unexpected: {flag_ids(result)}")


# --------------------------------------------------------------------------


class NormalizerTests(unittest.TestCase):
    def setUp(self):
        self.card = RateCardStore.from_dir(CARDS).for_invoice(
            "WH-NORTH", "ACME", date(2026, 7, 1)
        )

    def _map(self, description: str, mapper=None) -> InvoiceLine:
        line = InvoiceLine(
            line_no=1,
            description=description,
            quantity=Decimal(1),
            amount=money("1.00"),
        )
        normalizer = (
            Normalizer(card=self.card, mapper=mapper)
            if mapper
            else Normalizer(card=self.card)
        )
        return normalizer.apply(line)

    def test_alias_variants_all_land_on_the_same_key(self):
        for text in (
            "Pick & pack · per order",
            "pick and pack per order",
            "PICK/PACK PER ORDER",
            "Order fulfilment",
        ):
            with self.subTest(text=text):
                self.assertEqual(self._map(text).rate_key, "pickpack.per_order")

    def test_unrecognised_line_stays_unmapped(self):
        line = self._map("Container destuff fee")
        self.assertIsNone(line.rate_key)
        self.assertIsNone(line.mapped_by)

    def test_confident_model_suggestion_is_accepted(self):
        class Mapper:
            def suggest(self, description, candidates):
                return Suggestion("receiving.per_pallet", Decimal("0.95"), "shape")

        line = self._map("Goods in, per skid", mapper=Mapper())
        self.assertEqual(line.rate_key, "receiving.per_pallet")
        self.assertEqual(line.mapped_by, "model")

    def test_low_confidence_suggestion_is_rejected_not_priced(self):
        class Mapper:
            def suggest(self, description, candidates):
                return Suggestion("receiving.per_pallet", Decimal("0.40"), "guess")

        line = self._map("Goods in, per skid", mapper=Mapper())
        self.assertIsNone(line.rate_key)
        self.assertEqual(line.mapped_by, "model-rejected")

    def test_rejected_suggestion_produces_both_flags(self):
        class Mapper:
            def suggest(self, description, candidates):
                return Suggestion("receiving.per_pallet", Decimal("0.40"), "guess")

        engine = build_engine(mapper=Mapper())
        result = engine.audit_path(INVOICES / "INV-4482.csv")
        self.assertIn("LOW_CONFIDENCE_EXTRACTION", flag_ids(result))
        self.assertIn("UNKNOWN", flag_ids(result))


class TaxonomyTests(unittest.TestCase):
    def test_flag_outside_the_taxonomy_is_refused(self):
        with self.assertRaises(KeyError):
            Flag(flag_id="RATE_LOOKS_OFF", invoice_no="X", message="nope")

    def test_severities_are_assigned(self):
        for flag_id, definition in FLAGS.items():
            with self.subTest(flag=flag_id):
                self.assertIsInstance(definition.severity, Severity)


class IntakeTests(unittest.TestCase):
    def test_missing_metadata_is_a_failure_not_a_flag(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv"
            path.write_text(
                "# invoice_no: X\nline_no,description,quantity,amount\n1,a,1,1.00\n",
                encoding="utf-8",
            )
            with self.assertRaises(IntakeError) as ctx:
                read_invoice(path)
            self.assertIn("missing metadata", str(ctx.exception))

    def test_pdf_is_deferred_to_phase_two(self):
        with self.assertRaises(IntakeError) as ctx:
            read_invoice(Path("nowhere/invoice.pdf"))
        self.assertIn("phase 2", str(ctx.exception))

    def test_content_hash_ignores_presentation(self):
        a = read_invoice(INVOICES / "INV-4471.csv")
        b = read_invoice(INVOICES / "INV-4471.csv")
        b.lines[0].description = "  RECEIVING · PER PALLET  "
        self.assertEqual(a.content_hash(), b.content_hash())

    def test_content_hash_changes_with_money(self):
        a = read_invoice(INVOICES / "INV-4471.csv")
        b = read_invoice(INVOICES / "INV-4471.csv")
        b.lines[0].amount = money("241.00")
        self.assertNotEqual(a.content_hash(), b.content_hash())


if __name__ == "__main__":
    unittest.main(verbosity=2)
