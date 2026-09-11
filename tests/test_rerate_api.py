"""Sell-card re-rating, the workspace, and the HTTP API."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from decimal import Decimal
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from invoice_audit import AuditEngine, CsvOrderData, HistoryStore, RateCardStore
from invoice_audit.credits import CsvCreditLog
from invoice_audit.flagstore import FlagStore
from invoice_audit.rerate import SellCardStore, rerate
from invoice_audit.server import ApiHandler
from invoice_audit.snapshots import CsvSnapshots
from invoice_audit.workspace import (
    MAX_UPLOAD_BYTES,
    Paths,
    UploadRejected,
    Workspace,
)

ROOT = Path(__file__).resolve().parent.parent
INVOICES = ROOT / "data" / "invoices"


def build_engine() -> AuditEngine:
    return AuditEngine(
        store=RateCardStore.from_dir(ROOT / "data" / "rate_cards"),
        order_data=CsvOrderData.from_file(ROOT / "data" / "wms" / "counts.csv"),
        credit_log=CsvCreditLog.from_file(ROOT / "data" / "credits" / "log.csv"),
        snapshots=CsvSnapshots.from_file(ROOT / "data" / "snapshots" / "pallets.csv"),
        history=HistoryStore(),
        flag_store=FlagStore(),
    )


def sell_cards() -> SellCardStore:
    return SellCardStore.from_dir(ROOT / "data" / "sell_cards")


class RerateTests(unittest.TestCase):
    def test_clean_invoice_reproduces_the_spec_margin_table(self):
        """The worked example in spec.md, computed rather than transcribed."""
        result = build_engine().audit_path(INVOICES / "INV-4471.csv")
        rr = rerate(result, sell_cards())
        self.assertFalse(rr.blocked)
        self.assertEqual(rr.buy_total, Decimal("22136.00"))
        self.assertEqual(rr.sell_total, Decimal("29076.00"))
        self.assertEqual(rr.margin, Decimal("6940.00"))

    def test_per_line_margins_match_the_table(self):
        rr = rerate(build_engine().audit_path(INVOICES / "INV-4471.csv"), sell_cards())
        by_key = {l.rate_key: l for l in rr.lines}
        self.assertEqual(by_key["receiving.per_pallet"].margin, Decimal("120.00"))
        self.assertEqual(by_key["storage.per_pallet_month"].margin, Decimal("840.00"))
        self.assertEqual(by_key["pickpack.per_order"].margin, Decimal("4000.00"))
        self.assertEqual(by_key["pick.per_extra_unit"].margin, Decimal("810.00"))
        self.assertEqual(by_key["parcel.passthrough"].margin, Decimal("950.00"))
        self.assertEqual(by_key["freight.b2b_per_pallet_out"].margin, Decimal("220.00"))

    def test_margin_is_per_line_not_a_flat_markup(self):
        """The whole reason re-rating needs a card rather than a multiplier."""
        rr = rerate(build_engine().audit_path(INVOICES / "INV-4471.csv"), sell_cards())
        pcts = {str(l.margin_pct) for l in rr.lines}
        self.assertGreater(len(pcts), 3, pcts)

    def test_an_unpriceable_line_blocks_the_whole_invoice(self):
        result = build_engine().audit_path(INVOICES / "INV-4482.csv")
        rr = rerate(result, sell_cards())
        self.assertTrue(rr.blocked)
        self.assertTrue(any("could not be priced" in b for b in rr.blocked_by))

    def test_quantity_variance_bills_the_client_our_own_count(self):
        """An inbound error must not become an outbound one."""
        result = build_engine().audit_path(INVOICES / "INV-4482.csv")
        rr = rerate(result, sell_cards())
        line = next(l for l in rr.lines if l.rate_key == "pickpack.per_order")
        self.assertEqual(line.quantity, Decimal("3050"))
        self.assertEqual(line.quantity_source, "order data")
        self.assertIn("3200", line.note)

    def test_no_sell_card_blocks_rather_than_guessing_a_markup(self):
        result = build_engine().audit_path(INVOICES / "INV-4471.csv")
        rr = rerate(result, SellCardStore([]))
        self.assertTrue(rr.blocked)
        self.assertIn("no sell card", rr.blocked_by[0])

    def test_unresolved_high_flags_warn_but_do_not_block(self):
        result = build_engine().audit_path(INVOICES / "INV-4482.csv")
        rr = rerate(result, sell_cards())
        self.assertTrue(any("high-severity" in w for w in rr.warnings))

    def test_sell_card_lookup_is_by_client_and_date(self):
        store = sell_cards()
        self.assertIsNotNone(store.for_client("ACME", date(2026, 7, 1)))
        self.assertIsNone(store.for_client("NOBODY", date(2026, 7, 1)))
        self.assertIsNone(store.for_client("ACME", date(2020, 1, 1)))


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace.load(Paths(root=ROOT, invoices=INVOICES))

    def test_no_invoice_is_silently_dropped(self):
        """Every document on disk is either audited or reported as unreadable.

        A file that vanishes between the folder and the queue is the worst
        failure mode this system has: it looks exactly like a clean month.
        """
        self.assertEqual(
            len(self.ws.results) + len(self.ws.failures),
            len(self.ws.invoice_paths()),
        )

    def test_an_unreadable_document_names_itself(self):
        for failure in self.ws.failures:
            self.assertTrue(failure["path"])
            self.assertTrue(failure["error"])

    def test_rescan_is_idempotent(self):
        """Re-reading the folder must not flag every invoice as a duplicate."""
        before = sorted(f.flag_id for r in self.ws.results for f in r.flags)
        self.ws.rescan()
        after = sorted(f.flag_id for r in self.ws.results for f in r.flags)
        self.assertEqual(before, after)
        self.assertNotIn("DUPLICATE_INVOICE", after)

    def test_summary_counts_line_up(self):
        s = self.ws.summary()
        self.assertEqual(s["invoices"], len(self.ws.results))
        self.assertEqual(s["clean"] + s["flagged"], s["invoices"])

    def test_sources_report_what_is_wired(self):
        names = {s["name"]: s["present"] for s in self.ws.summary()["sources"]}
        self.assertTrue(all(names.values()), names)

    def test_a_missing_feed_is_reported_as_missing(self):
        ws = Workspace.load(
            Paths(root=ROOT, invoices=INVOICES, credits=Path("nope.csv"))
        )
        off = [s for s in ws.summary()["sources"] if not s["present"]]
        self.assertEqual([s["name"] for s in off], ["Dispute / credit log"])

    def test_csv_invoices_always_land(self):
        routes = {r["route"] for r in self.ws.invoices()}
        self.assertIn("csv", routes)

    def test_pdf_lands_when_an_extractor_is_available(self):
        from invoice_audit import pdftext

        if not pdftext.available():
            self.skipTest("pdfplumber not installed; the PDF is a read failure")
        routes = {r["route"] for r in self.ws.invoices()}
        self.assertIn("pdf", routes)


class ApiTests(unittest.TestCase):
    TOKEN = "test-token"

    @classmethod
    def setUpClass(cls):
        cls.ws = Workspace.load(Paths(root=ROOT, invoices=INVOICES))
        handler = partial(ApiHandler, workspace=cls.ws, token=cls.TOKEN)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path):
        with urllib.request.urlopen(f"{self.base}{path}") as res:
            return res.status, json.loads(res.read())

    def post(self, path, body, token=None):
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as err:
            with err:  # close the response so the test run stays warning-free
                return err.code, json.loads(err.read())

    def test_read_endpoints(self):
        for path in ("/api/summary", "/api/flags", "/api/invoices", "/api/trends", "/api/taxonomy"):
            with self.subTest(path=path):
                status, _ = self.get(path)
                self.assertEqual(status, 200)

    def test_taxonomy_is_the_whole_closed_list(self):
        _, payload = self.get("/api/taxonomy")
        self.assertEqual(len(payload), 14)

    def test_writes_need_a_token(self):
        _, flags = self.get("/api/flags")
        fp = flags[0]["fingerprint"]
        status, _ = self.post(f"/api/flags/{fp}", {"action": "resolve", "by": "a", "note": "b"})
        self.assertEqual(status, 401)

    def test_a_wrong_token_is_refused(self):
        _, flags = self.get("/api/flags")
        fp = flags[0]["fingerprint"]
        status, _ = self.post(
            f"/api/flags/{fp}", {"action": "resolve", "by": "a", "note": "b"}, token="nope"
        )
        self.assertEqual(status, 401)

    def test_resolution_requires_provenance(self):
        _, flags = self.get("/api/flags")
        fp = flags[0]["fingerprint"]
        status, payload = self.post(
            f"/api/flags/{fp}", {"action": "resolve"}, token=self.TOKEN
        )
        self.assertEqual(status, 400)
        self.assertIn("name and a note", payload["error"])

    def test_a_valid_resolution_sticks(self):
        _, flags = self.get("/api/flags")
        target = next(f for f in flags if f["status"] == "open")
        status, payload = self.post(
            f"/api/flags/{target['fingerprint']}",
            {"action": "resolve", "by": "sam", "note": "credit received"},
            token=self.TOKEN,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "resolved")

        _, after = self.get("/api/flags")
        again = next(f for f in after if f["fingerprint"] == target["fingerprint"])
        self.assertEqual(again["status"], "resolved")
        self.assertEqual(again["resolved_by"], "sam")

    def test_unknown_fingerprint(self):
        status, _ = self.post(
            "/api/flags/deadbeef", {"action": "reopen"}, token=self.TOKEN
        )
        self.assertEqual(status, 404)

    def test_unknown_action(self):
        _, flags = self.get("/api/flags")
        status, _ = self.post(
            f"/api/flags/{flags[0]['fingerprint']}", {"action": "delete"}, token=self.TOKEN
        )
        self.assertEqual(status, 400)

    def test_flag_rows_carry_age_and_escalation(self):
        _, flags = self.get("/api/flags")
        for flag in flags:
            self.assertIn("age_days", flag)
            self.assertIn("escalated", flag)

    def test_invoice_rows_carry_margin(self):
        _, invoices = self.get("/api/invoices")
        clean = [i for i in invoices if not i["blocked_from_rerate"] and i["rerate"]]
        self.assertTrue(clean)
        self.assertIn("margin_display", clean[0]["rerate"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class UploadTests(unittest.TestCase):
    """The entry point for someone who never opens a terminal."""

    def setUp(self):
        import shutil
        import tempfile

        self.tmp = tempfile.mkdtemp()
        self.folder = Path(self.tmp) / "invoices"
        self.folder.mkdir()
        shutil.copy(INVOICES / "INV-4471.csv", self.folder)
        self.ws = Workspace.load(
            Paths(
                root=ROOT,
                invoices=self.folder,
                flags_db=Path(self.tmp) / "flags.json",
                history_db=Path(self.tmp) / "history.json",
            )
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def upload(self, name, source):
        return self.ws.ingest(name, Path(source).read_bytes())

    def test_a_csv_upload_is_audited_immediately(self):
        payload = self.upload("seeded.csv", INVOICES / "INV-4482.csv")
        self.assertEqual(payload["invoice_no"], "INV-4482")
        self.assertGreater(payload["flag_count"], 0)
        self.assertTrue(payload["blocked_from_rerate"])
        self.assertEqual(payload["filename"], "seeded.csv")

    def test_a_clean_upload_reports_margin(self):
        payload = self.upload("sept.pdf", INVOICES / "INV-4510.pdf")
        if payload is None:
            self.skipTest("no pdf extractor")
        self.assertFalse(payload["blocked_from_rerate"])
        self.assertIsNotNone(payload["rerate"])
        self.assertIn("margin_display", payload["rerate"])

    def test_the_file_lands_in_the_watched_folder(self):
        self.upload("seeded.csv", INVOICES / "INV-4482.csv")
        self.assertTrue((self.folder / "seeded.csv").exists())

    def test_re_uploading_the_same_document_is_caught(self):
        """The same invoice twice must not be payable twice."""
        self.upload("first.csv", INVOICES / "INV-4482.csv")
        again = self.upload("second.csv", INVOICES / "INV-4482.csv")
        self.assertIn("DUPLICATE_INVOICE", [f["flag"] for f in again["flags"]])

    def test_the_same_filename_is_refused_rather_than_overwritten(self):
        self.upload("one.csv", INVOICES / "INV-4482.csv")
        with self.assertRaises(UploadRejected) as ctx:
            self.upload("one.csv", INVOICES / "INV-4471.csv")
        self.assertIn("already here", str(ctx.exception))

    def test_a_traversal_filename_cannot_escape_the_folder(self):
        payload = self.upload("../../../../etc/evil.csv", INVOICES / "INV-4482.csv")
        self.assertEqual(payload["filename"], "evil.csv")
        self.assertTrue((self.folder / "evil.csv").exists())
        self.assertEqual(
            sorted(p.name for p in self.folder.iterdir()),
            ["INV-4471.csv", "evil.csv"],
        )

    def test_an_unsupported_type_is_refused(self):
        with self.assertRaises(UploadRejected) as ctx:
            self.ws.ingest("notes.txt", b"hello")
        self.assertIn("not an invoice format", str(ctx.exception))

    def test_an_empty_file_is_refused(self):
        with self.assertRaises(UploadRejected):
            self.ws.ingest("empty.csv", b"")

    def test_an_oversized_file_is_refused(self):
        with self.assertRaises(UploadRejected) as ctx:
            self.ws.ingest("big.pdf", b"x" * (MAX_UPLOAD_BYTES + 1))
        self.assertIn("limit is", str(ctx.exception))

    def test_an_unreadable_file_leaves_nothing_behind(self):
        """A rejected upload must not litter the folder with a half-invoice."""
        with self.assertRaises(UploadRejected):
            self.ws.ingest("garbage.csv", b"this is not an invoice at all")
        self.assertFalse((self.folder / "garbage.csv").exists())

    def test_an_upload_joins_the_queue(self):
        before = len(self.ws.flag_store)
        self.upload("seeded.csv", INVOICES / "INV-4482.csv")
        self.assertGreater(len(self.ws.flag_store), before)

    def test_filenames_are_sanitised_not_escaped(self):
        from invoice_audit.workspace import safe_filename

        self.assertEqual(safe_filename("INV 4471.csv"), "INV_4471.csv")
        self.assertEqual(safe_filename("a/b/c.pdf"), "c.pdf")
        with self.assertRaises(UploadRejected):
            safe_filename("")
