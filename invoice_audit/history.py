"""Invoice history — what we have already seen.

v1 uses it for DUPLICATE_INVOICE only. DUPLICATE_CHARGE and MISSING_CREDIT
(phase 3) read from the same store once there are enough periods in it to be
worth querying.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SeenInvoice:
    invoice_no: str
    warehouse_id: str
    client_id: str
    content_hash: str
    invoice_date: date
    source_path: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "invoice_no": self.invoice_no,
            "warehouse_id": self.warehouse_id,
            "client_id": self.client_id,
            "content_hash": self.content_hash,
            "invoice_date": self.invoice_date.isoformat(),
            "source_path": self.source_path,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SeenInvoice":
        return cls(
            invoice_no=raw["invoice_no"],
            warehouse_id=raw["warehouse_id"],
            client_id=raw["client_id"],
            content_hash=raw["content_hash"],
            invoice_date=date.fromisoformat(raw["invoice_date"]),
            source_path=raw.get("source_path"),
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

    def record(self, seen: SeenInvoice) -> None:
        self._seen.append(seen)

    def flush(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"invoices": [s.to_dict() for s in self._seen]}
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
