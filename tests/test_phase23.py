"""Phase 2 (mapping, retries, scoring) and phase 3 (queue lifecycle, trends)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from invoice_audit import AuditEngine, CsvOrderData, HistoryStore, RateCardStore
from invoice_audit.credits import CsvCreditLog
from invoice_audit.snapshots import CsvSnapshots
from invoice_audit.flagstore import ESCALATION_DAYS, FlagStore, Status
from invoice_audit.models import Flag
from invoice_audit.money import money
from invoice_audit.normalize import FuzzyMapper
from invoice_audit.scoring import load_cases, score

ROOT = Path(__file__).resolve().parent.parent
CARDS = ROOT / "data" / "rate_cards"
WMS = ROOT / "data" / "wms" / "counts.csv"
CREDITS = ROOT / "data" / "credits" / "log.csv"
SNAPSHOTS = ROOT / "data" / "snapshots" / "pallets.csv"
INVOICES = ROOT / "data" / "invoices"
SAMPLES = ROOT / "data" / "samples"
LABELS = ROOT / "data" / "labeled"


def build_engine(**kw) -> AuditEngine:
    defaults = dict(
        store=RateCardStore.from_dir(CARDS),
        order_data=CsvOrderData.from_file(WMS),
        credit_log=CsvCreditLog.from_file(CREDITS),
        snapshots=CsvSnapshots.from_file(SNAPSHOTS),
        history=HistoryStore(),
        flag_store=FlagStore(),
        mapper=FuzzyMapper(),
    )
    defaults.update(kw)
    return AuditEngine(**defaults)


def flag_ids(result) -> list[str]:
    return sorted(f.flag_id for f in result.flags)


class FingerprintTests(unittest.TestCase):
    def _flag(self, **kw) -> Flag:
        base = dict(
            flag_id="RATE_DRIFT",
            invoice_no="INV-1",
            message="m",
            warehouse_id="WH",
            line_no=1,
            expected="$18.00",
            actual="$20.00",
        )
        base.update(kw)
        return Flag(**base)

    def test_same_finding_same_fingerprint(self):
        self.assertEqual(self._flag().fingerprint, self._flag().fingerprint)

    def test_message_wording_does_not_change_identity(self):
        self.assertEqual(
            self._flag().fingerprint, self._flag(message="reworded").fingerprint
        )

    def test_a_different_number_is_a_different_finding(self):
        self.assertNotEqual(
            self._flag().fingerprint, self._flag(actual="$21.00").fingerprint
        )

    def test_different_line_is_a_different_finding(self):
        self.assertNotEqual(
            self._flag().fingerprint, self._flag(line_no=2).fingerprint
        )


class QueueLifecycleTests(unittest.TestCase):
    def test_reaudit_does_not_duplicate_the_queue(self):
        engine = build_engine()
        engine.audit_path(INVOICES / "INV-4482.csv")
        first = len(engine.flag_store)
        engine.audit_path(INVOICES / "INV-4482.csv")
        # Same findings, one extra DUPLICATE_INVOICE for the re-submission.
        self.assertEqual(len(engine.flag_store), first + 1)
        self.assertGreaterEqual(first, 9)

    def test_sightings_accumulate(self):
        engine = build_engine()
        engine.audit_path(INVOICES / "INV-4482.csv")
        target = [r for r in engine.flag_store.all() if r.flag_id == "RATE_DRIFT"][0]
        engine.audit_path(INVOICES / "INV-4482.csv")
        self.assertEqual(engine.flag_store.get(target.fingerprint).sightings, 2)

    def test_resolved_flag_stays_resolved_across_reruns(self):
        engine = build_engine()
        engine.audit_path(INVOICES / "INV-4482.csv")
        target = [r for r in engine.flag_store.all() if r.flag_id == "RATE_DRIFT"][0]
        engine.flag_store.resolve(target.fingerprint, "credit CN-118", by="sam")

        engine.audit_path(INVOICES / "INV-4482.csv")
        again = engine.flag_store.get(target.fingerprint)
        self.assertEqual(again.status, Status.RESOLVED)
        self.assertEqual(again.resolved_by, "sam")
        self.assertNotIn(
            target.fingerprint, [r.fingerprint for r in engine.flag_store.open_records()]
        )

    def test_prefix_lookup(self):
        engine = build_engine()
        engine.audit_path(INVOICES / "INV-4482.csv")
        target = engine.flag_store.all()[0]
        self.assertEqual(
            engine.flag_store.get(target.fingerprint[:6]).fingerprint,
            target.fingerprint,
        )

    def test_unknown_fingerprint_raises(self):
        with self.assertRaises(KeyError):
            FlagStore().get("nope")

    def test_dismiss_is_distinct_from_resolve(self):
        engine = build_engine()
        engine.audit_path(INVOICES / "INV-4482.csv")
        target = engine.flag_store.all()[0]
        engine.flag_store.resolve(
            target.fingerprint, "card was stale on our side", by="sam", dismissed=True
        )
        self.assertEqual(
            engine.flag_store.get(target.fingerprint).status, Status.DISMISSED
        )

    def test_store_round_trips_through_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flags.json"
            engine = build_engine(flag_store=FlagStore(path))
            engine.audit_path(INVOICES / "INV-4482.csv")
            target = engine.flag_store.all()[0]
            engine.flag_store.resolve(target.fingerprint, "done", by="sam")
            engine.flag_store.flush()

            reopened = FlagStore(path)
            self.assertEqual(len(reopened), len(engine.flag_store))
            self.assertEqual(
                reopened.get(target.fingerprint).resolution, "done"
            )


class EscalationTests(unittest.TestCase):
    def _store_with_old_flag(self, age_days: int) -> tuple[FlagStore, str]:
        store = FlagStore()
        old = datetime.now(timezone.utc) - timedelta(days=age_days)
        flag = Flag(
            flag_id="RATE_DRIFT",
            invoice_no="INV-1",
            message="m",
            warehouse_id="WH",
            line_no=1,
            expected="$18.00",
            actual="$20.00",
            created_at=old,
        )
        record = store.upsert(flag, period_month="2026-07", now=old)
        return store, record.fingerprint

    def test_fresh_flag_is_not_escalated(self):
        store, fp = self._store_with_old_flag(1)
        self.assertFalse(store.get(fp).escalated())
        self.assertEqual(store.escalated(), [])

    def test_flag_past_the_window_escalates(self):
        store, fp = self._store_with_old_flag(ESCALATION_DAYS + 2)
        self.assertTrue(store.get(fp).escalated())
        self.assertEqual(len(store.escalated()), 1)

    def test_resolved_flags_never_escalate(self):
        store, fp = self._store_with_old_flag(ESCALATION_DAYS + 30)
        store.resolve(fp, "settled", by="sam")
        self.assertFalse(store.get(fp).escalated())
        self.assertEqual(store.escalated(), [])


class TrendTests(unittest.TestCase):
    def test_groups_by_warehouse_and_month(self):
        engine = build_engine()
        engine.audit_path(INVOICES / "INV-4482.csv")
        trends = engine.flag_store.trends()
        self.assertIn("WH-NORTH", trends)
        self.assertIn("2026-08", trends["WH-NORTH"])
        self.assertEqual(trends["WH-NORTH"]["2026-08"]["by_flag"]["RATE_DRIFT"], 1)

    def test_repeat_offenders_need_more_than_one_month(self):
        store = FlagStore()
        for month in ("2026-06", "2026-07", "2026-08"):
            store.upsert(
                Flag(
                    flag_id="RATE_DRIFT",
                    invoice_no=f"INV-{month}",
                    message="m",
                    warehouse_id="WH-NORTH",
                    line_no=1,
                    expected="$18.00",
                    actual="$20.00",
                ),
                period_month=month,
            )
        repeats = store.repeat_offenders()
        self.assertEqual(repeats, [("WH-NORTH", "RATE_DRIFT", 3)])

    def test_a_single_month_is_not_a_pattern(self):
        store = FlagStore()
        store.upsert(
            Flag(
                flag_id="RATE_DRIFT",
                invoice_no="INV-1",
                message="m",
                warehouse_id="WH-NORTH",
                line_no=1,
                expected="a",
                actual="b",
            ),
            period_month="2026-08",
        )
        self.assertEqual(store.repeat_offenders(), [])


class DuplicateChargeTests(unittest.TestCase):
    def test_same_period_billed_on_two_invoices(self):
        engine = build_engine()
        engine.audit_path(INVOICES / "INV-4471.csv")
        result = engine.audit_path(SAMPLES / "INV-4501.csv")
        dupes = [f for f in result.flags if f.flag_id == "DUPLICATE_CHARGE"]
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0].evidence["scope"], "across_invoices")
        self.assertEqual(dupes[0].evidence["prior_invoice_no"], "INV-4471")

    def test_a_different_period_is_not_a_duplicate(self):
        """Billing storage every month is the job, not a fault."""
        engine = build_engine()
        engine.audit_path(INVOICES / "INV-4471.csv")   # July
        result = engine.audit_path(INVOICES / "INV-4482.csv")  # August
        self.assertNotIn("DUPLICATE_CHARGE", flag_ids(result))

    def test_identical_line_twice_on_one_invoice(self):
        engine = build_engine()
        invoice = engine.read(INVOICES / "INV-4471.csv")
        clone = invoice.lines[1]
        import copy

        repeat = copy.deepcopy(clone)
        repeat.line_no = 99
        invoice.lines.append(repeat)
        invoice.stated_total = money("24296.00")
        result = engine.audit(invoice)
        dupes = [f for f in result.flags if f.flag_id == "DUPLICATE_CHARGE"]
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0].evidence["scope"], "within_invoice")
        self.assertEqual(dupes[0].evidence["first_line_no"], 2)

    def test_split_lines_are_not_duplicates(self):
        """Same activity, different quantities: a legitimate split."""
        engine = build_engine()
        invoice = engine.read(INVOICES / "INV-4471.csv")
        import copy

        half = copy.deepcopy(invoice.lines[1])
        half.line_no = 99
        half.quantity = Decimal(60)
        half.amount = money("1080.00")
        invoice.lines.append(half)
        invoice.stated_total = money("23216.00")
        result = engine.audit(invoice)
        self.assertNotIn("DUPLICATE_CHARGE", flag_ids(result))


class MissingLineAcrossInvoicesTests(unittest.TestCase):
    def test_activity_billed_on_an_earlier_invoice_is_not_missing(self):
        """A correction invoice must not re-flag everything on the main one."""
        engine = build_engine()
        engine.audit_path(INVOICES / "INV-4471.csv")
        result = engine.audit_path(SAMPLES / "INV-4501.csv")
        self.assertNotIn("MISSING_LINE", flag_ids(result))

    def test_without_that_history_it_still_flags(self):
        engine = build_engine()
        result = engine.audit_path(SAMPLES / "INV-4501.csv")
        self.assertIn("MISSING_LINE", flag_ids(result))


class ScoringTests(unittest.TestCase):
    def test_labelled_set_scores_perfectly(self):
        cases = load_cases(LABELS, ROOT)
        card = score(cases, build_engine)
        self.assertEqual(card.false_negatives, 0, card.to_dict()["failures"])
        self.assertEqual(card.false_positives, 0, card.to_dict()["failures"])
        self.assertEqual(card.recall, 1.0)
        self.assertEqual(card.precision, 1.0)

    def test_cases_are_isolated_from_each_other(self):
        """Two cases covering the same period must not contaminate each other."""
        cases = load_cases(LABELS, ROOT)
        names = [c.invoice_path.name for c in cases]
        self.assertIn("INV-4471.csv", names)
        self.assertIn("INV-4495.csv", names)  # same July period, correct invoice
        card = score(cases, build_engine)
        spurious = [k for c in card.cases for k in c.spurious]
        self.assertEqual(spurious, [])

    def test_a_missed_flag_shows_up_as_recall_loss(self):
        cases = load_cases(LABELS, ROOT)
        # Alias-only mapping cannot resolve the reworded invoice, so the engine
        # under-performs and the scorecard must say so rather than hide it.
        from invoice_audit.normalize import NullMapper

        card = score(cases, lambda: build_engine(mapper=NullMapper()))
        self.assertGreater(card.false_positives, 0)
        self.assertLess(card.precision, 1.0)

    def test_prior_invoices_are_replayed_for_duplicate_cases(self):
        cases = {c.invoice_path.name: c for c in load_cases(LABELS, ROOT)}
        self.assertEqual(
            [p.name for p in cases["INV-4501.csv"].prior], ["INV-4471.csv"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
