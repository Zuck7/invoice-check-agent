"""Stage 01-02: intake and extraction.

Two extractors sit behind one :class:`Extractor` protocol, chosen by file
suffix; the rest of the pipeline never learns which one produced the lines.

* ``.csv`` / ``.txt`` -> :class:`CsvExtractor`. Exact, free, deterministic.
  Use it for hand-keyed invoices and for any warehouse that sends structured
  data.
* ``.pdf`` -> :class:`PdfRouter`. A born-digital PDF is read from its text
  layer by ``pdftext.PdfTextExtractor`` -- exact, free, offline. A scan has no
  text layer, and anything the text parser cannot read with certainty falls
  through to the vision model rather than being guessed at.
* images -> ``vision.VisionExtractor``.

File format: ``# key: value`` metadata lines, then a normal CSV table.

    # invoice_no: INV-4471
    # warehouse_id: WH-NORTH
    # client_id: ACME
    # invoice_date: 2026-08-05
    # period_start: 2026-07-01
    # period_end: 2026-07-31
    # stated_total: 22136.00
    line_no,description,quantity,uom,unit_rate,amount,base_amount
    1,Receiving - per pallet,40,pallet,6.00,240.00,
"""

from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path
from typing import Protocol

from .models import Invoice, InvoiceLine
from .money import money, quantity

REQUIRED_META = (
    "invoice_no",
    "warehouse_id",
    "client_id",
    "invoice_date",
    "period_start",
    "period_end",
    "stated_total",
)


class Extractor(Protocol):
    """Turns a source document into an :class:`Invoice`."""

    def supports(self, path: Path) -> bool: ...

    def extract(self, path: Path) -> Invoice: ...


class IntakeError(ValueError):
    """The document could not be read at all — never a flag, always a failure."""


def _split(text: str) -> tuple[dict[str, str], str]:
    meta: dict[str, str] = {}
    body: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            payload = stripped.lstrip("#").strip()
            if ":" in payload:
                key, _, value = payload.partition(":")
                meta[key.strip()] = value.strip()
            continue
        if stripped or body:
            body.append(line)
    return meta, "\n".join(body)


class CsvExtractor:
    """The v1 extractor. Deterministic, no model involved."""

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in {".csv", ".txt"}

    def extract(self, path: Path) -> Invoice:
        path = Path(path)
        meta, body = _split(path.read_text(encoding="utf-8"))

        missing = [k for k in REQUIRED_META if k not in meta]
        if missing:
            raise IntakeError(f"{path.name}: missing metadata {', '.join(missing)}")

        lines: list[InvoiceLine] = []
        reader = csv.DictReader(io.StringIO(body))
        if reader.fieldnames is None:
            raise IntakeError(f"{path.name}: no line-item table found")

        for index, row in enumerate(reader, start=1):
            row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            description = row.get("description", "")
            if not description:
                continue
            try:
                lines.append(
                    InvoiceLine(
                        line_no=int(row.get("line_no") or index),
                        description=description,
                        quantity=quantity(row.get("quantity")),
                        amount=money(row.get("amount")),
                        uom=row.get("uom") or None,
                        unit_rate=(
                            money(row["unit_rate"]) if row.get("unit_rate") else None
                        ),
                        base_amount=(
                            money(row["base_amount"]) if row.get("base_amount") else None
                        ),
                        source_page=(
                            int(row["source_page"]) if row.get("source_page") else None
                        ),
                    )
                )
            except (ValueError, TypeError) as exc:
                raise IntakeError(
                    f"{path.name} line {index}: unreadable row ({exc})"
                ) from exc

        if not lines:
            raise IntakeError(f"{path.name}: no line items")

        try:
            return Invoice(
                invoice_no=meta["invoice_no"],
                warehouse_id=meta["warehouse_id"],
                client_id=meta["client_id"],
                invoice_date=date.fromisoformat(meta["invoice_date"]),
                period_start=date.fromisoformat(meta["period_start"]),
                period_end=date.fromisoformat(meta["period_end"]),
                lines=lines,
                stated_total=money(meta["stated_total"]),
                currency=meta.get("currency", "USD"),
                source_path=str(path),
            )
        except ValueError as exc:
            raise IntakeError(f"{path.name}: bad metadata ({exc})") from exc


class LazyVisionExtractor:
    """Defers constructing the API client until a document actually needs it.

    Without this, importing the package would require ``anthropic`` and API
    credentials even for a CSV-only run, and the test suite would need both.

    Claims images only. PDFs go to :class:`PdfRouter`, which owns the choice
    between the text layer and vision and calls this one directly when it
    decides the document needs a model.
    """

    _SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})


    def __init__(self, **kwargs: object) -> None:
        self._kwargs = kwargs
        self._inner: Extractor | None = None

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self._SUFFIXES

    def extract(self, path: Path) -> Invoice:
        if self._inner is None:
            from .vision import VisionExtractor

            self._inner = VisionExtractor(**self._kwargs)  # type: ignore[arg-type]
        return self._inner.extract(path)


class PdfRouter:
    """Text layer first, vision second.

    The order is a cost and determinism decision, not a quality one: when the
    characters are in the file, reading them beats transcribing a picture of
    them. Everything else -- scans, unfamiliar layouts, tables the parser
    cannot label -- goes to the model.

    ``last_route`` records which path a document actually took, so the report
    can say so and so the split is measurable rather than assumed.
    """

    def __init__(self, **kwargs: object) -> None:
        self._kwargs = kwargs
        self._text: object | None = None
        self._vision = LazyVisionExtractor(**kwargs)
        self.last_route: str | None = None
        self.routes: dict[str, int] = {"text": 0, "vision": 0}

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".pdf"

    def _text_extractor(self):
        if self._text is None:
            from .pdftext import PdfTextExtractor

            self._text = PdfTextExtractor(**self._kwargs)  # type: ignore[arg-type]
        return self._text

    def extract(self, path: Path) -> Invoice:
        from . import pdftext

        reason = "pdfplumber is not installed"
        if pdftext.available():
            try:
                invoice = self._text_extractor().extract(path)
                self.last_route = "text"
                self.routes["text"] += 1
                return invoice
            except pdftext.NotConfident as exc:
                reason = str(exc)

        self.last_route = "vision"
        self.routes["vision"] += 1
        try:
            return self._vision.extract(path)
        except IntakeError as exc:
            raise IntakeError(
                f"{Path(path).name}: text layer declined ({reason}); "
                f"vision also failed ({exc})"
            ) from exc


DEFAULT_EXTRACTORS: tuple[Extractor, ...] = (
    CsvExtractor(),
    PdfRouter(),
    LazyVisionExtractor(),
)


def read_invoice(
    path: Path, extractors: tuple[Extractor, ...] = DEFAULT_EXTRACTORS
) -> Invoice:
    path = Path(path)
    for extractor in extractors:
        if extractor.supports(path):
            return extractor.extract(path)
    raise IntakeError(f"{path.name}: no extractor handles {path.suffix!r}")
