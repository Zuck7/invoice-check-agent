"""Vision extraction, exercised against a fake client — no API calls, no key.

The point of these tests is the boundary between the model and the audit: what
the extractor accepts, what it refuses, and above all that it does not "help"
by correcting the invoice. A transcriber that silently fixes 22 x $28 = $644
would delete the MATH_ERROR finding that pays for the project.
"""

from __future__ import annotations

import json
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from invoice_audit import AuditEngine, CsvOrderData, HistoryStore, RateCardStore
from invoice_audit.flagstore import FlagStore
from invoice_audit.vision import (
    CONFIDENCE_THRESHOLD,
    ExtractionError,
    INVOICE_SCHEMA,
    SYSTEM,
    VisionExtractor,
)

ROOT = Path(__file__).resolve().parent.parent
CARDS = ROOT / "data" / "rate_cards"
WMS = ROOT / "data" / "wms" / "counts.csv"


GOOD = {
    "invoice_no": "INV-9001",
    "warehouse_id": "WH-NORTH",
    "client_id": "ACME",
    "invoice_date": "2026-09-04",
    "period_start": "2026-08-01",
    "period_end": "2026-08-31",
    "currency": "USD",
    "stated_total": "644.00",
    "uncertain_fields": [],
    "lines": [
        {
            "line_no": 1,
            "description": "B2B freight · per pallet out",
            "quantity": "22",
            "uom": "pallet",
            "unit_rate": "28.00",
            "amount": "644.00",
            "base_amount": None,
            "source_page": 2,
            "confidence": 0.97,
        }
    ],
}


class FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class FakeClient:
    """Records requests and replays canned responses, one per attempt."""

    def __init__(self, *payloads, stop_reason="end_turn", stop_details=None):
        self.payloads = list(payloads)
        self.requests: list[dict] = []
        self.stop_reason = stop_reason
        self.stop_details = stop_details
        outer = self

        class Messages:
            def stream(self, **kwargs):
                outer.requests.append(kwargs)
                payload = (
                    outer.payloads.pop(0)
                    if outer.payloads
                    else outer.requests and "{}"
                )
                text = payload if isinstance(payload, str) else json.dumps(payload)
                message = SimpleNamespace(
                    content=[SimpleNamespace(type="text", text=text)],
                    stop_reason=outer.stop_reason,
                    stop_details=outer.stop_details,
                    usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
                )
                return FakeStream(message)

        self.beta = SimpleNamespace(messages=Messages())


def write_pdf(tmp: str, name: str = "scan.pdf") -> Path:
    path = Path(tmp) / name
    path.write_bytes(b"%PDF-1.4\nnot a real pdf, the client is faked\n")
    return path


class PromptTests(unittest.TestCase):
    def test_system_prompt_forbids_calculation(self):
        """The single most important instruction in the file."""
        self.assertIn("NEVER CALCULATE", SYSTEM)
        self.assertIn("644.00", SYSTEM)  # the worked example of not correcting

    def test_system_prompt_demands_string_money(self):
        self.assertIn("STRING", SYSTEM)

    def test_schema_forbids_extra_fields(self):
        self.assertFalse(INVOICE_SCHEMA["additionalProperties"])
        self.assertFalse(INVOICE_SCHEMA["properties"]["lines"]["items"]["additionalProperties"])

    def test_money_fields_are_strings_in_the_schema(self):
        line = INVOICE_SCHEMA["properties"]["lines"]["items"]["properties"]
        self.assertEqual(line["amount"]["type"], "string")
        self.assertEqual(INVOICE_SCHEMA["properties"]["stated_total"]["type"], "string")


class ExtractionTests(unittest.TestCase):
    def test_reads_a_document(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(GOOD)
            invoice = VisionExtractor(client=client).extract(write_pdf(tmp))

        self.assertEqual(invoice.invoice_no, "INV-9001")
        self.assertEqual(invoice.invoice_date, date(2026, 9, 4))
        self.assertEqual(invoice.stated_total, Decimal("644.00"))
        self.assertEqual(invoice.lines[0].unit_rate, Decimal("28.00"))
        self.assertEqual(invoice.lines[0].source_page, 2)

    def test_a_wrong_extension_survives_transcription(self):
        """22 x 28.00 is 616.00. The extractor must keep the printed 644.00."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            invoice = VisionExtractor(client=FakeClient(GOOD)).extract(write_pdf(tmp))

        line = invoice.lines[0]
        self.assertEqual(line.quantity * line.unit_rate, Decimal("616.00"))
        self.assertEqual(line.amount, Decimal("644.00"))

    def test_the_audit_then_catches_it(self):
        """End to end: a scanned invoice with a bad extension raises MATH_ERROR."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            engine = AuditEngine(
                store=RateCardStore.from_dir(CARDS),
                order_data=CsvOrderData.from_file(WMS),
                history=HistoryStore(),
                flag_store=FlagStore(),
                extractors=(VisionExtractor(client=FakeClient(GOOD)),),
            )
            result = engine.audit_path(write_pdf(tmp))

        math = [f for f in result.flags if f.flag_id == "MATH_ERROR"]
        self.assertTrue(math)
        self.assertEqual(math[0].delta, Decimal("28.00"))

    def test_money_never_becomes_a_float(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            invoice = VisionExtractor(client=FakeClient(GOOD)).extract(write_pdf(tmp))
        for line in invoice.lines:
            self.assertIsInstance(line.amount, Decimal)
            self.assertIsInstance(line.unit_rate, Decimal)

    def test_request_shape(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(GOOD)
            VisionExtractor(client=client).extract(write_pdf(tmp))

        sent = client.requests[0]
        self.assertEqual(sent["model"], "claude-opus-5")
        self.assertEqual(sent["thinking"], {"type": "adaptive"})
        self.assertEqual(sent["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", sent["betas"])
        self.assertEqual(
            sent["output_config"]["format"]["schema"], INVOICE_SCHEMA
        )
        block = sent["messages"][0]["content"][0]
        self.assertEqual(block["type"], "document")
        self.assertEqual(block["source"]["media_type"], "application/pdf")

    def test_images_are_sent_as_image_blocks(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "photo.jpg"
            path.write_bytes(b"\xff\xd8\xff not really a jpeg")
            client = FakeClient(GOOD)
            VisionExtractor(client=client).extract(path)

        block = client.requests[0]["messages"][0]["content"][0]
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["source"]["media_type"], "image/jpeg")


class RetryTests(unittest.TestCase):
    def test_a_bad_first_pass_is_retried(self):
        import tempfile

        broken = dict(GOOD, stated_total="not a number")
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(broken, GOOD)
            invoice = VisionExtractor(client=client).extract(write_pdf(tmp))

        self.assertEqual(len(client.requests), 2)
        self.assertEqual(invoice.stated_total, Decimal("644.00"))

    def test_the_retry_says_what_went_wrong(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(dict(GOOD, stated_total="???"), GOOD)
            VisionExtractor(client=client).extract(write_pdf(tmp))

        second = client.requests[1]["messages"][0]["content"][1]["text"]
        self.assertIn("stated_total", second)
        self.assertIn("do not compute", second)

    def test_the_budget_is_bounded(self):
        import tempfile

        broken = dict(GOOD, stated_total="???")
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(broken, broken, broken, broken, broken)
            with self.assertRaises(ExtractionError):
                VisionExtractor(client=client, max_retries=2).extract(write_pdf(tmp))

        self.assertEqual(len(client.requests), 3)


class RefusalTests(unittest.TestCase):
    def test_a_refusal_is_a_read_failure_not_a_flag(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(
                GOOD,
                stop_reason="refusal",
                stop_details=SimpleNamespace(type="refusal", category="cyber"),
            )
            with self.assertRaises(ExtractionError) as ctx:
                VisionExtractor(client=client, max_retries=0).extract(write_pdf(tmp))
        self.assertIn("declined", str(ctx.exception))
        self.assertIn("human", str(ctx.exception))


class ValidationTests(unittest.TestCase):
    def _fails_with(self, payload, fragment):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(payload, payload, payload)
            with self.assertRaises(ExtractionError) as ctx:
                VisionExtractor(client=client).extract(write_pdf(tmp))
        self.assertIn(fragment, str(ctx.exception))

    def test_no_lines(self):
        self._fails_with(dict(GOOD, lines=[]), "no line items")

    def test_blank_description(self):
        bad = json.loads(json.dumps(GOOD))
        bad["lines"][0]["description"] = "   "
        self._fails_with(bad, "no description")

    def test_missing_invoice_number(self):
        self._fails_with(dict(GOOD, invoice_no=""), "no invoice number")

    def test_backwards_period(self):
        self._fails_with(
            dict(GOOD, period_start="2026-08-31", period_end="2026-08-01"),
            "ends before it starts",
        )

    def test_unparseable_json(self):
        self._fails_with("{not json", "not valid JSON")

    def test_unknown_warehouse_without_an_override(self):
        self._fails_with(
            dict(GOOD, warehouse_id=None),
            "could not determine the warehouse",
        )

    def test_routing_overrides_what_the_model_read(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(dict(GOOD, warehouse_id="WH-TYPO", client_id="ACNE"))
            invoice = VisionExtractor(
                client=client, warehouse_id="WH-NORTH", client_id="ACME"
            ).extract(write_pdf(tmp))
        self.assertEqual(invoice.warehouse_id, "WH-NORTH")
        self.assertEqual(invoice.client_id, "ACME")


class ConfidenceTests(unittest.TestCase):
    def test_a_smudged_line_is_flagged(self):
        import tempfile

        smudged = json.loads(json.dumps(GOOD))
        smudged["lines"][0]["confidence"] = 0.42

        with tempfile.TemporaryDirectory() as tmp:
            engine = AuditEngine(
                store=RateCardStore.from_dir(CARDS),
                order_data=CsvOrderData.from_file(WMS),
                history=HistoryStore(),
                flag_store=FlagStore(),
                extractors=(VisionExtractor(client=FakeClient(smudged)),),
            )
            result = engine.audit_path(write_pdf(tmp))

        low = [f for f in result.flags if f.flag_id == "LOW_CONFIDENCE_EXTRACTION"]
        self.assertEqual(len(low), 1)
        self.assertEqual(low[0].evidence["stage"], "extract")
        self.assertEqual(low[0].source_page, 2)

    def test_a_clean_line_is_not_flagged(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            engine = AuditEngine(
                store=RateCardStore.from_dir(CARDS),
                order_data=CsvOrderData.from_file(WMS),
                history=HistoryStore(),
                flag_store=FlagStore(),
                extractors=(VisionExtractor(client=FakeClient(GOOD)),),
            )
            result = engine.audit_path(write_pdf(tmp))

        self.assertNotIn(
            "LOW_CONFIDENCE_EXTRACTION", [f.flag_id for f in result.flags]
        )

    def test_csv_lines_carry_no_extraction_confidence(self):
        """Hand-keyed input is exact; it must never trip the confidence check."""
        from invoice_audit import read_invoice

        invoice = read_invoice(ROOT / "data" / "invoices" / "INV-4471.csv")
        self.assertTrue(all(l.extract_confidence is None for l in invoice.lines))


if __name__ == "__main__":
    unittest.main(verbosity=2)
