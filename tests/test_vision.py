"""Vision extraction, against fake backends — no API calls, no keys.

What matters at this boundary is what the extractor accepts, what it refuses,
and above all that it does not "help" by correcting the invoice. A transcriber
that silently fixes 22 x $28 = $644 deletes the MATH_ERROR that pays for the
project — and that has to hold whichever provider is behind it.
"""

from __future__ import annotations

import json
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from invoice_audit import AuditEngine, CsvOrderData, HistoryStore, RateCardStore
from invoice_audit.backends import (
    AnthropicBackend,
    BackendError,
    GeminiBackend,
    make_backend,
    to_gemini_schema,
)
from invoice_audit.flagstore import FlagStore
from invoice_audit.vision import (
    INVOICE_SCHEMA,
    SYSTEM,
    ExtractionError,
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


class FakeBackend:
    """Replays canned responses and records exactly what it was asked."""

    def __init__(self, *payloads, error=None):
        self.name = "fake"
        self.payloads = list(payloads)
        self.calls = []
        self.error = error

    def transcribe(self, data, media_type, instruction, system, schema):
        self.calls.append(
            {
                "bytes": len(data),
                "media_type": media_type,
                "instruction": instruction,
                "system": system,
                "schema": schema,
            }
        )
        if self.error:
            raise self.error
        payload = self.payloads.pop(0) if self.payloads else "{}"
        return payload if isinstance(payload, str) else json.dumps(payload)


def write_pdf(tmp: str, name: str = "scan.pdf") -> Path:
    path = Path(tmp) / name
    path.write_bytes(b"%PDF-1.4\nnot a real pdf, the backend is faked\n")
    return path


def engine_with(backend) -> AuditEngine:
    return AuditEngine(
        store=RateCardStore.from_dir(CARDS),
        order_data=CsvOrderData.from_file(WMS),
        history=HistoryStore(),
        flag_store=FlagStore(),
        extractors=(VisionExtractor(backend=backend),),
    )


class PromptTests(unittest.TestCase):
    def test_system_prompt_forbids_calculation(self):
        """The single most important instruction in the project."""
        self.assertIn("NEVER CALCULATE", SYSTEM)
        self.assertIn("644.00", SYSTEM)

    def test_system_prompt_demands_string_money(self):
        self.assertIn("STRING", SYSTEM)

    def test_money_fields_are_strings_in_the_schema(self):
        line = INVOICE_SCHEMA["properties"]["lines"]["items"]["properties"]
        self.assertEqual(line["amount"]["type"], "string")
        self.assertEqual(INVOICE_SCHEMA["properties"]["stated_total"]["type"], "string")

    def test_the_same_prompt_and_schema_reach_every_backend(self):
        """Swapping providers must not change what a valid invoice is."""
        import tempfile

        backend = FakeBackend(GOOD)
        with tempfile.TemporaryDirectory() as tmp:
            VisionExtractor(backend=backend).extract(write_pdf(tmp))
        self.assertEqual(backend.calls[0]["system"], SYSTEM)
        self.assertEqual(backend.calls[0]["schema"], INVOICE_SCHEMA)


class ExtractionTests(unittest.TestCase):
    def test_reads_a_document(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            invoice = VisionExtractor(backend=FakeBackend(GOOD)).extract(write_pdf(tmp))
        self.assertEqual(invoice.invoice_no, "INV-9001")
        self.assertEqual(invoice.invoice_date, date(2026, 9, 4))
        self.assertEqual(invoice.lines[0].unit_rate, Decimal("28.00"))
        self.assertEqual(invoice.lines[0].source_page, 2)

    def test_a_wrong_extension_survives_transcription(self):
        """22 x 28.00 is 616.00. The printed 644.00 must come through intact."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            invoice = VisionExtractor(backend=FakeBackend(GOOD)).extract(write_pdf(tmp))
        line = invoice.lines[0]
        self.assertEqual(line.quantity * line.unit_rate, Decimal("616.00"))
        self.assertEqual(line.amount, Decimal("644.00"))

    def test_the_audit_then_catches_it(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = engine_with(FakeBackend(GOOD)).audit_path(write_pdf(tmp))
        math = [f for f in result.flags if f.flag_id == "MATH_ERROR"]
        self.assertTrue(math)
        self.assertEqual(math[0].delta, Decimal("28.00"))

    def test_money_never_becomes_a_float(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            invoice = VisionExtractor(backend=FakeBackend(GOOD)).extract(write_pdf(tmp))
        for line in invoice.lines:
            self.assertIsInstance(line.amount, Decimal)
            self.assertIsInstance(line.unit_rate, Decimal)

    def test_the_media_type_reaches_the_backend(self):
        import tempfile

        backend = FakeBackend(GOOD, GOOD)
        with tempfile.TemporaryDirectory() as tmp:
            VisionExtractor(backend=backend).extract(write_pdf(tmp))
            jpg = Path(tmp) / "photo.jpg"
            jpg.write_bytes(b"\xff\xd8\xff not really a jpeg")
            VisionExtractor(backend=backend).extract(jpg)
        self.assertEqual(backend.calls[0]["media_type"], "application/pdf")
        self.assertEqual(backend.calls[1]["media_type"], "image/jpeg")


class RetryTests(unittest.TestCase):
    def test_a_bad_first_pass_is_retried(self):
        import tempfile

        broken = dict(GOOD, stated_total="not a number")
        backend = FakeBackend(broken, GOOD)
        with tempfile.TemporaryDirectory() as tmp:
            invoice = VisionExtractor(backend=backend).extract(write_pdf(tmp))
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(invoice.stated_total, Decimal("644.00"))

    def test_the_retry_says_what_went_wrong(self):
        import tempfile

        backend = FakeBackend(dict(GOOD, stated_total="???"), GOOD)
        with tempfile.TemporaryDirectory() as tmp:
            VisionExtractor(backend=backend).extract(write_pdf(tmp))
        second = backend.calls[1]["instruction"]
        self.assertIn("stated_total", second)
        self.assertIn("do not compute", second)

    def test_the_budget_is_bounded(self):
        import tempfile

        broken = dict(GOOD, stated_total="???")
        backend = FakeBackend(broken, broken, broken, broken, broken)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ExtractionError):
                VisionExtractor(backend=backend, max_retries=2).extract(write_pdf(tmp))
        self.assertEqual(len(backend.calls), 3)


class BackendFailureTests(unittest.TestCase):
    def test_a_refusal_is_a_read_failure_not_a_flag(self):
        import tempfile

        backend = FakeBackend(error=BackendError("Gemini declined to read this document (SAFETY). Route it to a human."))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ExtractionError) as ctx:
                VisionExtractor(backend=backend, max_retries=0).extract(write_pdf(tmp))
        self.assertIn("declined", str(ctx.exception))
        self.assertIn("human", str(ctx.exception))

    def test_a_transport_failure_surfaces_as_a_read_failure(self):
        import tempfile

        backend = FakeBackend(error=BackendError("Gemini request failed: timeout"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ExtractionError):
                VisionExtractor(backend=backend, max_retries=0).extract(write_pdf(tmp))


class ValidationTests(unittest.TestCase):
    def _fails_with(self, payload, fragment):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ExtractionError) as ctx:
                VisionExtractor(
                    backend=FakeBackend(payload, payload, payload)
                ).extract(write_pdf(tmp))
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
        self._fails_with(dict(GOOD, warehouse_id=None), "could not determine the warehouse")

    def test_routing_overrides_what_the_model_read(self):
        import tempfile

        backend = FakeBackend(dict(GOOD, warehouse_id="WH-TYPO", client_id="ACNE"))
        with tempfile.TemporaryDirectory() as tmp:
            invoice = VisionExtractor(
                backend=backend, warehouse_id="WH-NORTH", client_id="ACME"
            ).extract(write_pdf(tmp))
        self.assertEqual(invoice.warehouse_id, "WH-NORTH")
        self.assertEqual(invoice.client_id, "ACME")


class ConfidenceTests(unittest.TestCase):
    def test_a_smudged_line_is_flagged(self):
        import tempfile

        smudged = json.loads(json.dumps(GOOD))
        smudged["lines"][0]["confidence"] = 0.42
        with tempfile.TemporaryDirectory() as tmp:
            result = engine_with(FakeBackend(smudged)).audit_path(write_pdf(tmp))
        low = [f for f in result.flags if f.flag_id == "LOW_CONFIDENCE_EXTRACTION"]
        self.assertEqual(len(low), 1)
        self.assertEqual(low[0].evidence["stage"], "extract")

    def test_a_clean_line_is_not_flagged(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = engine_with(FakeBackend(GOOD)).audit_path(write_pdf(tmp))
        self.assertNotIn("LOW_CONFIDENCE_EXTRACTION", [f.flag_id for f in result.flags])

    def test_csv_lines_carry_no_extraction_confidence(self):
        from invoice_audit import read_invoice

        invoice = read_invoice(ROOT / "data" / "invoices" / "INV-4471.csv")
        self.assertTrue(all(l.extract_confidence is None for l in invoice.lines))


class GeminiSchemaTests(unittest.TestCase):
    """Gemini's schema dialect differs where it matters most: nullable fields."""

    def setUp(self):
        self.schema = to_gemini_schema(INVOICE_SCHEMA)

    def test_union_types_become_nullable(self):
        field = self.schema["properties"]["warehouse_id"]
        self.assertEqual(field["type"], "string")
        self.assertTrue(field["nullable"])

    def test_nested_line_fields_are_translated_too(self):
        uom = self.schema["properties"]["lines"]["items"]["properties"]["uom"]
        self.assertEqual(uom["type"], "string")
        self.assertTrue(uom["nullable"])

    def test_additional_properties_is_dropped(self):
        self.assertNotIn("additionalProperties", self.schema)
        self.assertNotIn(
            "additionalProperties", self.schema["properties"]["lines"]["items"]
        )

    def test_non_union_types_are_left_alone(self):
        self.assertEqual(self.schema["properties"]["invoice_no"]["type"], "string")
        self.assertNotIn("nullable", self.schema["properties"]["invoice_no"])

    def test_required_survives_translation(self):
        self.assertEqual(
            set(self.schema["required"]), set(INVOICE_SCHEMA["required"])
        )

    def test_the_original_schema_is_not_mutated(self):
        """Anthropic must still get the untranslated schema."""
        self.assertEqual(
            INVOICE_SCHEMA["properties"]["warehouse_id"]["type"], ["string", "null"]
        )


class BackendSelectionTests(unittest.TestCase):
    def test_gemini_is_the_default(self):
        self.assertIsInstance(make_backend(), GeminiBackend)

    def test_providers_by_name(self):
        self.assertIsInstance(make_backend("gemini"), GeminiBackend)
        self.assertIsInstance(make_backend("google"), GeminiBackend)
        self.assertIsInstance(make_backend("anthropic"), AnthropicBackend)
        self.assertIsInstance(make_backend("claude"), AnthropicBackend)

    def test_an_unknown_provider_is_refused(self):
        with self.assertRaises(BackendError):
            make_backend("llama")

    def test_a_missing_google_key_says_what_to_set(self):
        import os

        saved = {k: os.environ.pop(k, None) for k in ("GOOGLE_API_KEY", "GEMINI_API_KEY")}
        try:
            with self.assertRaises(BackendError) as ctx:
                GeminiBackend().transcribe(b"x", "application/pdf", "i", "s", {})
            self.assertIn("GOOGLE_API_KEY", str(ctx.exception))
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_an_explicit_key_is_used(self):
        backend = GeminiBackend(api_key="test-key")
        self.assertEqual(backend.api_key, "test-key")

    def test_the_model_is_overridable(self):
        self.assertEqual(GeminiBackend(model="gemini-2.5-flash").model, "gemini-2.5-flash")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class DotenvTests(unittest.TestCase):
    def setUp(self):
        from invoice_audit import env

        self.env = env

    def test_parses_the_shapes_a_real_file_has(self):
        parsed = self.env.parse(
            "\n".join(
                [
                    "# a comment",
                    "",
                    "GOOGLE_API_KEY=abc123",
                    'QUOTED="with spaces"',
                    "SINGLE='single'",
                    "export EXPORTED=yes",
                    "  SPACED = trimmed  ",
                    "NOEQUALS",
                ]
            )
        )
        self.assertEqual(parsed["GOOGLE_API_KEY"], "abc123")
        self.assertEqual(parsed["QUOTED"], "with spaces")
        self.assertEqual(parsed["SINGLE"], "single")
        self.assertEqual(parsed["EXPORTED"], "yes")
        self.assertEqual(parsed["SPACED"], "trimmed")
        self.assertNotIn("NOEQUALS", parsed)

    def test_a_real_environment_variable_wins(self):
        """A deploy's secret must not be replaced by a stray file."""
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("IA_TEST_KEY=from-file\n")
            os.environ["IA_TEST_KEY"] = "from-shell"
            try:
                applied = self.env.load(path)
                self.assertEqual(os.environ["IA_TEST_KEY"], "from-shell")
                self.assertNotIn("IA_TEST_KEY", applied)
            finally:
                os.environ.pop("IA_TEST_KEY", None)

    def test_it_fills_in_what_the_shell_lacks(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("IA_TEST_UNSET=from-file\n")
            os.environ.pop("IA_TEST_UNSET", None)
            try:
                applied = self.env.load(path)
                self.assertEqual(os.environ["IA_TEST_UNSET"], "from-file")
                self.assertIn("IA_TEST_UNSET", applied)
            finally:
                os.environ.pop("IA_TEST_UNSET", None)

    def test_a_missing_file_is_not_an_error(self):
        self.assertEqual(self.env.load(Path("/nowhere/.env")), {})

    def test_it_searches_parent_directories(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            # .resolve() on both sides: macOS symlinks /var to /private/var.
            root = Path(tmp).resolve()
            (root / ".env").write_text("X=1\n")
            deep = root / "a" / "b"
            deep.mkdir(parents=True)
            self.assertEqual(self.env.find(deep), root / ".env")

    def test_the_example_file_carries_no_secret(self):
        example = ROOT / ".env.example"
        if not example.exists():
            self.skipTest("no committed .env template")
        for key, value in self.env.parse(example.read_text()).items():
            with self.subTest(key=key):
                if key.endswith("_API_KEY"):
                    self.assertEqual(value, "", f"{key} has a value committed")

    def test_dotenv_is_gitignored(self):
        ignore = (ROOT / ".gitignore").read_text()
        self.assertIn(".env", ignore)
        self.assertIn("!.env.example", ignore)
