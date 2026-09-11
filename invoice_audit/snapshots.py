"""WMS inventory snapshots — what was actually on hand, and for how long.

STORAGE_AGING_ERROR needs to know a pallet's received and shipped dates. The
period counts in ``orderdata`` cannot answer that: they say how many
pallet-months were stored, not which pallets were old enough to attract a
long-term surcharge, nor which had already left the building.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

#: A pallet attracts the long-term surcharge once it has been on hand this
#: long. Warehouses vary; override per rate card when that becomes a real
#: difference rather than a guess.
LONG_TERM_DAYS = 90


@dataclass(frozen=True)
class PalletRecord:
    pallet_id: str
    warehouse_id: str
    client_id: str
    received: date
    shipped: date | None = None

    def on_hand_during(self, start: date, end: date) -> bool:
        if self.received > end:
            return False
        return self.shipped is None or self.shipped >= start

    def shipped_before(self, when: date) -> bool:
        return self.shipped is not None and self.shipped < when

    def age_days_at(self, when: date) -> int:
        """Days on hand as of ``when``, stopping the clock when it shipped."""
        end = min(when, self.shipped) if self.shipped else when
        return max(0, (end - self.received).days)


@dataclass
class LongTermPosition:
    """What the snapshot supports for one period."""

    qualifying: list[PalletRecord]
    already_shipped: list[PalletRecord]

    @property
    def qualifying_count(self) -> int:
        return len(self.qualifying)


class SnapshotSource(Protocol):
    def position(
        self,
        warehouse_id: str,
        client_id: str,
        period_start: date,
        period_end: date,
        long_term_days: int = LONG_TERM_DAYS,
    ) -> LongTermPosition | None: ...


@dataclass
class CsvSnapshots:
    """Pallet-level inventory from a flat export.

    Columns: pallet_id, warehouse_id, client_id, received, shipped
    """

    _pallets: list[PalletRecord] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> "CsvSnapshots":
        rows: list[PalletRecord] = []
        with Path(path).open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                shipped = (row.get("shipped") or "").strip()
                rows.append(
                    PalletRecord(
                        pallet_id=row["pallet_id"].strip(),
                        warehouse_id=row["warehouse_id"].strip(),
                        client_id=row["client_id"].strip(),
                        received=date.fromisoformat(row["received"].strip()),
                        shipped=date.fromisoformat(shipped) if shipped else None,
                    )
                )
        return cls(rows)

    def position(
        self,
        warehouse_id: str,
        client_id: str,
        period_start: date,
        period_end: date,
        long_term_days: int = LONG_TERM_DAYS,
    ) -> LongTermPosition | None:
        pallets = [
            p
            for p in self._pallets
            if p.warehouse_id == warehouse_id and p.client_id == client_id
        ]
        if not pallets:
            return None

        cutoff = timedelta(days=long_term_days)
        qualifying: list[PalletRecord] = []
        already_shipped: list[PalletRecord] = []

        for pallet in pallets:
            old_enough = timedelta(days=pallet.age_days_at(period_end)) >= cutoff
            if not old_enough:
                continue
            if pallet.shipped_before(period_start):
                # Gone before the period even opened: it cannot be stored, let
                # alone stored long-term.
                already_shipped.append(pallet)
            elif pallet.on_hand_during(period_start, period_end):
                qualifying.append(pallet)

        return LongTermPosition(
            qualifying=qualifying, already_shipped=already_shipped
        )


@dataclass
class NoSnapshots:
    """No pallet-level feed. STORAGE_AGING_ERROR is skipped, and the audit
    reports that rather than passing the invoice as fully checked."""

    def position(
        self,
        warehouse_id: str,
        client_id: str,
        period_start: date,
        period_end: date,
        long_term_days: int = LONG_TERM_DAYS,
    ) -> LongTermPosition | None:
        return None
