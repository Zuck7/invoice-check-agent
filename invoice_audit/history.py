"""Invoice history — what we have already seen.

DUPLICATE_INVOICE compares content hashes; DUPLICATE_CHARGE compares the
priced lines within a service period. MISSING_CREDIT still needs the dispute
log, which is a data source we do not have yet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SeenLine:
    """Enough of a billed line to recognise it billed again.

    Only priced lines are kept: an unmapped line has no rate key to compare,
    and matching on free text would produce duplicates out of rewording.
    """

    rate_key: str
    quantity: str
    amount: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rate_key": self.rate_key,
            "quantity": self.quantity,
            "amount": self.amount,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SeenLine":
        return cls(
            rate_key=raw["rate_key"],
            quantity=raw["quantity"],
            amount=raw["amount"],
        )


@dataclass(frozen=True)
class SeenInvoice:
    invoice_no: str
    warehouse_id: str
    client_id: str
    content_hash: str
    invoice_date: date
    source_path: str | None
    period_start: date | None = None
    period_end: date | None = None
    lines: tuple[SeenLine, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "invoice_no": self.invoice_no,
            "warehouse_id": self.warehouse_id,
            "client_id": self.client_id,
            "content_hash": self.content_hash,
            "invoice_date": self.invoice_date.isoformat(),
            "source_path": self.source_path,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "lines": [line.to_dict() for line in self.lines],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SeenInvoice":
        def maybe(value: str | None) -> date | None:
            return date.fromisoformat(value) if value else None

        return cls(
            invoice_no=raw["invoice_no"],
            warehouse_id=raw["warehouse_id"],
            client_id=raw["client_id"],
            content_hash=raw["content_hash"],
            invoice_date=date.fromisoformat(raw["invoice_date"]),
            source_path=raw.get("source_path"),
            period_start=maybe(raw.get("period_start")),
            period_end=maybe(raw.get("period_end")),
            lines=tuple(SeenLine.from_dict(r) for r in raw.get("lines", [])),
        )


class HistoryStore:
    """JSON-backed, or purely in memory when no path is given."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._seen: list[SeenInvoice] = []
        if self.path and self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._seen = [SeenInvoice.from_dict(r) for r in raw.get("invoices", [])]

    def __len__(self) -> int:
        return len(self._seen)

    def lookup(self, invoice_no: str, warehouse_id: str) -> list[SeenInvoice]:
        return [
            s
            for s in self._seen
            if s.invoice_no == invoice_no and s.warehouse_id == warehouse_id
        ]

    def lookup_hash(self, content_hash: str) -> list[SeenInvoice]:
        return [s for s in self._seen if s.content_hash == content_hash]

    def billed_in_period(
        self,
        warehouse_id: str,
        client_id: str,
        period_start: date,
        period_end: date,
        exclude_invoice_no: str = "",
    ) -> list[tuple[SeenInvoice, SeenLine]]:
        """Lines already billed for this exact service period.

        Keyed on the period rather than the invoice date, because billing
        storage for July on two different invoices is the duplicate we care
        about; billing storage every month is not.
        """
        out: list[tuple[SeenInvoice, SeenLine]] = []
        for seen in self._seen:
            if seen.warehouse_id != warehouse_id or seen.client_id != client_id:
                continue
            if seen.period_start != period_start or seen.period_end != period_end:
                continue
            if seen.invoice_no == exclude_invoice_no:
                continue
            out.extend((seen, line) for line in seen.lines)
        return out

    def record(self, seen: SeenInvoice) -> None:
        self._seen.append(seen)

    def flush(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"invoices": [s.to_dict() for s in self._seen]}
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
