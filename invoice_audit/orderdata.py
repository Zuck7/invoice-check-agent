"""WMS / OMS activity counts — the ground truth for QTY_VARIANCE.

The audit is only as good as this data. A missing period returns ``None``
rather than zero, so a gap in the warehouse feed reads as "cannot check" and
never as "you billed for activity that did not happen".
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from .money import quantity


@dataclass(frozen=True)
class PeriodKey:
    warehouse_id: str
    client_id: str
    period_start: date
    period_end: date


class OrderDataProvider(Protocol):
    """Seam for a real WMS/OMS integration."""

    def counts(self, key: PeriodKey) -> dict[str, Decimal] | None:
        """Activity counts by rate-card key, or None if the period is unknown."""
        ...


@dataclass
class CsvOrderData:
    """Counts loaded from a flat export.

    Columns: warehouse_id, client_id, period_start, period_end, rate_key, count
    """

    _rows: dict[PeriodKey, dict[str, Decimal]] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: Path) -> "CsvOrderData":
        store: dict[PeriodKey, dict[str, Decimal]] = {}
        with Path(path).open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                key = PeriodKey(
                    warehouse_id=row["warehouse_id"].strip(),
                    client_id=row["client_id"].strip(),
                    period_start=date.fromisoformat(row["period_start"].strip()),
                    period_end=date.fromisoformat(row["period_end"].strip()),
                )
                store.setdefault(key, {})[row["rate_key"].strip()] = quantity(
                    row["count"].strip()
                )
        return cls(store)

    def counts(self, key: PeriodKey) -> dict[str, Decimal] | None:
        return self._rows.get(key)


@dataclass
class NoOrderData:
    """Used when no WMS export is wired up yet.

    Quantity checks are skipped and the audit says so in its notes, rather than
    silently reporting a clean invoice it never actually verified.
    """

    def counts(self, key: PeriodKey) -> dict[str, Decimal] | None:
        return None
