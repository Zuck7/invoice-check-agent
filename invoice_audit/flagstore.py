"""The exception queue, persisted.

A :class:`Flag` is an observation the engine made. A :class:`FlagRecord` is the
piece of work a human owns: it has a status, an age, and a resolution. Keeping
them separate means re-auditing an invoice never resurrects a finding somebody
already dealt with.

Records are keyed by :attr:`Flag.fingerprint`, so the same finding seen on ten
re-runs is one queue entry with ten sightings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .flags import Severity, get as get_flag
from .models import Flag
from .money import ZERO, money

#: Escalate anything still open past this. Contractual dispute windows run
#: 30-90 days and carrier windows 30-60, so a flag has to surface with room to
#: act on it, not on the last day.
ESCALATION_DAYS = 21


class Status:
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FlagRecord:
    fingerprint: str
    flag_id: str
    severity: str
    invoice_no: str
    warehouse_id: str
    client_id: str
    period_month: str
    line_no: int | None
    line_description: str | None
    message: str
    expected: str | None
    actual: str | None
    delta: Decimal
    currency: str
    rate_card_version: str | None
    source_page: int | None
    evidence: dict[str, Any]
    first_seen: datetime
    last_seen: datetime
    sightings: int = 1
    status: str = Status.OPEN
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status == Status.OPEN

    def age_days(self, now: datetime | None = None) -> int:
        return ((now or _now()) - self.first_seen).days

    def escalated(self, now: datetime | None = None) -> bool:
        return self.is_open and self.age_days(now) >= ESCALATION_DAYS

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "flag": self.flag_id,
            "severity": self.severity,
            "invoice_no": self.invoice_no,
            "warehouse_id": self.warehouse_id,
            "client_id": self.client_id,
            "period_month": self.period_month,
            "line_no": self.line_no,
            "line_description": self.line_description,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
            "delta": str(self.delta),
            "currency": self.currency,
            "rate_card_version": self.rate_card_version,
            "source_page": self.source_page,
            "evidence": self.evidence,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "sightings": self.sightings,
            "status": self.status,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by": self.resolved_by,
            "resolution": self.resolution,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FlagRecord":
        def dt(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        return cls(
            fingerprint=raw["fingerprint"],
            flag_id=raw["flag"],
            severity=raw["severity"],
            invoice_no=raw["invoice_no"],
            warehouse_id=raw["warehouse_id"],
            client_id=raw["client_id"],
            period_month=raw["period_month"],
            line_no=raw.get("line_no"),
            line_description=raw.get("line_description"),
            message=raw["message"],
            expected=raw.get("expected"),
            actual=raw.get("actual"),
            delta=money(raw.get("delta", "0")),
            currency=raw.get("currency", "USD"),
            rate_card_version=raw.get("rate_card_version"),
            source_page=raw.get("source_page"),
            evidence=raw.get("evidence", {}),
            first_seen=dt(raw["first_seen"]),  # type: ignore[arg-type]
            last_seen=dt(raw["last_seen"]),  # type: ignore[arg-type]
            sightings=raw.get("sightings", 1),
            status=raw.get("status", Status.OPEN),
            resolved_at=dt(raw.get("resolved_at")),
            resolved_by=raw.get("resolved_by"),
            resolution=raw.get("resolution"),
        )


class FlagStore:
    """JSON-backed, or purely in memory when no path is given.

    Deliberately not a database yet: at dozens of invoices a day this file is
    small, greppable, and diffable in git, which is worth more right now than
    query performance.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._records: dict[str, FlagRecord] = {}
        if self.path and self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for row in raw.get("flags", []):
                record = FlagRecord.from_dict(row)
                self._records[record.fingerprint] = record

    def __len__(self) -> int:
        return len(self._records)

    # -- writing -----------------------------------------------------------

    def upsert(
        self,
        flag: Flag,
        period_month: str,
        currency: str = "USD",
        now: datetime | None = None,
    ) -> FlagRecord:
        """Record a sighting. Returns the record, new or existing.

        An existing record keeps its status: a finding a human resolved stays
        resolved even though the engine keeps re-detecting it on every run.
        """
        stamp = now or _now()
        existing = self._records.get(flag.fingerprint)
        if existing is not None:
            existing.last_seen = stamp
            existing.sightings += 1
            existing.message = flag.message
            return existing

        record = FlagRecord(
            fingerprint=flag.fingerprint,
            flag_id=flag.flag_id,
            severity=flag.severity.value,
            invoice_no=flag.invoice_no,
            warehouse_id=flag.warehouse_id,
            client_id=flag.client_id,
            period_month=period_month,
            line_no=flag.line_no,
            line_description=flag.line_description,
            message=flag.message,
            expected=flag.expected,
            actual=flag.actual,
            delta=flag.delta,
            currency=currency,
            rate_card_version=flag.rate_card_version,
            source_page=flag.source_page,
            evidence=flag.evidence,
            first_seen=flag.created_at or stamp,
            last_seen=stamp,
        )
        self._records[record.fingerprint] = record
        return record

    def resolve(
        self,
        fingerprint: str,
        resolution: str,
        by: str,
        dismissed: bool = False,
        now: datetime | None = None,
    ) -> FlagRecord:
        record = self.get(fingerprint)
        record.status = Status.DISMISSED if dismissed else Status.RESOLVED
        record.resolved_at = now or _now()
        record.resolved_by = by
        record.resolution = resolution
        return record

    def reopen(self, fingerprint: str) -> FlagRecord:
        record = self.get(fingerprint)
        record.status = Status.OPEN
        record.resolved_at = None
        record.resolved_by = None
        record.resolution = None
        return record

    def flush(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "flags": [r.to_dict() for r in self._sorted(self._records.values())]
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    # -- reading -----------------------------------------------------------

    def get(self, fingerprint: str) -> FlagRecord:
        """Accepts a unique prefix, so a human can type the first six chars."""
        if fingerprint in self._records:
            return self._records[fingerprint]
        matches = [k for k in self._records if k.startswith(fingerprint)]
        if len(matches) == 1:
            return self._records[matches[0]]
        if not matches:
            raise KeyError(f"no flag matching {fingerprint!r}")
        raise KeyError(
            f"{fingerprint!r} matches {len(matches)} flags; use more characters"
        )

    def all(self) -> list[FlagRecord]:
        return self._sorted(self._records.values())

    def open_records(self) -> list[FlagRecord]:
        return self._sorted(r for r in self._records.values() if r.is_open)

    def escalated(self, now: datetime | None = None) -> list[FlagRecord]:
        return self._sorted(
            r for r in self._records.values() if r.escalated(now)
        )

    @staticmethod
    def _sorted(records: Iterable[FlagRecord]) -> list[FlagRecord]:
        order = {s.value: s.rank for s in Severity}
        return sorted(
            records,
            key=lambda r: (
                order.get(r.severity, 9),
                r.first_seen,
                r.invoice_no,
                r.line_no or 0,
            ),
        )

    # -- phase 3 reporting -------------------------------------------------

    def trends(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Flags per warehouse, per month.

        This is what answers "warehouse X has over-billed storage three months
        running" without anyone re-reading the invoices.
        """
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for record in self._records.values():
            wh = out.setdefault(record.warehouse_id, {})
            month = wh.setdefault(
                record.period_month,
                {"count": 0, "exposure": ZERO, "by_flag": {}, "open": 0},
            )
            month["count"] += 1
            month["exposure"] += record.delta
            month["by_flag"][record.flag_id] = (
                month["by_flag"].get(record.flag_id, 0) + 1
            )
            if record.is_open:
                month["open"] += 1
        return out

    def repeat_offenders(self, min_months: int = 2) -> list[tuple[str, str, int]]:
        """(warehouse, flag, month count) for patterns that keep recurring."""
        seen: dict[tuple[str, str], set[str]] = {}
        for record in self._records.values():
            key = (record.warehouse_id, record.flag_id)
            seen.setdefault(key, set()).add(record.period_month)
        return sorted(
            (
                (wh, flag, len(months))
                for (wh, flag), months in seen.items()
                if len(months) >= min_months
            ),
            key=lambda row: (-row[2], row[0], row[1]),
        )
