"""The dispute and credit log — what the warehouse already agreed to give back.

MISSING_CREDIT exists because an agreed credit that never lands is invisible:
nothing on the invoice is wrong, the money simply is not there. Only a record
of what was promised makes the absence detectable.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from .money import money


@dataclass(frozen=True)
class AgreedCredit:
    """A credit the warehouse accepted and has not yet applied."""

    credit_ref: str
    warehouse_id: str
    client_id: str
    period_start: date
    period_end: date
    amount: Decimal
    rate_key: str | None = None
    agreed_date: date | None = None
    note: str = ""


class CreditLog(Protocol):
    def outstanding(
        self,
        warehouse_id: str,
        client_id: str,
        period_start: date,
        period_end: date,
    ) -> list[AgreedCredit]: ...


@dataclass
class CsvCreditLog:
    """Credits from a flat export.

    Columns: credit_ref, warehouse_id, client_id, period_start, period_end,
    amount, rate_key, agreed_date, status, note

    Only rows with status ``agreed`` are outstanding. ``applied`` rows are
    already settled and ``rejected`` ones were never owed, so neither should
    produce a flag.
    """

    _credits: list[AgreedCredit] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> "CsvCreditLog":
        rows: list[AgreedCredit] = []
        with Path(path).open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if (row.get("status") or "agreed").strip().lower() != "agreed":
                    continue
                rows.append(
                    AgreedCredit(
                        credit_ref=row["credit_ref"].strip(),
                        warehouse_id=row["warehouse_id"].strip(),
                        client_id=row["client_id"].strip(),
                        period_start=date.fromisoformat(row["period_start"].strip()),
                        period_end=date.fromisoformat(row["period_end"].strip()),
                        amount=money(row["amount"].strip()),
                        rate_key=(row.get("rate_key") or "").strip() or None,
                        agreed_date=(
                            date.fromisoformat(row["agreed_date"].strip())
                            if row.get("agreed_date")
                            else None
                        ),
                        note=(row.get("note") or "").strip(),
                    )
                )
        return cls(rows)

    def outstanding(
        self,
        warehouse_id: str,
        client_id: str,
        period_start: date,
        period_end: date,
    ) -> list[AgreedCredit]:
        return [
            c
            for c in self._credits
            if c.warehouse_id == warehouse_id
            and c.client_id == client_id
            and c.period_start == period_start
            and c.period_end == period_end
        ]


@dataclass
class NoCreditLog:
    """No dispute log wired up. MISSING_CREDIT is skipped and the audit says so."""

    def outstanding(
        self,
        warehouse_id: str,
        client_id: str,
        period_start: date,
        period_end: date,
    ) -> list[AgreedCredit]:
        return []

    @property
    def configured(self) -> bool:
        return False
