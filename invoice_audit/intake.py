"""Stage 01-02: intake and extraction.

v1 reads the CSV/hand-keyed format below. PDF extraction is phase 2 and plugs
in behind :class:`Extractor` — the rest of the pipeline never learns which one
produced the lines.

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


class PdfExtractor:
    """Phase 2 placeholder.

    Kept so the wiring is visible: the engine already accepts a list of
    extractors and picks by suffix. Implementing this is the phase 2 task, and
    it is scored against the phase 1 engine on the labelled set.
    """

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".pdf"

    def extract(self, path: Path) -> Invoice:
        raise IntakeError(
            f"{Path(path).name}: PDF extraction is phase 2. "
            "Hand-key the invoice to the CSV format for now."
        )


DEFAULT_EXTRACTORS: tuple[Extractor, ...] = (CsvExtractor(), PdfExtractor())


def read_invoice(
    path: Path, extractors: tuple[Extractor, ...] = DEFAULT_EXTRACTORS
) -> Invoice:
    path = Path(path)
    for extractor in extractors:
        if extractor.supports(path):
            return extractor.extract(path)
    raise IntakeError(f"{path.name}: no extractor handles {path.suffix!r}")
