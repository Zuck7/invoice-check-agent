"""Stage 02: extract a warehouse invoice from a PDF or image with Claude.

Why a vision model rather than a text-layer parser: warehouses send scans, and
a scan has no text layer to parse. Beyond that, every warehouse has its own
layout, and reconstructing table rows from character coordinates is per-template
work that never ends.

The reason it is *safe* here is the architecture, not the model. This module
only transcribes. It never computes, never looks up a rate, never decides
anything. Every number it reads is checked by deterministic code against the
buy card and the WMS, so a misread becomes a flag rather than a silent error:

    misread rate      -> RATE_DRIFT
    misread quantity  -> QTY_VARIANCE
    misread extension -> MATH_ERROR
    misread total     -> MATH_ERROR (invoice total)

The checks that catch a warehouse's mistakes catch the extractor's too.

The provider is pluggable — see ``backends.py``. Everything that decides
whether an extraction is *valid* lives here and is shared, so switching between
Gemini and Claude cannot change what counts as a well-formed invoice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .backends import BackendError, VisionBackend, make_backend
from .intake import IntakeError
from .models import Invoice, InvoiceLine
from .money import money, quantity

#: Extraction retries. One focused re-read after a bad first pass, then give up
#: and let a human look at it. Same ceiling as the mapping ladder.
MAX_EXTRACT_RETRIES = 2

#: Per-line transcription confidence below this earns LOW_CONFIDENCE_EXTRACTION.
CONFIDENCE_THRESHOLD = Decimal("0.80")

_MEDIA = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

SYSTEM = """\
You transcribe third-party logistics invoices for an audit system.

Transcribe only. You are not checking the invoice and you are not helping.

Rules, in order of importance:

1. NEVER CALCULATE ANYTHING. Copy every number exactly as printed, including
   numbers that are wrong. If a line reads 22 x $28.00 = $644.00, record
   amount "644.00" -- do not correct it to 616.00. If the printed total does
   not match the lines, record the printed total. Downstream code exists
   specifically to find those errors, and a helpful correction here destroys
   the finding.
2. Never invent a value. If a field is not printed, use null. An absent unit
   rate is null, not the amount divided by the quantity.
3. Emit every monetary value and quantity as a STRING, exactly as printed, with
   no thousands separators and a period as the decimal point: "9500.00",
   "3200", "0.42". Never as a JSON number.
4. Record which page each line came from, 1-indexed.
5. Give each line a confidence between 0 and 1 for how legible it was. Be
   honest: a smudged or handwritten figure is low confidence, and saying so
   sends it to a human instead of into an invoice.
6. List any field you are unsure about in uncertain_fields, as
   "line 3 unit_rate" or "stated_total".

Dates are ISO 8601 (YYYY-MM-DD). If the invoice gives a service period as a
month name, convert it to first and last day of that month.
"""

LINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "line_no": {"type": "integer"},
        "description": {"type": "string"},
        "quantity": {"type": ["string", "null"]},
        "uom": {"type": ["string", "null"]},
        "unit_rate": {"type": ["string", "null"]},
        "amount": {"type": "string"},
        "base_amount": {"type": ["string", "null"]},
        "source_page": {"type": ["integer", "null"]},
        "confidence": {"type": "number"},
    },
    "required": [
        "line_no",
        "description",
        "quantity",
        "uom",
        "unit_rate",
        "amount",
        "base_amount",
        "source_page",
        "confidence",
    ],
    "additionalProperties": False,
}

INVOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "invoice_no": {"type": "string"},
        "warehouse_id": {"type": ["string", "null"]},
        "client_id": {"type": ["string", "null"]},
        "invoice_date": {"type": "string"},
        "period_start": {"type": "string"},
        "period_end": {"type": "string"},
        "currency": {"type": "string"},
        "stated_total": {"type": "string"},
        "lines": {"type": "array", "items": LINE_SCHEMA},
        "uncertain_fields": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "invoice_no",
        "warehouse_id",
        "client_id",
        "invoice_date",
        "period_start",
        "period_end",
        "currency",
        "stated_total",
        "lines",
        "uncertain_fields",
    ],
    "additionalProperties": False,
}


class ExtractionError(IntakeError):
    """The document could not be transcribed. Never a flag, always a failure."""


def _decimal(value: object, what: str) -> Decimal:
    try:
        return money(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ExtractionError(f"{what}: not a monetary value ({value!r})") from exc


def _date(value: object, what: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError) as exc:
        raise ExtractionError(f"{what}: not an ISO date ({value!r})") from exc


@dataclass
class VisionExtractor:
    """Transcribes PDFs and images via the Claude API.

    ``provider`` picks the backend ("gemini" or "anthropic"); leaving it unset
    uses ``INVOICE_AUDIT_VISION`` and otherwise Gemini.

    ``warehouse_id`` and ``client_id`` override whatever the model reads off the
    page. Intake usually knows them from routing (which mailbox the invoice
    arrived in), and an internal id is not something a warehouse prints. When
    they are left unset and the model's reading matches no rate card, the audit
    surfaces that as RATE_CARD_VERSION_STALE rather than guessing.
    """

    backend: VisionBackend | None = None
    provider: str | None = None
    api_key: str | None = None
    model: str | None = None
    warehouse_id: str | None = None
    client_id: str | None = None
    max_retries: int = MAX_EXTRACT_RETRIES
    confidence_threshold: Decimal = CONFIDENCE_THRESHOLD
    _usage: list[Any] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.backend is None:
            kwargs: dict[str, Any] = {}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            if self.model:
                kwargs["model"] = self.model
            self.backend = make_backend(self.provider, **kwargs)

    # -- Extractor protocol ------------------------------------------------

    def supports(self, path: Path) -> bool:
        return Path(path).suffix.lower() in _MEDIA

    def extract(self, path: Path) -> Invoice:
        path = Path(path)
        media_type = _MEDIA.get(path.suffix.lower())
        if media_type is None:
            raise ExtractionError(f"{path.name}: unsupported format")

        data = path.read_bytes()
        instruction = "Transcribe this warehouse invoice."
        last_error: str | None = None

        for attempt in range(self.max_retries + 1):
            if attempt:
                instruction = (
                    "Transcribe this warehouse invoice again, more carefully. "
                    f"The previous attempt failed: {last_error}. "
                    "Re-read the affected values character by character. "
                    "Remember: copy what is printed, do not compute or correct."
                )
            raw = self._ask(data, media_type, instruction)
            try:
                return self._build(raw, path)
            except ExtractionError as exc:
                last_error = str(exc)

        raise ExtractionError(
            f"{path.name}: could not be transcribed after "
            f"{self.max_retries + 1} attempts. Last problem: {last_error}"
        )

    # -- API ---------------------------------------------------------------

    def _ask(self, data: bytes, media_type: str, instruction: str) -> dict[str, Any]:
        assert self.backend is not None
        try:
            text = self.backend.transcribe(
                data=data,
                media_type=media_type,
                instruction=instruction,
                system=SYSTEM,
                schema=INVOICE_SCHEMA,
            )
        except BackendError as exc:
            raise ExtractionError(str(exc)) from exc

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"response was not valid JSON: {exc}") from exc

    # -- Mapping into our model -------------------------------------------

    def _build(self, raw: dict[str, Any], path: Path) -> Invoice:
        rows = raw.get("lines") or []
        if not rows:
            raise ExtractionError(f"{path.name}: no line items found")

        lines: list[InvoiceLine] = []
        for index, row in enumerate(rows, start=1):
            where = f"line {row.get('line_no', index)}"
            confidence = Decimal(str(row.get("confidence", 0)))
            lines.append(
                InvoiceLine(
                    line_no=int(row.get("line_no") or index),
                    description=str(row.get("description") or "").strip(),
                    quantity=quantity(row.get("quantity")),
                    amount=_decimal(row.get("amount"), f"{where} amount"),
                    uom=row.get("uom") or None,
                    unit_rate=(
                        _decimal(row["unit_rate"], f"{where} unit_rate")
                        if row.get("unit_rate")
                        else None
                    ),
                    base_amount=(
                        _decimal(row["base_amount"], f"{where} base_amount")
                        if row.get("base_amount")
                        else None
                    ),
                    source_page=row.get("source_page"),
                    extract_confidence=confidence,
                )
            )

        blank = [l.line_no for l in lines if not l.description]
        if blank:
            raise ExtractionError(
                f"{path.name}: lines {blank} came back with no description"
            )

        warehouse_id = self.warehouse_id or raw.get("warehouse_id")
        client_id = self.client_id or raw.get("client_id")
        if not warehouse_id or not client_id:
            raise ExtractionError(
                f"{path.name}: could not determine the warehouse or client. "
                "Pass them explicitly from intake routing."
            )

        invoice = Invoice(
            invoice_no=str(raw.get("invoice_no") or "").strip(),
            warehouse_id=str(warehouse_id).strip(),
            client_id=str(client_id).strip(),
            invoice_date=_date(raw.get("invoice_date"), "invoice_date"),
            period_start=_date(raw.get("period_start"), "period_start"),
            period_end=_date(raw.get("period_end"), "period_end"),
            lines=lines,
            stated_total=_decimal(raw.get("stated_total"), "stated_total"),
            currency=str(raw.get("currency") or "USD").strip().upper(),
            source_path=str(path),
        )
        if not invoice.invoice_no:
            raise ExtractionError(f"{path.name}: no invoice number found")
        if invoice.period_end < invoice.period_start:
            raise ExtractionError(
                f"{path.name}: service period ends before it starts "
                f"({invoice.period_start} to {invoice.period_end})"
            )
        return invoice
